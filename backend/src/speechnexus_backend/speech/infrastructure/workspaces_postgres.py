"""Bounded reads of the frozen 0003 workspace authority, plus accepted writes.

Reads stay one read-only transaction per query. Writes run one transaction per
accepted command: a same-key replay is resolved before any mutation, then the
metadata revision compare-and-set decides whether the change still applies.
A duplicate at commit time is re-read in a *fresh* transaction because the
refused transaction is already aborted; the original wins if it exists.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any, cast

from speechnexus_backend.speech.domain.assets import identifier
from speechnexus_backend.speech.domain.workspaces import (
    Workspace,
    WorkspaceConflictError,
    WorkspaceCreateCommand,
    WorkspaceNotFoundError,
    WorkspaceQuery,
    WorkspaceUnavailableError,
    WorkspaceUpdateCommand,
    WorkspaceWriteReceipt,
)
from speechnexus_backend.speech.infrastructure.postgres_pool import SpeechPool
from speechnexus_portable_sql.clock import ClockReader, now_us
from speechnexus_portable_sql.transaction import is_savepoint_control
from speechnexus_portable_sql.vendor import PoolClosed, PoolTimeout, TooManyRequests, psycopg

_COLUMNS = (
    "workspace_id, display_name, workspace_state, metadata_revision, policy_revision, created_at_us"
)
_COMMAND_COLUMNS = (
    "key_digest, operation, workspace_id, intent_digest, result_metadata_revision, request_id, "
    "recorded_at_us"
)


class PostgresWorkspaceReads:
    def __init__(self, pool: SpeechPool, *, timeout_seconds: float = 3) -> None:
        if isinstance(timeout_seconds, bool) or not 0 < timeout_seconds <= 30:
            raise ValueError("Workspace reads require bounded deadlines")
        self._pool, self._timeout = pool, timeout_seconds

    def _read(
        self, query: str, parameters: tuple[object, ...], *, page_size: int | None = None
    ) -> tuple[Workspace, ...]:
        deadline = time.monotonic() + self._timeout
        try:
            with self._pool.connection(timeout=self._timeout) as connection:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise WorkspaceUnavailableError
                connection.execute("SET TRANSACTION READ ONLY")
                connection.execute(
                    "SELECT set_config('statement_timeout', ?, true)",
                    (str(max(1, int(remaining * 1000))),),
                )
                cursor = connection.execute(query, parameters)
                # 受限方言没有 `LIMIT`:keyset 谓词已把扫描面收窄,页大小由 fetchmany 决定。
                records = cursor.fetchall() if page_size is None else cursor.fetchmany(page_size)
                result = tuple(_workspace(row) for row in records)
                if time.monotonic() >= deadline:
                    raise WorkspaceUnavailableError
            return result
        except (
            psycopg.Error,
            PoolClosed,
            PoolTimeout,
            TooManyRequests,
            ValueError,
            TypeError,
            KeyError,
        ):
            raise WorkspaceUnavailableError from None

    def get(self, workspace_id: str) -> Workspace | None:
        identifier(workspace_id)
        rows = self._read(
            "SELECT " + _COLUMNS + " FROM speech_workspaces WHERE workspace_id = ?",
            (workspace_id,),
        )
        return rows[0] if rows else None

    def scan(self, query: WorkspaceQuery) -> tuple[Workspace, ...]:
        return self._read(
            "SELECT " + _COLUMNS + " FROM speech_workspaces "
            "WHERE workspace_id > ? ORDER BY workspace_id",
            (query.after_id or "",),
            page_size=query.limit + 1,
        )


class _Cursor:
    def __init__(self, cursor: psycopg.Cursor[dict[str, Any]], deadline: float) -> None:
        self.raw, self.deadline = cursor, deadline

    def rows(self, query: str, parameters: tuple[object, ...] = ()) -> list[dict[str, Any]]:
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise WorkspaceUnavailableError
        if is_savepoint_control(query):
            self.raw.execute(query, parameters)
            return []
        self.raw.execute(
            "SELECT set_config('statement_timeout', ?, true)",
            (str(max(1, int(remaining * 1000))),),
        )
        self.raw.execute(query, parameters)
        return self.raw.fetchall() if self.raw.description else []

    def execute(self, sql: str, parameters: tuple[object, ...] = ()) -> Any:
        """便携时钟的执行入口:与 `rows` 共用同一预算,把游标交回去取行."""

        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise WorkspaceUnavailableError
        if is_savepoint_control(sql):
            return self.raw.execute(sql, parameters)
        self.raw.execute(
            "SELECT set_config('statement_timeout', ?, true)",
            (str(max(1, int(remaining * 1000))),),
        )
        return self.raw.execute(sql, parameters)

    def clock(self) -> int:
        """「现在」只从便携时钟读一次;语句里不再有品牌函数."""

        try:
            return now_us(cast(ClockReader, self))
        except (LookupError, TypeError) as error:
            raise WorkspaceUnavailableError from error


class PostgresWorkspaceWrites:
    """One transaction per command; replay identity is resolved before mutation.

    The optional idempotency key is scoped by this store's schema, the operation
    and the target: the key digest is the ledger primary key, and the recorded
    intent digest identifies the body. A same-key retry of the same intent
    returns the current record for the recorded identity; a different body,
    operation or target is a conflict rather than a second write.
    """

    def __init__(self, pool: SpeechPool, *, timeout_seconds: float = 3) -> None:
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not 0 < timeout_seconds <= 30
        ):
            raise ValueError("Workspace writes require bounded deadlines")
        self._pool, self._timeout = pool, float(timeout_seconds)

    def create(
        self, command: WorkspaceCreateCommand, *, workspace_id: str
    ) -> WorkspaceWriteReceipt:
        identifier(workspace_id)
        try:
            return self._run(lambda cursor: self._create(cursor, command, workspace_id))
        except psycopg.errors.UniqueViolation:
            return self._collision(command, "create", workspace_id=None)

    def update(self, command: WorkspaceUpdateCommand) -> WorkspaceWriteReceipt:
        try:
            return self._run(lambda cursor: self._update(cursor, command))
        except psycopg.errors.UniqueViolation:
            return self._collision(command, "update", workspace_id=command.workspace_id)

    def _run[T](self, operation: Callable[[_Cursor], T]) -> T:
        deadline = time.monotonic() + self._timeout
        try:
            with (
                self._pool.connection(timeout=self._timeout) as connection,
                connection.cursor() as raw,
            ):
                result = operation(_Cursor(raw, deadline))
                if time.monotonic() >= deadline:
                    raise WorkspaceUnavailableError
            # Report nothing until the context manager has committed.
            return result
        except WorkspaceConflictError, WorkspaceNotFoundError, WorkspaceUnavailableError:
            raise
        except psycopg.errors.UniqueViolation:
            raise
        except (
            psycopg.Error,
            PoolClosed,
            PoolTimeout,
            TooManyRequests,
            ValueError,
            TypeError,
            KeyError,
            RecursionError,
            OverflowError,
        ):
            raise WorkspaceUnavailableError from None

    def _collision(
        self,
        command: WorkspaceCreateCommand | WorkspaceUpdateCommand,
        operation: str,
        *,
        workspace_id: str | None,
    ) -> WorkspaceWriteReceipt:
        """Resolve a refused duplicate without guessing which constraint fired.

        The command key refuses a concurrent identical retry; the workspace
        primary key refuses an allocated identity that is already taken. The
        recorded original, when present, decides; otherwise it was the identity.
        """
        deadline = time.monotonic() + self._timeout
        try:
            with (
                self._pool.connection(timeout=self._timeout) as connection,
                connection.cursor() as raw,
            ):
                cursor = _Cursor(raw, deadline)
                original = self._original(cursor, command.key_digest)
                if original is None:
                    if operation != "create":
                        # An update has no identity to collide on; a duplicate
                        # it cannot explain is not a client-visible conflict.
                        raise WorkspaceUnavailableError
                    raise WorkspaceConflictError("workspace_identity_conflict")
                return self._replayed(cursor, original, command, operation, workspace_id)
        except WorkspaceConflictError, WorkspaceNotFoundError, WorkspaceUnavailableError:
            raise
        except (
            psycopg.Error,
            PoolClosed,
            PoolTimeout,
            TooManyRequests,
            ValueError,
            TypeError,
            KeyError,
            RecursionError,
            OverflowError,
        ):
            raise WorkspaceUnavailableError from None

    def _original(self, cursor: _Cursor, key_digest: str | None) -> dict[str, Any] | None:
        if key_digest is None:
            return None
        # The lock bucket only orders equal keys; the complete digest is the PK,
        # so a bucket collision adds contention and can never alias a replay.
        cursor.rows(
            "SELECT pg_advisory_xact_lock(hashtextextended("
            "'speechnexus:workspace:' || current_schema() || ':idempotency:' || ?, 0))",
            (key_digest,),
        )
        rows = cursor.rows(
            "SELECT " + _COMMAND_COLUMNS + " FROM speech_workspace_commands WHERE key_digest = ?",
            (key_digest,),
        )
        if len(rows) > 1:
            raise WorkspaceUnavailableError
        return rows[0] if rows else None

    def _replayed(
        self,
        cursor: _Cursor,
        original: dict[str, Any],
        command: WorkspaceCreateCommand | WorkspaceUpdateCommand,
        operation: str,
        workspace_id: str | None,
    ) -> WorkspaceWriteReceipt:
        if (
            original["operation"] != operation
            or original["intent_digest"] != command.intent_digest
            or (workspace_id is not None and original["workspace_id"] != workspace_id)
        ):
            raise WorkspaceConflictError("idempotency_conflict")
        record = self._record(cursor, original["workspace_id"])
        if record is None or record.metadata_revision < int(original["result_metadata_revision"]):
            # The accepted result is no longer present or a revision went
            # backwards. Never recreate the group or report an unproven state.
            raise WorkspaceUnavailableError
        return WorkspaceWriteReceipt(record, True)

    def _record(self, cursor: _Cursor, workspace_id: str) -> Workspace | None:
        rows = cursor.rows(
            "SELECT " + _COLUMNS + " FROM speech_workspaces WHERE workspace_id = ?",
            (workspace_id,),
        )
        return _workspace(rows[0]) if rows else None

    def _create(
        self, cursor: _Cursor, command: WorkspaceCreateCommand, workspace_id: str
    ) -> WorkspaceWriteReceipt:
        original = self._original(cursor, command.key_digest)
        if original is not None:
            return self._replayed(cursor, original, command, "create", None)
        now = cursor.clock()
        cursor.rows(
            "INSERT INTO speech_workspaces "
            "(workspace_id, display_name, workspace_state, created_at_us) "
            "VALUES (?, ?, 'active', ?)",
            (workspace_id, command.display_name, now),
        )
        # 方言没有 `RETURNING`:写在同一事务里,就地按主键读回同一行。
        record = self._read_back(cursor, workspace_id)
        self._record_command(cursor, command, "create", record, now)
        return WorkspaceWriteReceipt(record, False)

    def _update(self, cursor: _Cursor, command: WorkspaceUpdateCommand) -> WorkspaceWriteReceipt:
        current = self._locked(cursor, command.workspace_id)
        if current is None:
            raise WorkspaceNotFoundError
        original = self._original(cursor, command.key_digest)
        if original is not None:
            return self._replayed(cursor, original, command, "update", command.workspace_id)
        if current.metadata_revision != command.expected_metadata_revision:
            raise WorkspaceConflictError("configuration_changed")
        now = cursor.clock()
        cursor.rows(
            "UPDATE speech_workspaces SET "
            "display_name = COALESCE(?, display_name), "
            "workspace_state = COALESCE(?, workspace_state), "
            "metadata_revision = metadata_revision + 1 "
            "WHERE workspace_id = ? AND metadata_revision = ?",
            (
                command.display_name,
                command.status,
                command.workspace_id,
                command.expected_metadata_revision,
            ),
        )
        # 条件 UPDATE 命中与否由读回的同键行与版本号判定,不依赖 `RETURNING` 的行数。
        record = self._read_back(cursor, command.workspace_id)
        self._record_command(cursor, command, "update", record, now)
        return WorkspaceWriteReceipt(record, False)

    def _read_back(self, cursor: _Cursor, workspace_id: str) -> Workspace:
        """Write-back read: the row just written, read by primary key in the same transaction."""

        rows = cursor.rows(
            "SELECT " + _COLUMNS + " FROM speech_workspaces WHERE workspace_id = ?",
            (workspace_id,),
        )
        if len(rows) != 1:
            # 写成功却读不到一行,说明身份或权限出了协调问题,不是业务冲突。
            raise WorkspaceUnavailableError
        return _workspace(rows[0])

    def _locked(self, cursor: _Cursor, workspace_id: str) -> Workspace | None:
        rows = cursor.rows(
            "SELECT " + _COLUMNS + " FROM speech_workspaces WHERE workspace_id = ? FOR UPDATE",
            (workspace_id,),
        )
        return _workspace(rows[0]) if rows else None

    def _record_command(
        self,
        cursor: _Cursor,
        command: WorkspaceCreateCommand | WorkspaceUpdateCommand,
        operation: str,
        record: Workspace,
        now: int,
    ) -> None:
        if command.key_digest is None:
            return
        cursor.rows(
            "INSERT INTO speech_workspace_commands "
            "(" + _COMMAND_COLUMNS + ") VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                command.key_digest,
                operation,
                record.workspace_id,
                command.intent_digest,
                record.metadata_revision,
                command.request_id,
                now,
            ),
        )


def _workspace(row: dict[str, Any]) -> Workspace:
    return Workspace(
        row["workspace_id"],
        row["display_name"],
        row["workspace_state"],
        row["metadata_revision"],
        row["policy_revision"],
        row["created_at_us"],
    )
