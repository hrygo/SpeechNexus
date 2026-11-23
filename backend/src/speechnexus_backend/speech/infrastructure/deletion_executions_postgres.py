"""PostgreSQL deletion execution: one locked acceptance, one confirmed completion.

Execution re-checks the plan's complete scope under the same lock order as the
preview (``speech_workspaces FOR KEY SHARE`` first, then the target row, now
``FOR UPDATE``), removes the confirmed content in that one transaction, and writes
the durable ledger row before returning. The asset object purge is the only step
outside a database transaction -- object I/O never runs inside one -- so a crash
between the two leaves the ledger ``queued`` and replaying the original
``Idempotency-Key`` resumes the same row instead of deleting again.

Every refusal is a refusal: no partial cascade is committed, and a storage or
authorization failure surfaces as ``deletion_unavailable`` / ``deletion_not_configured``
rather than a silent success.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, cast

from speechnexus_backend.speech.application.deletions import AcceptedDeletion
from speechnexus_backend.speech.domain.assets import VerifiedObject
from speechnexus_backend.speech.domain.deletion import (
    DeletionDependencyConflictError,
    DeletionError,
    DeletionFailure,
    DeletionIdempotencyConflictError,
    DeletionLedgerConfigurationError,
    DeletionLedgerUnavailableError,
    DeletionObjectStorageUnavailableError,
    DeletionPlan,
    DeletionPlanChangedError,
    DeletionRecord,
    DeletionRequest,
    DeletionResult,
    DeletionTarget,
    DeletionTargetNotFoundError,
    DeletionTargetUnsupportedError,
    confirm_scope,
    confirmed_targets,
    dependency_snapshot,
    failure_payload,
    plan_digest,
    retained_independent_copies,
)
from speechnexus_backend.speech.infrastructure.assets_postgres import (
    CONFIRMED_REASON,
    AssetCursor,
    complete_content_deletion,
    prepare_content_deletion,
)
from speechnexus_backend.speech.infrastructure.deletion_ledger_schema import (
    ASSET_EVENT_INSERT_COLUMNS,
    ASSET_UPDATE_COLUMNS,
    CONTENT_DELETION_INSERT_COLUMNS,
    CONTENT_DELETION_UPDATE_COLUMNS,
    DELETION_LEDGER,
    JOB_INTENT_DELETE_TABLES,
    LEGACY_INTENT_UPDATE_COLUMNS,
)
from speechnexus_backend.speech.infrastructure.deletion_plans_postgres import (
    lock_workspace,
    plan_from_row,
    snapshot_target,
)
from speechnexus_backend.speech.infrastructure.postgres_pool import SpeechPool
from speechnexus_portable_sql.clock import ClockReader, now_us
from speechnexus_portable_sql.transaction import is_savepoint_control
from speechnexus_portable_sql.vendor import Jsonb, PoolClosed, PoolTimeout, TooManyRequests, psycopg

_PLAN_ROW_COLUMNS = (
    "id, workspace_id, target_owner, target_kind, target_id, target_revision, "
    "target_digest, mode, plan_revision, plan_digest, created_at, expires_at, "
    "dependencies, content_snapshot"
)
_LEDGER_ROW_COLUMNS = (
    "id, workspace_id, plan_id, plan_revision, plan_digest, target_owner, target_kind, "
    "target_id, target_revision, idempotency_key_digest, intent_digest, deleted, retained, "
    "error, status, cleanup_state, content_ledger_id, purge_claimed_at, created_at, "
    "updated_at, completed_at"
)
# How long one executor may hold the purge claim. It must exceed any object purge
# the deployment allows, because a live claim is what stops a second purge; an
# expired claim is the crash-recovery boundary the next same-key request crosses.
PURGE_CLAIM_LEASE_SECONDS = 120

# The route checks only its own authority. It is not a process startup condition:
# a writer that never serves deletion must still boot, and one that is missing
# these grants gets a diagnosable 503 on this route (exactly like the preview).
_PRIVILEGES: tuple[tuple[str, str, str | None], ...] = (
    ("SELECT", DELETION_LEDGER, None),
    ("SELECT", "speech_deletion_plans", None),
    ("SELECT", "speech_workspaces", None),
    ("SELECT", "speech_assets", None),
    ("SELECT", "speech_asset_objects", None),
    ("SELECT", "speech_asset_references", None),
    ("SELECT", "speech_content_deletions", None),
    ("SELECT", "speech_asset_events", None),
    ("SELECT", "speech_jobs", None),
    ("SELECT", "speech_job_intents", None),
    ("SELECT", "speech_voice_draft_revisions", None),
    ("SELECT", "speech_voice_drafts", None),
    # Row lock only: admission holds FOR SHARE on the draft while it reads the
    # revision, so the executor needs UPDATE to take FOR UPDATE. No column is
    # ever assigned by this route.
    ("UPDATE", "speech_voice_drafts", "head_revision"),
    ("SELECT", "speech_voice_capability_evidence", None),
    ("SELECT", "speech_voice_revisions", None),
    ("SELECT", "speech_artifacts", None),
    ("UPDATE", "speech_artifacts", "*"),
    ("DELETE", "speech_voice_draft_revisions", None),
    ("UPDATE", "speech_deletion_ledger", "*"),
    ("INSERT", DELETION_LEDGER, "*"),
    *(("INSERT", "speech_content_deletions", column) for column in CONTENT_DELETION_INSERT_COLUMNS),
    *(("UPDATE", "speech_content_deletions", column) for column in CONTENT_DELETION_UPDATE_COLUMNS),
    *(("INSERT", "speech_asset_events", column) for column in ASSET_EVENT_INSERT_COLUMNS),
    *(("UPDATE", "speech_assets", column) for column in ASSET_UPDATE_COLUMNS),
    ("UPDATE", "speech_workspaces", "metadata_revision"),
    *(("UPDATE", "admission_operations", column) for column in LEGACY_INTENT_UPDATE_COLUMNS),
    *(("DELETE", table, None) for table in JOB_INTENT_DELETE_TABLES),
)

# The read surface projects this one row; it needs no cache, plan or content table
# and no write privilege. Probing the executor's full surface here would answer 503
# for a deployment that granted the read exactly what it needs, and would let the
# read inherit write authority it never uses.
_READ_PRIVILEGES: tuple[tuple[str, str, str | None], ...] = (("SELECT", DELETION_LEDGER, None),)


def _unavailable() -> DeletionLedgerUnavailableError:
    return DeletionLedgerUnavailableError("deletion execution storage is unavailable")


_INSERT_LEDGER = (
    "INSERT INTO speech_deletion_ledger (id, workspace_id, plan_id, plan_revision, "
    "plan_digest, target_owner, target_kind, target_id, target_revision, "
    "idempotency_key_digest, intent_digest, deleted, retained, error, status, "
    "cleanup_state, content_ledger_id, created_at, updated_at, completed_at) "
    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
)


_CLAIM_PURGE = (
    "UPDATE speech_deletion_ledger SET purge_claimed_at = ?, updated_at = ? "
    "WHERE id = ? AND status = 'queued' "
    "AND (purge_claimed_at IS NULL OR purge_claimed_at <= "
    "? - make_interval(secs => ?))"
)
_RELEASE_PURGE = (
    "UPDATE speech_deletion_ledger SET purge_claimed_at = NULL, updated_at = ? "
    "WHERE id = ? AND status = 'queued'"
)


_DELETE_JOB_INTENT = "DELETE FROM speech_job_intents WHERE job_id = ? AND request_digest = ?"
_TOMBSTONE_ARTIFACT = (
    "UPDATE speech_artifacts SET artifact_state = 'deleted', deleted_at_us = ? "
    "WHERE artifact_id = ? AND workspace_id = ? "
    "AND artifact_state IN ('ready','expired') "
    "AND sha256 = ? AND object_key = ? AND storage_version_id = ?"
)
_SETTLE_LEDGER = (
    "UPDATE speech_deletion_ledger SET status = 'succeeded', cleanup_state = 'complete', "
    "completed_at = ?, updated_at = ? WHERE id = ? AND status <> 'succeeded'"
)


_SELECT_LEDGER_BY_KEY = (
    "SELECT "
    "id, workspace_id, plan_id, plan_revision, plan_digest, target_owner, "
    "target_kind, target_id, target_revision, idempotency_key_digest, "
    "intent_digest, deleted, retained, error, status, cleanup_state, "
    "content_ledger_id, purge_claimed_at, created_at, updated_at, completed_at "
    "FROM speech_deletion_ledger WHERE workspace_id = ? AND idempotency_key_digest = ?"
)
_SELECT_LEDGER_BY_KEY_LOCKED = (
    "SELECT "
    "id, workspace_id, plan_id, plan_revision, plan_digest, target_owner, "
    "target_kind, target_id, target_revision, idempotency_key_digest, "
    "intent_digest, deleted, retained, error, status, cleanup_state, "
    "content_ledger_id, purge_claimed_at, created_at, updated_at, completed_at "
    "FROM speech_deletion_ledger WHERE workspace_id = ? AND idempotency_key_digest = ? "
    "FOR UPDATE"
)
_SELECT_LEDGER_BY_ID_LOCKED = (
    "SELECT "
    "id, workspace_id, plan_id, plan_revision, plan_digest, target_owner, "
    "target_kind, target_id, target_revision, idempotency_key_digest, "
    "intent_digest, deleted, retained, error, status, cleanup_state, "
    "content_ledger_id, purge_claimed_at, created_at, updated_at, completed_at "
    "FROM speech_deletion_ledger WHERE workspace_id = ? AND id = ? FOR UPDATE"
)
_SELECT_LEDGER_FOR_PLAN = (
    "SELECT "
    "id, workspace_id, plan_id, plan_revision, plan_digest, target_owner, "
    "target_kind, target_id, target_revision, idempotency_key_digest, "
    "intent_digest, deleted, retained, error, status, cleanup_state, "
    "content_ledger_id, purge_claimed_at, created_at, updated_at, completed_at "
    "FROM speech_deletion_ledger WHERE workspace_id = ? AND plan_id = ?"
)
_SELECT_QUEUED_LEDGER = (
    "SELECT "
    "id, workspace_id, plan_id, plan_revision, plan_digest, target_owner, "
    "target_kind, target_id, target_revision, idempotency_key_digest, "
    "intent_digest, deleted, retained, error, status, cleanup_state, "
    "content_ledger_id, purge_claimed_at, created_at, updated_at, completed_at "
    "FROM speech_deletion_ledger WHERE workspace_id = ? AND id = ? AND status = 'queued'"
)
_SELECT_PLAN_BY_ID = (
    "SELECT "
    "id, workspace_id, target_owner, target_kind, target_id, target_revision, "
    "target_digest, mode, plan_revision, plan_digest, created_at, expires_at, "
    "dependencies, content_snapshot "
    "FROM speech_deletion_plans WHERE id = ? AND workspace_id = ?"
)


_SELECT_LEDGER_BY_PK = (
    "SELECT "
    "id, workspace_id, plan_id, plan_revision, plan_digest, target_owner, "
    "target_kind, target_id, target_revision, idempotency_key_digest, "
    "intent_digest, deleted, retained, error, status, cleanup_state, "
    "content_ledger_id, purge_claimed_at, created_at, updated_at, completed_at "
    "FROM speech_deletion_ledger WHERE id = ?"
)
_SELECT_LEDGER_BY_ID = (
    "SELECT "
    "id, workspace_id, plan_id, plan_revision, plan_digest, target_owner, "
    "target_kind, target_id, target_revision, idempotency_key_digest, "
    "intent_digest, deleted, retained, error, status, cleanup_state, "
    "content_ledger_id, purge_claimed_at, created_at, updated_at, completed_at "
    "FROM speech_deletion_ledger WHERE workspace_id = ? AND id = ?"
)
_SELECT_LEDGER_CLAIM_STATE = (
    "SELECT "
    "id, workspace_id, plan_id, plan_revision, plan_digest, target_owner, "
    "target_kind, target_id, target_revision, idempotency_key_digest, "
    "intent_digest, deleted, retained, error, status, cleanup_state, "
    "content_ledger_id, purge_claimed_at, created_at, updated_at, completed_at "
    ", (purge_claimed_at IS NOT NULL AND purge_claimed_at > "
    "? - make_interval(secs => ?)) AS claim_live "
    "FROM speech_deletion_ledger WHERE id = ?"
)


def _targets(items: list[list[Any]]) -> tuple[DeletionTarget, ...]:
    return tuple(
        DeletionTarget(cast(str, owner), cast(str, kind), cast(str, identifier), int(revision))
        for owner, kind, identifier, revision in items
    )


def _scope(targets: tuple[DeletionTarget, ...]) -> list[list[str]]:
    return [[item.owner, item.kind, item.resource_id, str(item.revision)] for item in targets]


@dataclass(frozen=True)
class _LedgerRow:
    row: dict[str, Any]

    def record(self) -> DeletionRecord:
        status = cast(str, self.row["status"])
        completed_at = cast(datetime | None, self.row["completed_at"])
        result = (
            DeletionResult(
                deleted=_targets(cast(list[list[Any]], self.row["deleted"])),
                retained_independent_copies=_targets(cast(list[list[Any]], self.row["retained"])),
                ledger_id=cast(str, self.row["id"]),
                completed_at=completed_at,
            )
            if status == "succeeded" and completed_at is not None
            else None
        )
        error = self.row["error"]
        failure = (
            DeletionFailure(
                code=cast(str, error["code"]),
                type=cast(str, error["type"]),
                message=cast(str, error["message"]),
                param=cast(str | None, error["param"]),
                request_id=cast(str, error["request_id"]),
                retryable=bool(error["retryable"]),
            )
            if isinstance(error, dict)
            else None
        )
        return DeletionRecord(
            id=cast(str, self.row["id"]),
            plan_id=cast(str, self.row["plan_id"]),
            plan_digest=cast(str, self.row["plan_digest"]),
            status=status,
            cleanup_state=cast(str, self.row["cleanup_state"]),
            created_at=cast(datetime, self.row["created_at"]),
            updated_at=cast(datetime, self.row["updated_at"]),
            result=result,
            error=failure,
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
        if is_savepoint_control(statement):
            self._connection.execute(statement, args)
            return []
        self._connection.execute(
            "SELECT set_config('statement_timeout', ?, true)",
            (str(max(1, int(remaining * 1000))),),
        )
        cursor = self._connection.execute(statement, args)
        return cursor.fetchall() if cursor.description else []

    def changed(self, statement: str, args: tuple[object, ...] = ()) -> int:
        """执行写语句并只回报 `rowcount`;命中与否是 CAS 判据,不靠 `RETURNING` 的行。"""

        remaining = self._deadline - time.monotonic()
        if remaining <= 0:
            raise _unavailable()
        if is_savepoint_control(statement):
            return int(self._connection.execute(statement, args).rowcount)
        self._connection.execute(
            "SELECT set_config('statement_timeout', ?, true)",
            (str(max(1, int(remaining * 1000))),),
        )
        return int(self._connection.execute(statement, args).rowcount)

    def now(self) -> datetime:
        """「现在」只从便携时钟读一次,换算回时间点给需要 timestamp 的语句用."""

        return datetime.fromtimestamp(self.now_us() / 1_000_000, tz=UTC)

    def now_us(self) -> int:
        return now_us(cast(ClockReader, self))

    def execute(self, statement: str, args: tuple[object, ...] = ()) -> Any:
        """便携时钟的执行入口:与 `rows` 共用同一预算,把游标交回去取行."""

        remaining = self._deadline - time.monotonic()
        if remaining <= 0:
            raise _unavailable()
        if is_savepoint_control(statement):
            return self._connection.execute(statement, args)
        self._connection.execute(
            "SELECT set_config('statement_timeout', ?, true)",
            (str(max(1, int(remaining * 1000))),),
        )
        return self._connection.execute(statement, args)

    @contextmanager
    def asset_cursor(self) -> Iterator[AssetCursor]:
        with self._connection.cursor() as raw:
            yield AssetCursor(raw, self._deadline)

    def _read_ledger(self, ledger_id: str) -> dict[str, Any] | None:
        rows = self.rows(_SELECT_LEDGER_BY_PK, (ledger_id,))
        return rows[0] if rows else None

    def claim_purge(self, ledger_id: str) -> dict[str, Any] | None:
        """CAS: exactly one executor owns the object purge of this row."""
        # 时间戳必须晚于本行的 created_at(表上有 CHECK),所以只能来自即时时钟,
        # 不能用 CURRENT_TIMESTAMP —— 后者是事务开始时间,同行同事务插入时会比 created_at 早。
        now = self.now()
        # CAS 只看 `rowcount`:语句里没有 `RETURNING`,命中后再按主键读回该行。
        # 同一事务内读得到自己的写,而 CAS 已证明这一行只归本次调用。
        claimed = self.changed(_CLAIM_PURGE, (now, now, ledger_id, now, PURGE_CLAIM_LEASE_SECONDS))
        return self._read_ledger(ledger_id) if claimed else None

    def release_purge(self, ledger_id: str) -> dict[str, Any] | None:
        """Give the claim back after this attempt ended without a completion."""
        released = self.changed(_RELEASE_PURGE, (self.now(), ledger_id))
        return self._read_ledger(ledger_id) if released else None

    def missing_privileges(
        self, probes: tuple[tuple[str, str, str | None], ...] = _PRIVILEGES
    ) -> tuple[str, ...]:
        # 探针行由 schema 常量派生(列级授权必须跟着 DDL 走),所以只有这一条语句按行数拼占位符;
        # 行内容本身不可字面化,否则列清单会在两个地方各写一遍。
        placeholders = ", ".join(["(?, ?, ?)"] * len(probes))
        rows = self.rows(
            "SELECT v.privilege, v.table_name, v.column_name, "
            "CASE WHEN v.column_name IS NULL "
            "THEN pg_catalog.has_table_privilege(current_user, v.table_name, v.privilege) "
            "WHEN v.column_name = '*' "
            "THEN pg_catalog.has_any_column_privilege(current_user, v.table_name, v.privilege) "
            "ELSE pg_catalog.has_column_privilege("
            "current_user, v.table_name, v.column_name, v.privilege) END AS ok "
            f"FROM (VALUES {placeholders}) AS v(privilege, table_name, column_name)",
            tuple(item for probe in probes for item in probe),
        )
        return tuple(
            f"{row['privilege']} on {row['table_name']}"
            + (f".{row['column_name']}" if row["column_name"] not in (None, "*") else "")
            for row in rows
            if not row["ok"]
        )

    def plan_row(self, plan_id: str) -> dict[str, Any] | None:
        rows = self.rows(
            _SELECT_PLAN_BY_ID,
            (plan_id, self._workspace_id),
        )
        return rows[0] if rows else None

    def ledger_by_key(self, key_digest: str, *, lock: bool = False) -> _LedgerRow | None:
        rows = self.rows(
            _SELECT_LEDGER_BY_KEY_LOCKED if lock else _SELECT_LEDGER_BY_KEY,
            (self._workspace_id, key_digest),
        )
        return _LedgerRow(rows[0]) if rows else None

    def ledger_by_id(self, deletion_id: str) -> _LedgerRow | None:
        rows = self.rows(
            _SELECT_LEDGER_BY_ID_LOCKED,
            (self._workspace_id, deletion_id),
        )
        return _LedgerRow(rows[0]) if rows else None

    def ledger_for_plan(self, plan_id: str) -> _LedgerRow | None:
        rows = self.rows(
            _SELECT_LEDGER_FOR_PLAN,
            (self._workspace_id, plan_id),
        )
        return _LedgerRow(rows[0]) if rows else None


class _DeletionExecution:
    """One accept/complete pair over the Job writer's pool."""

    def __init__(self, transaction: _Transaction, workspace_id: str) -> None:
        self._tx = transaction
        self._workspace_id = workspace_id

    # -- acceptance ---------------------------------------------------------
    def accept(
        self,
        request: DeletionRequest,
        *,
        key_digest: str,
        intent_digest: str,
        purge_available: bool,
        artifact_purge_available: bool,
        request_id: str,
    ) -> AcceptedDeletion:
        existing = self._tx.ledger_by_key(key_digest, lock=True)
        if existing is not None:
            # The accepted intent is replayed before any current-state check: the
            # same key returns the stored record even after the target moved.
            if existing.row["intent_digest"] != intent_digest:
                raise DeletionIdempotencyConflictError
            return self._resume(existing)
        plan_row = self._tx.plan_row(request.plan_id)
        if plan_row is None:
            raise DeletionTargetNotFoundError
        plan = plan_from_row(plan_row)
        # The plan's identity, window and confirmed scope are refusals. Its
        # dependency verdict is not: an ineligible plan is accepted as a *blocked*
        # Deletion, so the reason is durable and the status is never `unknown`.
        confirm_scope(plan, request, now=self._tx.now())
        if not plan.eligible:
            return self._blocked(plan, request, key_digest, intent_digest, request_id)
        if plan.target.kind == "voice_revision":
            # Admission takes a FOR SHARE lock on the same draft row before it
            # reads the revision it is about to pin into a Job intent. Taking the
            # row exclusively here, before the dependency re-read, makes that
            # read-or-delete decision one serial order: either admission commits
            # its intent first and the snapshot below sees it, or this deletion
            # commits first and admission finds no revision. Without this lock the
            # intent has no foreign key to catch the race.
            self._tx.rows(
                "SELECT 1 FROM speech_voice_drafts WHERE draft_id = ? FOR UPDATE",
                (plan.target.resource_id,),
            )
        snapshot = snapshot_target(self._tx, self._workspace_id, plan.target, exclusive=True)
        # Re-read the key now that the target lock is held: a concurrent request
        # with the same key may have committed while this transaction waited, and
        # its accepted deletion -- not the moved target -- is the answer.
        late = self._tx.ledger_by_key(key_digest)
        if late is not None:
            if late.row["intent_digest"] != intent_digest:
                raise DeletionIdempotencyConflictError
            return self._resume(late)
        if snapshot is None:
            raise DeletionPlanChangedError
        if (
            snapshot.target != plan.target
            or snapshot.target_digest != plan_row["target_digest"]
            # The private snapshot is the exact content the owner pinned: a moved
            # object version, a new artifact or an extra intent is a different
            # deletion, never one this plan authorized.
            or snapshot.content_snapshot != plan_row["content_snapshot"]
            or dependency_snapshot(snapshot.dependencies, mode=plan.mode) != plan.dependencies
        ):
            raise DeletionPlanChangedError
        if plan.target.kind == "asset":
            return self._accept_asset(plan, request, key_digest, intent_digest, purge_available)
        if plan.target.kind == "operation_content":
            return self._accept_operation_content(
                plan, request, plan_row["target_digest"], key_digest, intent_digest
            )
        if plan.target.kind == "artifact":
            return self._accept_artifact(
                plan,
                request,
                snapshot.content_snapshot,
                key_digest,
                intent_digest,
                artifact_purge_available,
            )
        if plan.target.kind == "voice_revision":
            return self._accept_voice_revision(plan, request, key_digest, intent_digest)
        raise DeletionTargetUnsupportedError

    def _blocked(
        self,
        plan: DeletionPlan,
        request: DeletionRequest,
        key_digest: str,
        intent_digest: str,
        request_id: str,
    ) -> AcceptedDeletion:
        """Record why an ineligible plan cannot remove its target.

        The plan is a single-use credential, so the blocked row is the durable
        answer for this key; resolving the dependency means a new preview and a
        new key. The message names the blocking dependencies and never claims any
        content was removed.
        """
        blockers = [item for item in plan.dependencies if item.disposition == "blocks"]
        names = ", ".join(
            f"{item.target.kind} {item.target.resource_id}@{item.target.revision} ({item.relation})"
            for item in blockers[:8]
        )
        if len(blockers) > 8:
            names += f", and {len(blockers) - 8} more"
        failure = DeletionFailure(
            code="deletion_target_in_use",
            type="invalid_request_error",
            message=(
                "The target is still referenced, so nothing was deleted: "
                + names
                + ". Resolve the dependency, create a new plan and confirm it."
            ),
            param="target",
            request_id=request_id,
            retryable=False,
        )
        return self._record(
            plan,
            request,
            key_digest,
            intent_digest,
            status="blocked",
            content_ledger_id=None,
            error=failure,
        )

    def _accept_artifact(
        self,
        plan: DeletionPlan,
        request: DeletionRequest,
        content: dict[str, object],
        key_digest: str,
        intent_digest: str,
        artifact_purge_available: bool,
    ) -> AcceptedDeletion:
        """Accept the purge of one pinned Artifact object version.

        The row is not moved yet: ``complete`` writes the same ``deleted`` record
        the retention path does only after the object version is confirmed gone,
        so a failed purge leaves the record ``queued`` instead of claiming a
        removal.
        """
        if not artifact_purge_available:
            raise DeletionObjectStorageUnavailableError(
                "artifact deletion requires the object-store identity"
            )
        object_key = content.get("object_key")
        version_id = content.get("storage_version_id")
        if not isinstance(object_key, str) or not isinstance(version_id, str):
            raise DeletionObjectStorageUnavailableError(
                "the pinned Artifact object identity is not readable"
            )
        return self._record(
            plan,
            request,
            key_digest,
            intent_digest,
            status="queued",
            content_ledger_id=None,
            pending_artifact=(object_key, version_id),
        )

    def _accept_voice_revision(
        self,
        plan: DeletionPlan,
        request: DeletionRequest,
        key_digest: str,
        intent_digest: str,
    ) -> AcceptedDeletion:
        """Delete one unreferenced draft revision in the accepting transaction.

        The row's content lives only in the database, so there is no object step
        and no queued state: ``accept`` already holds the draft row lock while it
        re-reads the dependencies, and the DELETE is the removal. Holding that
        lock through the delete is what keeps a concurrent admission from pinning
        the revision into an intent after the dependency snapshot.
        """
        deleted = self._tx.changed(
            "DELETE FROM speech_voice_draft_revisions WHERE draft_id = ? AND revision = ?",
            (plan.target.resource_id, str(plan.target.revision)),
        )
        if deleted != 1:
            raise DeletionPlanChangedError
        return self._record(
            plan,
            request,
            key_digest,
            intent_digest,
            status="succeeded",
            content_ledger_id=None,
        )

    def _accept_asset(
        self,
        plan: DeletionPlan,
        request: DeletionRequest,
        key_digest: str,
        intent_digest: str,
        purge_available: bool,
    ) -> AcceptedDeletion:
        if not purge_available:
            # The content is the object; without the object identity this route
            # cannot confirm a deletion and must not pretend it did.
            raise DeletionObjectStorageUnavailableError
        content_ledger_id = "cdl-" + uuid.uuid4().hex
        with self._tx.asset_cursor() as cursor:
            asset = cursor.asset(plan.target.resource_id)
            if asset is None:
                raise DeletionPlanChangedError
            ticket = prepare_content_deletion(
                cursor,
                asset,
                content_ledger_id,
                reason=CONFIRMED_REASON,
                require_expired=False,
            )
        if ticket is None:
            raise DeletionPlanChangedError
        if ticket.state == "blocked":
            # A live reference or an uncertain upload state now blocks the same
            # content the plan pinned; the caller resolves it and re-previews.
            raise DeletionDependencyConflictError
        if ticket.state == "complete" or ticket.object is None:
            # Nothing is left in the object store (already purged, or no object was
            # ever bound), so the content deletion can complete in this transaction.
            with self._tx.asset_cursor() as cursor:
                asset = cursor.asset(plan.target.resource_id)
                if asset is None or not complete_content_deletion(cursor, asset, ticket.ledger_id):
                    raise DeletionDependencyConflictError
            return self._record(
                plan,
                request,
                key_digest,
                intent_digest,
                status="succeeded",
                content_ledger_id=ticket.ledger_id,
            )
        return self._record(
            plan,
            request,
            key_digest,
            intent_digest,
            status="queued",
            content_ledger_id=ticket.ledger_id,
            pending=ticket.object,
        )

    def _accept_operation_content(
        self,
        plan: DeletionPlan,
        request: DeletionRequest,
        target_digest: str,
        key_digest: str,
        intent_digest: str,
    ) -> AcceptedDeletion:
        deletion_id = "dl-" + uuid.uuid4().hex
        job_id = plan.target.resource_id
        # The authoritative public intent is the row itself: deleting it removes
        # document and digest together, so retry cannot replay a body (#540).
        deleted = self._tx.changed(_DELETE_JOB_INTENT, (job_id, target_digest))
        if not deleted and self._tx.rows(
            "SELECT request_digest FROM speech_job_intents WHERE job_id = ?", (job_id,)
        ):
            # A row survived with another digest: the content is not the one this
            # plan pinned, so the plan no longer authorizes its removal.
            raise DeletionPlanChangedError
        # The legacy private command copy is tombstoned, not dropped: the
        # command-shape CHECK requires this exact quartet plus a version bump, and
        # it keeps the operation identity while removing the body.
        self._tx.rows(
            "UPDATE admission_operations SET request_document = NULL, "
            "command_redacted_at_us = GREATEST(recorded_at_us, ?), "
            "command_deletion_job_id = ?, command_deletion_evidence_digest = ?, "
            "version = version + 1 "
            "WHERE operation_id = ? AND request_document IS NOT NULL",
            (self._tx.now_us(), deletion_id, plan_digest(plan), job_id),
        )
        return self._record(
            plan,
            request,
            key_digest,
            intent_digest,
            status="succeeded",
            content_ledger_id=None,
            deletion_id=deletion_id,
        )

    # -- resume and completion ---------------------------------------------
    def _resume(self, existing: _LedgerRow) -> AcceptedDeletion:
        record = existing.record()
        if record.status in ("succeeded", "blocked", "failed"):
            # A blocked/failed row is this key's durable answer; resuming never
            # turns it into a removal or a different status.
            return AcceptedDeletion(record, None)
        if existing.row["target_kind"] == "artifact":
            return self._resume_artifact(existing, record)
        content_ledger_id = cast(str | None, existing.row["content_ledger_id"])
        if content_ledger_id is None:
            raise _unavailable()
        content = self._tx.rows(
            "SELECT deletion_state, storage_version_id FROM speech_content_deletions "
            "WHERE ledger_id = ?",
            (content_ledger_id,),
        )
        if not content:
            raise _unavailable()
        state = cast(str, content[0]["deletion_state"])
        if state == "complete":
            return AcceptedDeletion(self._finalize(existing, self._tx.now()), None)
        if state != "queued":
            raise DeletionDependencyConflictError
        proof = self._proof(cast(str, existing.row["target_id"]))
        # The accepted deletion pinned one object version. Confirming a different
        # live version would claim a purge the ledger cannot back up, so it stays
        # queued and the same key can be retried after the inconsistency is fixed.
        if proof is None or proof.version_id != content[0]["storage_version_id"]:
            raise _unavailable()
        claimed = self._tx.claim_purge(cast(str, existing.row["id"]))
        if claimed is None:
            # Another request owns this row's purge right now. The caller waits
            # for its committed result outside this transaction instead of
            # purging the pinned version a second time.
            return AcceptedDeletion(record, None)
        return AcceptedDeletion(_LedgerRow(claimed).record(), proof)

    def _resume_artifact(self, existing: _LedgerRow, record: DeletionRecord) -> AcceptedDeletion:
        plan_row = self._tx.plan_row(cast(str, existing.row["plan_id"]))
        if plan_row is None:
            raise _unavailable()
        content = cast(dict[str, Any], plan_row["content_snapshot"])
        object_key = content.get("object_key")
        version_id = content.get("storage_version_id")
        if not isinstance(object_key, str) or not isinstance(version_id, str):
            raise _unavailable()
        claimed = self._tx.claim_purge(cast(str, existing.row["id"]))
        if claimed is None:
            return AcceptedDeletion(record, None)
        return AcceptedDeletion(_LedgerRow(claimed).record(), None, (object_key, version_id))

    def complete(self, deletion_id: str, *, key_digest: str) -> DeletionRecord:
        existing = self._tx.ledger_by_id(deletion_id)
        if existing is None or existing.row["idempotency_key_digest"] != key_digest:
            raise _unavailable()
        record = existing.record()
        if record.status != "queued":
            return record
        if existing.row["target_kind"] == "artifact":
            return self._complete_artifact(existing)
        if not lock_workspace(self._tx, self._workspace_id):
            raise DeletionPlanChangedError
        with self._tx.asset_cursor() as cursor:
            asset = cursor.asset(cast(str, existing.row["target_id"]), lock=True)
            if asset is None:
                raise _unavailable()
            if not complete_content_deletion(
                cursor, asset, cast(str, existing.row["content_ledger_id"])
            ):
                # The pinned preconditions changed after the purge. Do not claim a
                # completion the ledger did not record; the row stays queued.
                return record
        return self._finalize(existing, self._tx.now())

    def _complete_artifact(self, existing: _LedgerRow) -> DeletionRecord:
        """Confirm the purge by writing the same `deleted` record retention writes.

        The pinned object identity and digest are rechecked in the UPDATE itself.
        A row whose identity moved, or that another path already deleted, is not
        claimed as this deletion's success: the ledger stays ``queued``.
        """
        plan_row = self._tx.plan_row(cast(str, existing.row["plan_id"]))
        if plan_row is None:
            raise _unavailable()
        content = cast(dict[str, Any], plan_row["content_snapshot"])
        updated = self._tx.changed(
            _TOMBSTONE_ARTIFACT,
            (
                self._tx.now_us(),
                cast(str, existing.row["target_id"]),
                self._workspace_id,
                cast(str, content.get("sha256")),
                cast(str, content.get("object_key")),
                cast(str, content.get("storage_version_id")),
            ),
        )
        if not updated:
            return existing.record()
        return self._finalize(existing, self._tx.now())

    def _finalize(self, existing: _LedgerRow, now: datetime) -> DeletionRecord:
        settled = self._tx.changed(_SETTLE_LEDGER, (now, now, existing.row["id"]))
        if settled:
            settled_row = self._tx.ledger_by_id(cast(str, existing.row["id"]))
            if settled_row is not None:
                return settled_row.record()
        current = self._tx.ledger_by_id(cast(str, existing.row["id"]))
        if current is None:
            raise _unavailable()
        return current.record()

    def _proof(self, asset_id: str) -> VerifiedObject | None:
        rows = self._tx.rows(
            "SELECT asset_id, object_key, storage_version_id, bytes, sha256, writer_id "
            "FROM speech_asset_objects WHERE asset_id = ?",
            (asset_id,),
        )
        if not rows:
            return None
        row = rows[0]
        return VerifiedObject(
            cast(str, row["asset_id"]),
            cast(str, row["object_key"]),
            cast(str, row["storage_version_id"]),
            _positive(row["bytes"]),
            cast(str, row["sha256"]),
            cast(str, row["writer_id"]),
        )

    # -- the ledger row ------------------------------------------------------
    def _record(
        self,
        plan: DeletionPlan,
        request: DeletionRequest,
        key_digest: str,
        intent_digest: str,
        *,
        status: str,
        content_ledger_id: str | None,
        pending: VerifiedObject | None = None,
        pending_artifact: tuple[str, str] | None = None,
        deletion_id: str | None = None,
        error: DeletionFailure | None = None,
    ) -> AcceptedDeletion:
        now = self._tx.now()
        ledger_id = deletion_id or ("dl-" + uuid.uuid4().hex)
        completed_at = now if status == "succeeded" else None
        # 列与占位符写死;列顺序由 test_deletion_ledger_insert_matches_the_schema_constant
        # 对着 DELETION_LEDGER_INSERT_COLUMNS 与 DDL 核对。
        self._tx.rows(
            _INSERT_LEDGER,
            (
                ledger_id,
                self._workspace_id,
                plan.id,
                str(plan.revision),
                plan_digest(plan),
                plan.target.owner,
                plan.target.kind,
                plan.target.resource_id,
                str(plan.target.revision),
                key_digest,
                intent_digest,
                Jsonb(_scope(confirmed_targets(plan))),
                Jsonb(_scope(retained_independent_copies(plan))),
                Jsonb(failure_payload(error)) if error is not None else None,
                status,
                "complete" if status == "succeeded" else "pending",
                content_ledger_id,
                now,
                now,
                completed_at,
            ),
        )
        stored = self._tx.ledger_by_key(key_digest)
        if stored is None:
            raise _unavailable()
        if pending is not None or pending_artifact is not None:
            claimed = self._tx.claim_purge(cast(str, stored.row["id"]))
            if claimed is None:
                # Another request already owns this row's purge; the caller must
                # wait for it rather than purging the same version again.
                return AcceptedDeletion(stored.record(), None)
            stored = _LedgerRow(claimed)
        return AcceptedDeletion(stored.record(), pending, pending_artifact)


def _positive(value: Any) -> int:
    if type(value) is not int or value < 1:
        raise _unavailable()
    return value


class PostgresDeletionExecutions:
    """Bounded accept/complete transactions over the public Job writer identity."""

    def __init__(self, pool: SpeechPool, *, timeout_seconds: float = 3) -> None:
        if type(timeout_seconds) not in (int, float) or not 0 < timeout_seconds <= 30:
            raise ValueError("Deletion transactions require bounded deadlines")
        self._pool = pool
        self._timeout = timeout_seconds

    def missing_privileges(self) -> tuple[str, ...]:
        with self._pool.connection(timeout=self._timeout) as connection:
            transaction = _Transaction(
                connection, time.monotonic() + self._timeout, workspace_id=""
            )
            return transaction.missing_privileges()

    def read(self, deletion_id: str, *, workspace_id: str) -> DeletionRecord | None:
        """Project one ledger row under a read-only transaction.

        The transaction is declared ``READ ONLY`` on purpose: the read may not
        claim a purge, advance a status or write a row, so a future edit that
        resumes the execution fails at the database instead of becoming a second
        executor. ``None`` is both "no such deletion" and "another workspace's
        deletion"; the caller turns it into the same public 404.
        """
        try:
            with (
                self._pool.connection(timeout=self._timeout) as connection,
                connection.transaction(),
            ):
                transaction = _Transaction(
                    connection, time.monotonic() + self._timeout, workspace_id
                )
                transaction.rows("SET TRANSACTION READ ONLY")
                self._require_privileges(transaction, _READ_PRIVILEGES)
                rows = transaction.rows(
                    _SELECT_LEDGER_BY_ID,
                    (workspace_id, deletion_id),
                )
                if not rows:
                    return None
                try:
                    return _LedgerRow(rows[0]).record()
                except KeyError, TypeError, ValueError:
                    # A ledger row this projection cannot state is not a 500: it
                    # is storage this deployment cannot answer from.
                    raise _unavailable() from None
        except DeletionError:
            raise
        except psycopg.Error, PoolClosed, PoolTimeout, TooManyRequests:
            raise _unavailable() from None

    def accept(
        self,
        request: DeletionRequest,
        *,
        key_digest: str,
        intent_digest: str,
        purge_available: bool,
        artifact_purge_available: bool,
        request_id: str,
        workspace_id: str,
    ) -> AcceptedDeletion:
        try:
            with (
                self._pool.connection(timeout=self._timeout) as connection,
                connection.transaction(),
            ):
                transaction = _Transaction(
                    connection, time.monotonic() + self._timeout, workspace_id
                )
                self._require_privileges(transaction)
                return _DeletionExecution(transaction, workspace_id).accept(
                    request,
                    key_digest=key_digest,
                    intent_digest=intent_digest,
                    purge_available=purge_available,
                    artifact_purge_available=artifact_purge_available,
                    request_id=request_id,
                )
        except psycopg.errors.ForeignKeyViolation:
            # A reference was committed after the recheck: the delete itself is
            # what the database refuses. This is an explicit dependency conflict,
            # never an unknown outcome or a 500.
            raise DeletionDependencyConflictError(
                "a live dependency appeared while deleting"
            ) from None
        except psycopg.errors.UniqueViolation:
            return self._replay_after_race(
                request,
                key_digest=key_digest,
                intent_digest=intent_digest,
                workspace_id=workspace_id,
            )
        except DeletionError:
            raise
        except psycopg.Error, PoolClosed, PoolTimeout, TooManyRequests:
            raise _unavailable() from None

    def complete(self, deletion_id: str, *, key_digest: str, workspace_id: str) -> DeletionRecord:
        try:
            with (
                self._pool.connection(timeout=self._timeout) as connection,
                connection.transaction(),
            ):
                transaction = _Transaction(
                    connection, time.monotonic() + self._timeout, workspace_id
                )
                self._require_privileges(transaction)
                return _DeletionExecution(transaction, workspace_id).complete(
                    deletion_id, key_digest=key_digest
                )
        except DeletionError:
            raise
        except psycopg.Error, PoolClosed, PoolTimeout, TooManyRequests:
            raise _unavailable() from None

    def release_claim(
        self, deletion_id: str, *, key_digest: str, workspace_id: str
    ) -> DeletionRecord:
        """Hand the purge claim back after an attempt ended without completing.

        The row stays ``queued``; releasing the claim is what lets the same key
        retry at once instead of waiting for the crash-recovery lease.
        """
        try:
            with (
                self._pool.connection(timeout=self._timeout) as connection,
                connection.transaction(),
            ):
                transaction = _Transaction(
                    connection, time.monotonic() + self._timeout, workspace_id
                )
                self._require_privileges(transaction)
                existing = transaction.ledger_by_id(deletion_id)
                if existing is None or existing.row["idempotency_key_digest"] != key_digest:
                    raise _unavailable()
                released = transaction.release_purge(deletion_id)
                return existing.record() if released is None else _LedgerRow(released).record()
        except DeletionError:
            raise
        except psycopg.Error, PoolClosed, PoolTimeout, TooManyRequests:
            raise _unavailable() from None

    def await_completion(
        self, deletion_id: str, *, key_digest: str, workspace_id: str
    ) -> DeletionRecord:
        """Wait for the executor that owns the claim, then report its result.

        A same-key request that lost the purge claim must answer the *same*
        deletion, not a conflict and not a second purge. If the owner releases or
        its claim lease expires first, the queued record is returned so the caller
        can resume; a wait past the bounded deadline is a retryable 503.
        """
        deadline = time.monotonic() + min(self._timeout, 5.0)
        while True:
            try:
                with (
                    self._pool.connection(timeout=self._timeout) as connection,
                    connection.transaction(),
                ):
                    transaction = _Transaction(
                        connection, time.monotonic() + self._timeout, workspace_id
                    )
                    rows = transaction.rows(
                        _SELECT_LEDGER_CLAIM_STATE,
                        (transaction.now(), PURGE_CLAIM_LEASE_SECONDS, deletion_id),
                    )
                    if not rows or rows[0]["idempotency_key_digest"] != key_digest:
                        raise _unavailable()
                    record = _LedgerRow(rows[0]).record()
                    if record.status == "succeeded" or not rows[0]["claim_live"]:
                        return record
            except DeletionError:
                raise
            except psycopg.Error, PoolClosed, PoolTimeout, TooManyRequests:
                raise _unavailable() from None
            if time.monotonic() >= deadline:
                raise _unavailable()
            time.sleep(0.02)

    def _require_privileges(
        self,
        transaction: _Transaction,
        probes: tuple[tuple[str, str, str | None], ...] = _PRIVILEGES,
    ) -> None:
        missing = transaction.missing_privileges(probes)
        if missing:
            raise DeletionLedgerConfigurationError("deletion execution lacks " + ", ".join(missing))

    def _replay_after_race(
        self,
        request: DeletionRequest,
        *,
        key_digest: str,
        intent_digest: str,
        workspace_id: str,
    ) -> AcceptedDeletion:
        """A concurrent writer committed the same key or plan first.

        The rollback already discarded this transaction's work. Read the winner:
        the same key converges on its record (the caller may still purge, which is
        idempotent), while a *different* key on an already executed plan is a
        single-use credential that was spent and therefore a plan change.
        """
        try:
            with (
                self._pool.connection(timeout=self._timeout) as connection,
                connection.transaction(),
            ):
                transaction = _Transaction(
                    connection, time.monotonic() + self._timeout, workspace_id
                )
                execution = _DeletionExecution(transaction, workspace_id)
                existing = transaction.ledger_by_key(key_digest)
                if existing is not None:
                    if existing.row["intent_digest"] != intent_digest:
                        raise DeletionIdempotencyConflictError
                    return execution._resume(existing)
                if transaction.ledger_for_plan(request.plan_id) is not None:
                    raise DeletionPlanChangedError
                raise _unavailable()
        except DeletionError:
            raise
        except psycopg.Error, PoolClosed, PoolTimeout, TooManyRequests:
            raise _unavailable() from None


__all__ = ["PostgresDeletionExecutions"]
