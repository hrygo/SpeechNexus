"""Short PostgreSQL plan transactions; one snapshot, one immutable row."""

from __future__ import annotations

import hashlib
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
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
    canonical_bytes,
    plan_digest,
)
from speechnexus_backend.speech.infrastructure.deletion_plan_schema import (
    DELETION_PLAN_INSERT_COLUMNS,
    DELETION_PLANS,
)
from speechnexus_backend.speech.infrastructure.postgres_pool import SpeechPool
from speechnexus_portable_sql.clock import now_us

_ACTIVE_JOB_STATES = frozenset({"pending", "running", "cancelling"})
# The route checks only its own authority. These are not part of the process
# startup gate: a deployment that never serves deletion preview must still boot.
_PRIVILEGES = (
    ("SELECT", "speech_assets", None),
    ("SELECT", "speech_voice_capability_evidence", None),
    ("SELECT", "speech_voice_revisions", None),
    ("SELECT", "speech_workspaces", None),
    ("SELECT", "speech_asset_objects", None),
    ("SELECT", "speech_asset_references", None),
    ("SELECT", "speech_jobs", None),
    ("SELECT", "speech_job_intents", None),
    ("SELECT", "speech_artifacts", None),
    ("SELECT", "speech_voice_draft_revisions", None),
    ("SELECT", "speech_voice_drafts", None),
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
            "WHERE workspace_id = ? AND workspace_state = 'active' FOR KEY SHARE",
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
    if target.kind == "artifact":
        return artifact_snapshot(reader, workspace_id, target, exclusive=exclusive)
    if target.kind == "voice_revision":
        return voice_revision_snapshot(reader, workspace_id, target)
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
        "WHERE a.asset_id = ? AND a.workspace_id = ? "
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
        "WHERE asset_id = ? AND released_at_us IS NULL "
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
        "WHERE j.job_id = ? AND j.workspace_id = ? "
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
        "WHERE generation_job_id = ? ORDER BY draft_id, revision",
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
        "WHERE source_operation_id = ? AND workspace_id = ? ORDER BY artifact_id",
        (target.resource_id, workspace_id),
    )
    intents = reader.rows(
        "SELECT job_id, request_digest FROM speech_job_intents WHERE job_id = ?",
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


def artifact_snapshot(
    reader: DeletionRows,
    workspace_id: str,
    target: DeletionTarget,
    *,
    exclusive: bool = False,
) -> PlanSnapshot | None:
    """Pin one committed Artifact row and the execution evidence that references it.

    An Artifact is immutable and has no revision column, so its public revision is
    always ``1``; the content identity (digest, object key and storage version)
    lives in ``target_digest`` and the private snapshot instead. A row the
    retention path already marked ``deleted`` is not a target any more.
    """
    if target.revision != 1:
        # An Artifact has no revision column: revision 2 is not a newer artifact,
        # it is a different resource that does not exist.
        return None
    if not lock_workspace(reader, workspace_id):
        return None
    # Preview reads without a row lock, so it needs only SELECT. Execution takes
    # FOR UPDATE to keep the row from moving between the recheck and the purge.
    rows = reader.rows(
        "SELECT artifact_id, workspace_id, artifact_kind, artifact_state, media_type, "
        "bytes, sha256, object_key, storage_version_id "
        "FROM speech_artifacts "
        "WHERE artifact_id = ? AND workspace_id = ? " + ("FOR UPDATE" if exclusive else ""),
        (target.resource_id, workspace_id),
    )
    if not rows:
        return None
    row = rows[0]
    if row["artifact_state"] == "deleted":
        return None
    dependencies = tuple(
        Dependency(
            DeletionTarget(
                "speech",
                "operation_content",
                cast(str, evidence["validation_job_id"]),
                _number(evidence["status_version"], "validation job revision"),
            ),
            "persistent_reference",
            "blocks",
        )
        for evidence in reader.rows(
            "SELECT e.validation_job_id, j.status_version "
            "FROM speech_voice_capability_evidence AS e "
            "JOIN speech_jobs AS j ON j.job_id = e.validation_job_id "
            "WHERE e.report_artifact_id = ? ORDER BY e.validation_job_id",
            (target.resource_id,),
        )
    )
    return PlanSnapshot(
        target=DeletionTarget("speech", "artifact", target.resource_id, 1),
        dependencies=dependencies,
        target_digest=cast(str, row["sha256"]),
        content_snapshot={
            "artifact_id": row["artifact_id"],
            "workspace_id": row["workspace_id"],
            "artifact_kind": row["artifact_kind"],
            "media_type": row["media_type"],
            "bytes": row["bytes"],
            "sha256": row["sha256"],
            "object_key": row["object_key"],
            "storage_version_id": row["storage_version_id"],
        },
    )


def _content_digest(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def voice_revision_snapshot(
    reader: DeletionRows,
    workspace_id: str,
    target: DeletionTarget,
) -> PlanSnapshot | None:
    """Pin one draft revision plus every live dependency this release can judge.

    The public identity is ``(draft_id, revision)``: the identifier is the draft
    and the revision number is the revision, matching how the pre-existing Job
    snapshot names a draft revision. The private snapshot stores only digests of
    the two content columns, never their text.
    """
    rows = reader.rows(
        "SELECT r.draft_id, r.revision, r.kind, r.generation_job_id, r.input_json, "
        "r.binding_json, d.head_revision, j.state AS job_state, "
        "j.status_version, j.workspace_id "
        "FROM speech_voice_draft_revisions AS r "
        "JOIN speech_voice_drafts AS d ON d.draft_id = r.draft_id "
        "JOIN speech_jobs AS j ON j.job_id = r.generation_job_id "
        "WHERE r.draft_id = ? AND r.revision = ? AND j.workspace_id = ?",
        (target.resource_id, str(target.revision), workspace_id),
    )
    if not rows:
        return None
    row = rows[0]
    me = DeletionTarget(
        "speech", "voice_revision", cast(str, row["draft_id"]), int(row["revision"])
    )
    dependencies: list[Dependency] = []
    if str(row["head_revision"]) == str(row["revision"]):
        # The draft still points here; deleting the head would leave that pointer
        # with no row to resolve, so the target blocks on itself by name.
        dependencies.append(Dependency(me, "persistent_reference", "blocks"))
    if row["job_state"] in _ACTIVE_JOB_STATES:
        dependencies.append(
            Dependency(
                DeletionTarget(
                    "speech",
                    "operation_content",
                    cast(str, row["generation_job_id"]),
                    _number(row["status_version"], "job revision"),
                ),
                "active_execution",
                "blocks",
            )
        )
    # Evidence and immutable published revisions hold the same FK reference, so
    # either one makes the row undeletable; both are execution evidence this route
    # must not touch.
    for reference in reader.rows(
        "SELECT validation_job_id, j.status_version "
        "FROM speech_voice_capability_evidence AS e "
        "JOIN speech_jobs AS j ON j.job_id = e.validation_job_id "
        "WHERE e.draft_id = ? AND e.draft_revision = ? ORDER BY e.validation_job_id",
        (target.resource_id, str(target.revision)),
    ):
        dependencies.append(
            Dependency(
                DeletionTarget(
                    "speech",
                    "operation_content",
                    cast(str, reference["validation_job_id"]),
                    _number(reference["status_version"], "validation job revision"),
                ),
                "persistent_reference",
                "blocks",
            )
        )
    if reader.rows(
        "SELECT 1 FROM speech_voice_revisions WHERE draft_id = ? AND draft_revision = ?",
        (target.resource_id, str(target.revision)),
    ):
        dependencies.append(Dependency(me, "persistent_reference", "blocks"))
    # The public Job intent is not deleted by this release (#540). A revision a
    # retain intent still names must be reported, not removed into a dangling
    # body.
    for intent in reader.rows(
        "SELECT i.job_id, j.status_version "
        "FROM speech_job_intents AS i "
        "JOIN speech_jobs AS j ON j.job_id = i.job_id "
        "WHERE i.request_document IS JSON "
        "AND (CAST(i.request_document AS jsonb) -> 'input' ->> 'voice_draft_id') = ? "
        "AND (CAST(i.request_document AS jsonb) -> 'input' ->> 'draft_revision') = ? "
        "ORDER BY i.job_id",
        (target.resource_id, str(target.revision)),
    ):
        dependencies.append(
            Dependency(
                DeletionTarget(
                    "speech",
                    "operation_content",
                    cast(str, intent["job_id"]),
                    _number(intent["status_version"], "job revision"),
                ),
                "persistent_reference",
                "blocks",
            )
        )
    input_sha = _content_digest(cast(str, row["input_json"]))
    binding_sha = _content_digest(cast(str, row["binding_json"]))
    identity = {
        "draft_id": row["draft_id"],
        "revision": row["revision"],
        "kind": row["kind"],
        "generation_job_id": row["generation_job_id"],
        "input_sha256": input_sha,
        "binding_sha256": binding_sha,
    }
    return PlanSnapshot(
        target=me,
        dependencies=tuple(dependencies),
        target_digest="sha256:" + hashlib.sha256(canonical_bytes(identity)).hexdigest(),
        content_snapshot=identity,
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
            "SELECT set_config('statement_timeout', ?, true)",
            (str(max(1, int(remaining * 1000))),),
        )
        cursor = self._connection.execute(statement, args)
        return cursor.fetchall() if cursor.description else []

    def execute(self, sql: str, args: tuple[object, ...] = ()) -> Any:
        """便携时钟的执行入口:与 `rows` 共用预算,把游标交回去取行."""

        remaining = self._deadline - time.monotonic()
        if remaining <= 0:
            raise _unavailable()
        self._connection.execute(
            "SELECT set_config('statement_timeout', ?, true)",
            (str(max(1, int(remaining * 1000))),),
        )
        return self._connection.execute(sql, args)

    def now(self) -> datetime:
        """「现在」只从便携时钟读一次;语句里不再有品牌函数."""

        return datetime.fromtimestamp(now_us(cast(Any, self)) / 1_000_000, tz=UTC)

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
            "WHERE workspace_id = ? AND idempotency_key_digest = ?",
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
            "WHERE workspace_id = ? AND idempotency_key_digest = ?",
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
