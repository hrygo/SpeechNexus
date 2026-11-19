"""Short PostgreSQL plan transactions; one locked snapshot, one immutable row."""

from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from typing import Any, Protocol, cast

import psycopg
from psycopg.types.json import Jsonb
from psycopg_pool import PoolClosed, PoolTimeout, TooManyRequests

from speechnexus_backend.speech.application.deletions import (
    PlanReceipt,
    PlanSnapshot,
)
from speechnexus_backend.speech.domain.deletion import (
    DeletionIdempotencyConflictError,
    DeletionPlan,
    DeletionPlanConfigurationError,
    DeletionPlanUnavailableError,
    DeletionReferenceUnsupportedError,
    DeletionTarget,
    DeletionTargetUnsupportedError,
    Dependency,
    plan_digest,
)
from speechnexus_backend.speech.infrastructure.deletion_plan_schema import (
    DELETION_PLAN_INSERT_COLUMNS,
    DELETION_PLANS,
)
from speechnexus_backend.speech.infrastructure.postgres_pool import SpeechPool

_ACTIVE_JOB_STATES = frozenset({"pending", "running", "cancelling"})
# The route checks only its own authority. These are not part of the process
# startup gate: a deployment that never serves deletion preview must still boot.
_PRIVILEGES = (
    ("SELECT", "speech_assets", None),
    ("SELECT", "speech_workspaces", None),
    ("SELECT", "speech_asset_objects", None),
    ("SELECT", "speech_asset_references", None),
    ("SELECT", "speech_jobs", None),
    ("SELECT", "speech_job_intents", None),
    ("SELECT", "speech_artifacts", None),
    ("SELECT", "speech_voice_draft_revisions", None),
    ("INSERT", "speech_deletion_plans", "*"),
    # Both row locks require UPDATE; the columns are never assigned.
    ("UPDATE", "speech_assets", "version"),
    ("UPDATE", "speech_workspaces", "metadata_revision"),
)
# A reference consumer is another owner of the same deployment; a kind this
# release cannot plan for is a refusal, never a silently dropped dependency.
_REFERENCE_KINDS = {"voice_revision": "voice_revision"}

_PLAN_COLUMNS = (
    "id, workspace_id, target_owner, target_kind, target_id, target_revision, mode, "
    "plan_revision, plan_digest, created_at, expires_at, dependencies"
)


def _unavailable() -> DeletionPlanUnavailableError:
    return DeletionPlanUnavailableError("deletion plan storage is unavailable")


def _number(value: Any, what: str) -> int:
    if type(value) is not int or value < 1:
        raise DeletionPlanUnavailableError(f"invalid stored {what}")
    return value


def plan_from_row(row: dict[str, Any]) -> DeletionPlan:
    try:
        dependencies = tuple(
            Dependency(
                DeletionTarget(
                    cast(str, owner), cast(str, kind), cast(str, identifier), int(revision)
                ),
                cast(str, relation),
                cast(str, disposition),
            )
            for owner, kind, identifier, revision, relation, disposition in cast(
                list[list[Any]], row["dependencies"]
            )
        )
        plan = DeletionPlan(
            id=cast(str, row["id"]),
            workspace_id=cast(str, row["workspace_id"]),
            target=DeletionTarget(
                cast(str, row["target_owner"]),
                cast(str, row["target_kind"]),
                cast(str, row["target_id"]),
                int(row["target_revision"]),
            ),
            mode=cast(str, row["mode"]),
            revision=int(row["plan_revision"]),
            created_at=cast(datetime, row["created_at"]),
            expires_at=cast(datetime, row["expires_at"]),
            dependencies=dependencies,
        )
    except (KeyError, TypeError, ValueError) as error:
        raise DeletionPlanUnavailableError("stored deletion plan is not valid") from error
    if plan_digest(plan) != row["plan_digest"]:
        # A stored plan whose bytes changed is not a snapshot any more.
        raise DeletionPlanUnavailableError("stored deletion plan failed its digest")
    return plan


class DeletionRows(Protocol):
    """The one cursor surface the plan and execution transactions share."""

    def rows(self, statement: str, args: tuple[object, ...] = ()) -> list[dict[str, Any]]: ...


def lock_workspace(reader: DeletionRows, workspace_id: str) -> bool:
    """Take the FK parent lock before any child row, like every asset writer.

    The plan insert takes a key-share lock on ``speech_workspaces`` anyway;
    acquiring it first keeps one order with ``_locked_asset`` (workspace, then
    asset) instead of locking the child and waiting on the parent. Execution uses
    the same order, so a concurrent deletion and asset write cannot deadlock.
    """
    return bool(
        reader.rows(
            "SELECT workspace_id FROM speech_workspaces "
            "WHERE workspace_id = %s AND workspace_state = 'active' FOR KEY SHARE",
            (workspace_id,),
        )
    )


def snapshot_target(
    reader: DeletionRows,
    workspace_id: str,
    target: DeletionTarget,
    *,
    exclusive: bool = False,
) -> PlanSnapshot | None:
    """Read the pinned target under the shared workspace -> target lock order.

    ``exclusive`` upgrades the target row lock from ``FOR SHARE`` (preview: pin the
    revision) to ``FOR UPDATE`` (execution: this transaction will expire and delete
    it). The workspace key-share lock is taken first either way, so execution and
    the asset writers cannot invert the order and deadlock.
    """
    if target.kind == "asset":
        return asset_snapshot(reader, workspace_id, target, exclusive=exclusive)
    if target.kind == "operation_content":
        return job_snapshot(reader, workspace_id, target, exclusive=exclusive)
    raise DeletionTargetUnsupportedError


def asset_snapshot(
    reader: DeletionRows,
    workspace_id: str,
    target: DeletionTarget,
    *,
    exclusive: bool = False,
) -> PlanSnapshot | None:
    if not lock_workspace(reader, workspace_id):
        return None
    rows = reader.rows(
        "SELECT a.asset_id, a.workspace_id, a.version, a.sha256, a.asset_state, "
        "a.object_key, o.storage_version_id "
        "FROM speech_assets AS a "
        "LEFT JOIN speech_asset_objects AS o ON o.asset_id = a.asset_id "
        "WHERE a.asset_id = %s AND a.workspace_id = %s "
        + ("FOR UPDATE OF a" if exclusive else "FOR SHARE OF a"),
        (target.resource_id, workspace_id),
    )
    if not rows:
        return None
    row = rows[0]
    dependencies = tuple(
        Dependency(
            DeletionTarget(
                "speech",
                _REFERENCE_KINDS[cast(str, reference["consumer_kind"])],
                cast(str, reference["consumer_id"]),
                int(reference["consumer_revision"]),
            ),
            "persistent_reference",
            "blocks",
        )
        for reference in asset_references(reader, target.resource_id)
    )
    return PlanSnapshot(
        target=DeletionTarget(
            "speech", "asset", target.resource_id, _number(row["version"], "asset version")
        ),
        dependencies=dependencies,
        target_digest=cast(str, row["sha256"]),
        content_snapshot={
            "asset_id": row["asset_id"],
            "workspace_id": row["workspace_id"],
            "version": row["version"],
            "sha256": row["sha256"],
            "asset_state": row["asset_state"],
            "object_key": row["object_key"],
            "storage_version_id": row["storage_version_id"],
        },
    )


def asset_references(reader: DeletionRows, asset_id: str) -> list[dict[str, Any]]:
    references = reader.rows(
        "SELECT consumer_kind, consumer_id, consumer_revision "
        "FROM speech_asset_references "
        "WHERE asset_id = %s AND released_at_us IS NULL "
        "ORDER BY consumer_kind, consumer_id, consumer_revision",
        (asset_id,),
    )
    for reference in references:
        kind = reference["consumer_kind"]
        if not isinstance(kind, str) or kind not in _REFERENCE_KINDS:
            raise DeletionReferenceUnsupportedError
    return references


def job_snapshot(
    reader: DeletionRows,
    workspace_id: str,
    target: DeletionTarget,
    *,
    exclusive: bool = False,
) -> PlanSnapshot | None:
    if not lock_workspace(reader, workspace_id):
        return None
    rows = reader.rows(
        "SELECT j.job_id, j.workspace_id, j.status_version, j.request_digest, j.state "
        "FROM speech_jobs AS j "
        "WHERE j.job_id = %s AND j.workspace_id = %s "
        + ("FOR UPDATE OF j" if exclusive else "FOR SHARE OF j"),
        (target.resource_id, workspace_id),
    )
    if not rows:
        return None
    row = rows[0]
    current = DeletionTarget(
        "speech",
        "operation_content",
        cast(str, row["job_id"]),
        _number(row["status_version"], "job revision"),
    )
    dependencies: list[Dependency] = []
    if row["state"] in _ACTIVE_JOB_STATES:
        # The content cannot be removed while the execution that owns it may
        # still write. The blocker is this operation itself; cancellation is
        # not stop evidence.
        dependencies.append(Dependency(current, "active_execution", "blocks"))
    for draft in reader.rows(
        "SELECT draft_id, revision FROM speech_voice_draft_revisions "
        "WHERE generation_job_id = %s ORDER BY draft_id, revision",
        (target.resource_id,),
    ):
        dependencies.append(
            Dependency(
                DeletionTarget(
                    "speech",
                    "voice_revision",
                    cast(str, draft["draft_id"]),
                    int(draft["revision"]),
                ),
                "persistent_reference",
                "blocks",
            )
        )
    artifacts = reader.rows(
        "SELECT artifact_id, artifact_state, sha256, object_key, storage_version_id "
        "FROM speech_artifacts "
        "WHERE source_operation_id = %s AND workspace_id = %s ORDER BY artifact_id",
        (target.resource_id, workspace_id),
    )
    intents = reader.rows(
        "SELECT job_id, request_digest FROM speech_job_intents WHERE job_id = %s",
        (target.resource_id,),
    )
    return PlanSnapshot(
        target=current,
        dependencies=tuple(dependencies),
        target_digest=cast(str, row["request_digest"]),
        content_snapshot={
            "job_id": row["job_id"],
            "workspace_id": row["workspace_id"],
            "state": row["state"],
            "status_version": row["status_version"],
            "request_digest": row["request_digest"],
            "artifacts": [
                {
                    "artifact_id": artifact["artifact_id"],
                    "artifact_state": artifact["artifact_state"],
                    "sha256": artifact["sha256"],
                    "object_key": artifact["object_key"],
                    "storage_version_id": artifact["storage_version_id"],
                }
                for artifact in artifacts
            ],
            "intents": [
                {
                    "job_id": intent["job_id"],
                    "request_digest": intent["request_digest"],
                }
                for intent in intents
            ],
        },
    )


class _Transaction:
    def __init__(
        self,
        connection: psycopg.Connection[dict[str, Any]],
        deadline: float,
        workspace_id: str,
    ) -> None:
        self._connection = connection
        self._deadline = deadline
        self._workspace_id = workspace_id

    def rows(self, statement: str, args: tuple[object, ...] = ()) -> list[dict[str, Any]]:
        remaining = self._deadline - time.monotonic()
        if remaining <= 0:
            raise _unavailable()
        self._connection.execute(
            "SELECT set_config('statement_timeout', %s, true)",
            (str(max(1, int(remaining * 1000))),),
        )
        cursor = self._connection.execute(statement, args)
        return cursor.fetchall() if cursor.description else []

    def now(self) -> datetime:
        rows = self.rows("SELECT clock_timestamp() AS now")
        return cast(datetime, rows[0]["now"])

    def missing_privileges(self) -> tuple[str, ...]:
        placeholders = ", ".join(["(%s, %s, %s)"] * len(_PRIVILEGES))
        rows = self.rows(
            "SELECT v.privilege, v.table_name, v.column_name, "
            "CASE WHEN v.column_name IS NULL "
            "THEN pg_catalog.has_table_privilege(current_user, v.table_name, v.privilege) "
            "WHEN v.column_name = '*' "
            "THEN pg_catalog.has_any_column_privilege(current_user, v.table_name, v.privilege) "
            "ELSE pg_catalog.has_column_privilege("
            "current_user, v.table_name, v.column_name, v.privilege) END AS ok "
            f"FROM (VALUES {placeholders}) AS v(privilege, table_name, column_name)",
            tuple(item for probe in _PRIVILEGES for item in probe),
        )
        return tuple(
            f"{row['privilege']} on {row['table_name']}"
            + (f".{row['column_name']}" if row["column_name"] not in (None, "*") else "")
            for row in rows
            if not row["ok"]
        )

    def replay(self, key_digest: str) -> PlanReceipt | None:
        rows = self.rows(
            f"SELECT {_PLAN_COLUMNS}, intent_digest FROM {DELETION_PLANS} "
            "WHERE workspace_id = %s AND idempotency_key_digest = %s",
            (self._workspace_id, key_digest),
        )
        if not rows:
            return None
        return PlanReceipt(cast(str, rows[0]["intent_digest"]), plan_from_row(rows[0]))

    def snapshot(self, target: DeletionTarget, mode: str) -> PlanSnapshot | None:
        return snapshot_target(self, self._workspace_id, target)

    def save(
        self,
        plan: DeletionPlan,
        *,
        key_digest: str,
        intent_digest: str,
        target_digest: str,
        content_snapshot: dict[str, object],
    ) -> DeletionPlan:
        if plan.workspace_id != self._workspace_id:
            raise ValueError("Deletion plan workspace does not match the transaction")
        dependencies = [
            [
                item.target.owner,
                item.target.kind,
                item.target.resource_id,
                str(item.target.revision),
                item.relation,
                item.disposition,
            ]
            for item in plan.dependencies
        ]
        columns = ", ".join(DELETION_PLAN_INSERT_COLUMNS)
        placeholders = ", ".join(["%s"] * len(DELETION_PLAN_INSERT_COLUMNS))
        self.rows(
            f"INSERT INTO {DELETION_PLANS} ({columns}) VALUES ({placeholders}) "
            "ON CONFLICT (workspace_id, idempotency_key_digest) DO NOTHING",
            (
                plan.id,
                plan.workspace_id,
                plan.target.owner,
                plan.target.kind,
                plan.target.resource_id,
                str(plan.target.revision),
                target_digest,
                plan.mode,
                str(plan.revision),
                plan_digest(plan),
                key_digest,
                intent_digest,
                plan.created_at,
                plan.expires_at,
                Jsonb(dependencies),
                Jsonb(content_snapshot),
            ),
        )
        # The unique key makes the winner authoritative: a concurrent request
        # with the same key either replays it (same intent) or conflicts.
        stored = self.rows(
            f"SELECT {_PLAN_COLUMNS}, intent_digest FROM {DELETION_PLANS} "
            "WHERE workspace_id = %s AND idempotency_key_digest = %s",
            (self._workspace_id, key_digest),
        )
        if not stored:
            raise _unavailable()
        if stored[0]["intent_digest"] != intent_digest:
            raise DeletionIdempotencyConflictError
        return plan_from_row(stored[0])


class PostgresDeletionPlans:
    """Bounded transactions over the public Job writer identity."""

    def __init__(self, pool: SpeechPool, *, timeout_seconds: float = 3) -> None:
        if type(timeout_seconds) not in (int, float) or not 0 < timeout_seconds <= 30:
            raise ValueError("Deletion plan transactions require bounded deadlines")
        self._pool = pool
        self._timeout = timeout_seconds

    def missing_privileges(self) -> tuple[str, ...]:
        """Probe the route's own authority without opening a plan transaction."""
        with self._pool.connection(timeout=self._timeout) as connection:
            transaction = _Transaction(
                connection, time.monotonic() + self._timeout, workspace_id=""
            )
            return transaction.missing_privileges()

    @contextmanager
    def transaction(self, workspace_id: str) -> Iterator[_Transaction]:
        deadline = time.monotonic() + self._timeout
        try:
            with (
                self._pool.connection(timeout=self._timeout) as connection,
                connection.transaction(),
            ):
                transaction = _Transaction(connection, deadline, workspace_id)
                missing = transaction.missing_privileges()
                if missing:
                    raise DeletionPlanConfigurationError(
                        "deletion preview lacks " + ", ".join(missing)
                    )
                yield transaction
        except DeletionIdempotencyConflictError:
            raise
        except psycopg.Error, PoolClosed, PoolTimeout, TooManyRequests:
            raise _unavailable() from None
