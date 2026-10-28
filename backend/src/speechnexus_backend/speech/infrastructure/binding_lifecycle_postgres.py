"""Atomic binding retirement, activation and append-only lifecycle evidence."""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import Callable
from typing import Any, Literal, cast

import psycopg
from psycopg_pool import PoolClosed, PoolTimeout, TooManyRequests

from speechnexus_backend.speech.domain.binding_lifecycle import (
    BindingActivationCommand,
    BindingLifecycleConflictError,
    BindingLifecycleNotFoundError,
    BindingLifecycleReceipt,
    BindingLifecycleStatus,
    BindingLifecycleUnavailableError,
    BindingRetirementCommand,
)
from speechnexus_backend.speech.infrastructure.catalog_revision_schema import RETIREMENT_TABLE
from speechnexus_backend.speech.infrastructure.postgres_pool import SpeechPool
from speechnexus_backend.speech.infrastructure.publication_lifecycle_schema import (
    EVENT_TABLE,
    HEAD_TABLE,
)

type LifecycleOperation = Literal["retire", "disable", "activate"]


class _Cursor:
    def __init__(self, cursor: psycopg.Cursor[dict[str, Any]], deadline: float) -> None:
        self.raw, self.deadline = cursor, deadline

    @property
    def rowcount(self) -> int:
        return self.raw.rowcount

    def execute(self, query: str, parameters: tuple[object, ...] = ()) -> object:
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise BindingLifecycleUnavailableError
        self.raw.execute(
            "SELECT set_config('statement_timeout', %s, true)",
            (str(max(1, int(remaining * 1000))),),
        )
        return self.raw.execute(query, parameters)

    def fetchall(self) -> list[dict[str, Any]]:
        return self.raw.fetchall()


class PostgresBindingLifecycle:
    """One transaction per command; replay is resolved before mutation."""

    def __init__(self, pool: SpeechPool, *, timeout_seconds: float = 3) -> None:
        if type(timeout_seconds) not in (int, float) or not 0 < timeout_seconds <= 30:
            raise ValueError("Binding lifecycle transactions require bounded deadlines")
        self._pool, self._timeout = pool, timeout_seconds

    def retire(self, command: BindingRetirementCommand) -> BindingLifecycleReceipt:
        return self._run(
            lambda cursor: self._retire(cursor, command),
            preserve=(BindingLifecycleConflictError, BindingLifecycleNotFoundError),
        )

    def activate(self, command: BindingActivationCommand) -> BindingLifecycleReceipt:
        return self._run(
            lambda cursor: self._activate(cursor, command),
            preserve=(BindingLifecycleConflictError, BindingLifecycleNotFoundError),
        )

    def _run[T](
        self,
        operation: Callable[[_Cursor], T],
        *,
        preserve: tuple[type[BaseException], ...],
    ) -> T:
        deadline = time.monotonic() + self._timeout
        try:
            with (
                self._pool.connection(timeout=self._timeout) as connection,
                connection.cursor() as raw,
            ):
                result = operation(_Cursor(raw, deadline))
                if time.monotonic() >= deadline:
                    raise BindingLifecycleUnavailableError
            if time.monotonic() >= deadline:
                raise BindingLifecycleUnavailableError
            return result
        except preserve:
            raise
        except (
            psycopg.Error,
            PoolClosed,
            PoolTimeout,
            TooManyRequests,
            BindingLifecycleUnavailableError,
            ValueError,
            TypeError,
            KeyError,
            RecursionError,
            OverflowError,
        ):
            raise BindingLifecycleUnavailableError from None

    def _retire(
        self, cursor: _Cursor, command: BindingRetirementCommand
    ) -> BindingLifecycleReceipt:
        self._lock(cursor, command.binding_id)
        replay = self._replay(cursor, command, "retire" if command.mode == "retire" else "disable")
        if replay is not None:
            return replay
        state = self._state(cursor, command.binding_id, command.revision)
        if state is None:
            raise BindingLifecycleNotFoundError
        if state["retired_at_us"] is not None:
            raise BindingLifecycleConflictError("revision_already_retired")
        head = self._head(cursor, command.binding_id)
        if (
            head is None
            or head["revision"] != command.revision
            or head["revision_id"] != state["revision_id"]
        ):
            raise BindingLifecycleConflictError("revision_not_current")
        occurred_at_us = self._clock(cursor)
        cursor.execute(
            f"INSERT INTO {RETIREMENT_TABLE} "
            "(kind, object_id, revision, revision_id, retired_at_us, mode, reason) "
            "VALUES ('binding', %s, %s, %s, %s, %s, %s)",
            (
                command.binding_id,
                command.revision,
                state["revision_id"],
                occurred_at_us,
                command.mode,
                command.reason,
            ),
        )
        if cursor.rowcount != 1:
            raise BindingLifecycleUnavailableError
        cursor.execute(
            f"DELETE FROM {HEAD_TABLE} WHERE kind='binding' AND object_id=%s "
            "AND revision=%s AND revision_id=%s",
            (command.binding_id, command.revision, state["revision_id"]),
        )
        if cursor.rowcount != 1:
            raise BindingLifecycleUnavailableError
        policy_revision = self._catalog_revision(cursor)
        status = "retired" if command.mode == "retire" else "disabled"
        disabled_reason = command.reason if command.mode == "disable" else None
        self._record(
            cursor,
            command,
            action="model_binding." + command.mode,
            result_status=status,
            retired_at_us=occurred_at_us,
            disabled_reason=disabled_reason,
            occurred_at_us=occurred_at_us,
            policy_revision=policy_revision,
            change_summary={
                "changed_fields": ["/publication/status", "/publication/retired_at"],
                "reason": command.reason,
            },
        )
        return self._receipt(
            state,
            status=status,
            retired_at_us=occurred_at_us,
            disabled_reason=disabled_reason,
            replayed=False,
        )

    def _activate(
        self, cursor: _Cursor, command: BindingActivationCommand
    ) -> BindingLifecycleReceipt:
        self._lock(cursor, command.binding_id)
        replay = self._replay(cursor, command, "activate")
        if replay is not None:
            return replay
        state = self._state(cursor, command.binding_id, command.revision)
        if state is None or state["published_at_us"] is None:
            raise BindingLifecycleNotFoundError
        if state["retired_at_us"] is not None:
            raise BindingLifecycleConflictError("revision_not_published")
        head = self._head(cursor, command.binding_id)
        current_revision = None if head is None else head["revision"]
        if current_revision != command.expected_current_revision:
            raise BindingLifecycleConflictError("configuration_changed")
        occurred_at_us = self._clock(cursor)
        changed = (
            head is None
            or head["revision"] != command.revision
            or head["revision_id"] != state["revision_id"]
        )
        if changed:
            cursor.execute(
                f"INSERT INTO {HEAD_TABLE} "
                "(kind, object_id, revision, revision_id, updated_at_us) "
                "VALUES ('binding', %s, %s, %s, %s) "
                "ON CONFLICT (kind, object_id) DO UPDATE SET "
                "revision=EXCLUDED.revision, revision_id=EXCLUDED.revision_id, "
                "updated_at_us=EXCLUDED.updated_at_us",
                (command.binding_id, command.revision, state["revision_id"], occurred_at_us),
            )
            self._advance_catalog(cursor, occurred_at_us)
        policy_revision = self._catalog_revision(cursor)
        self._record(
            cursor,
            command,
            action="model_binding.activate",
            result_status="published",
            retired_at_us=None,
            disabled_reason=None,
            occurred_at_us=occurred_at_us,
            policy_revision=policy_revision,
            change_summary={
                "changed_fields": ["/publication/head_revision"],
                "reason": None,
            },
        )
        return self._receipt(
            state,
            status="published",
            retired_at_us=None,
            disabled_reason=None,
            replayed=False,
        )

    def _replay(
        self,
        cursor: _Cursor,
        command: BindingRetirementCommand | BindingActivationCommand,
        operation: LifecycleOperation,
    ) -> BindingLifecycleReceipt | None:
        if command.key_digest is None:
            return None
        cursor.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended("
            "'speechnexus:catalog:' || current_schema() || ':idempotency:' || %s, 0))",
            (command.key_digest,),
        )
        cursor.execute(
            "SELECT * FROM " + EVENT_TABLE + " WHERE idempotency_key_digest=%s",
            (command.key_digest,),
        )
        rows = cursor.fetchall()
        if not rows:
            return None
        if len(rows) != 1:
            raise BindingLifecycleUnavailableError
        event = rows[0]
        expected_action = "model_binding." + operation
        if (
            event["intent_digest"] != command.intent_digest
            or event["action"] != expected_action
            or event["binding_id"] != command.binding_id
            or event["revision"] != command.revision
        ):
            raise BindingLifecycleConflictError("idempotency_conflict")
        state = self._state(cursor, command.binding_id, command.revision)
        if state is None or state["revision_id"] != event["revision_id"]:
            raise BindingLifecycleUnavailableError
        return self._receipt(
            state,
            status=event["result_status"],
            retired_at_us=event["retired_at_us"],
            disabled_reason=event["disabled_reason"],
            replayed=True,
        )

    def _record(
        self,
        cursor: _Cursor,
        command: BindingRetirementCommand | BindingActivationCommand,
        *,
        action: str,
        result_status: str,
        retired_at_us: int | None,
        disabled_reason: str | None,
        occurred_at_us: int,
        policy_revision: int,
        change_summary: dict[str, object],
    ) -> None:
        state = self._state(cursor, command.binding_id, command.revision)
        if state is None:
            raise BindingLifecycleUnavailableError
        cursor.execute(
            f"INSERT INTO {EVENT_TABLE} "
            "(event_id, kind, action, binding_id, revision, revision_id, result_status, "
            " retired_at_us, disabled_reason, request_id, actor_type, credential_label, "
            " occurred_at_us, policy_revision, intent_digest, idempotency_key_digest, "
            " change_summary_json) "
            "VALUES (%s, 'binding', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, "
            " %s::jsonb)",
            (
                "lifecycle-" + uuid.uuid4().hex,
                action,
                command.binding_id,
                command.revision,
                state["revision_id"],
                result_status,
                retired_at_us,
                disabled_reason,
                command.request_id,
                command.actor_type,
                command.credential_label,
                occurred_at_us,
                policy_revision,
                command.intent_digest,
                command.key_digest,
                json.dumps(change_summary, sort_keys=True, separators=(",", ":")),
            ),
        )

    def _state(self, cursor: _Cursor, binding_id: str, revision: str) -> dict[str, Any] | None:
        cursor.execute(
            "SELECT p.kind, p.object_id AS binding_id, p.revision, p.revision_id, "
            "p.published_at_us, "
            "r.definition_json, r.created_at_us, "
            "e.capacity_manifest_id, e.qualification_report_id, "
            "ret.retired_at_us, ret.mode, ret.reason "
            "FROM speech_publications AS p "
            "JOIN speech_binding_revisions AS r "
            "  ON r.binding_id=p.object_id AND r.revision=p.revision "
            " AND r.revision_id=p.revision_id "
            "JOIN speech_binding_publication_evidence AS e "
            "  ON e.kind=p.kind AND e.object_id=p.object_id AND e.revision=p.revision "
            " AND e.revision_id=p.revision_id "
            f"LEFT JOIN {RETIREMENT_TABLE} AS ret "
            "  ON ret.kind=p.kind AND ret.object_id=p.object_id AND ret.revision=p.revision "
            " AND ret.revision_id=p.revision_id "
            "WHERE p.kind='binding' AND p.object_id=%s AND p.revision=%s",
            (binding_id, revision),
        )
        rows = cursor.fetchall()
        if not rows:
            return None
        if len(rows) != 1:
            raise BindingLifecycleUnavailableError
        return rows[0]

    def _head(self, cursor: _Cursor, binding_id: str) -> dict[str, Any] | None:
        cursor.execute(
            "SELECT revision, revision_id FROM " + HEAD_TABLE + " "
            "WHERE kind='binding' AND object_id=%s",
            (binding_id,),
        )
        rows = cursor.fetchall()
        if not rows:
            return None
        if len(rows) != 1:
            raise BindingLifecycleUnavailableError
        return rows[0]

    def _lock(self, cursor: _Cursor, binding_id: str) -> None:
        cursor.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended("
            "'speechnexus:catalog:' || current_schema() || ':binding:' || %s, 0))",
            (binding_id,),
        )

    def _clock(self, cursor: _Cursor) -> int:
        cursor.execute(
            "SELECT CAST(EXTRACT(EPOCH FROM clock_timestamp())*1000000 AS BIGINT) AS t"
        )
        rows = cursor.fetchall()
        if len(rows) != 1:
            raise BindingLifecycleUnavailableError
        return int(rows[0]["t"])

    def _catalog_revision(self, cursor: _Cursor) -> int:
        cursor.execute("SELECT revision FROM speech_catalog_state WHERE singleton IS TRUE")
        rows = cursor.fetchall()
        if len(rows) != 1:
            raise BindingLifecycleUnavailableError
        return int(rows[0]["revision"])

    def _advance_catalog(self, cursor: _Cursor, changed_at_us: int) -> None:
        cursor.execute(
            "UPDATE speech_catalog_state SET revision=revision+1, "
            "generated_at_us=GREATEST(generated_at_us, %s)",
            (changed_at_us,),
        )
        if cursor.rowcount != 1:
            raise BindingLifecycleUnavailableError

    def _receipt(
        self,
        state: dict[str, Any],
        *,
        status: str,
        retired_at_us: int | None,
        disabled_reason: str | None,
        replayed: bool,
    ) -> BindingLifecycleReceipt:
        return BindingLifecycleReceipt(
            binding_id=str(state["binding_id"]),
            revision=str(state["revision"]),
            revision_id=str(state["revision_id"]),
            definition=str(state["definition_json"]).encode("utf-8"),
            created_at_us=int(state["created_at_us"]),
            published_at_us=int(state["published_at_us"]),
            capacity_manifest_id=str(state["capacity_manifest_id"]),
            qualification_report_id=str(state["qualification_report_id"]),
            status=cast(BindingLifecycleStatus, status),
            retired_at_us=retired_at_us,
            disabled_reason=disabled_reason,
            replayed=replayed,
        )


class PostgresAuditEvents:
    """Read the same append-only lifecycle events exposed by the audit endpoint."""

    def __init__(self, pool: SpeechPool, *, timeout_seconds: float = 3) -> None:
        self._pool, self._timeout = pool, timeout_seconds

    def list(
        self, *, limit: int, cursor: tuple[int, str] | None, action: str | None
    ) -> tuple[list[dict[str, Any]], tuple[int, str] | None]:
        deadline = time.monotonic() + self._timeout
        filters = [] if action is None else ["action=%s"]
        parameters: list[object] = [] if action is None else [action]
        if cursor is not None:
            filters.append("(occurred_at_us, event_id) < (%s, %s)")
            parameters.extend(cursor)
        clause = "" if not filters else " WHERE " + " AND ".join(filters)
        try:
            with self._pool.connection(timeout=self._timeout) as connection:
                connection.execute("SET TRANSACTION READ ONLY")
                rows = connection.execute(
                    "SELECT event_id, action, binding_id, revision, request_id, actor_type, "
                    "credential_label, occurred_at_us, policy_revision, change_summary_json, "
                    "result_status, retired_at_us, disabled_reason "
                    f"FROM {EVENT_TABLE}{clause} "
                    "ORDER BY occurred_at_us DESC, event_id DESC LIMIT %s",
                    (*parameters, limit + 1),
                ).fetchall()
                if time.monotonic() >= deadline:
                    raise BindingLifecycleUnavailableError
        except (
            psycopg.Error,
            PoolClosed,
            PoolTimeout,
            TooManyRequests,
            BindingLifecycleUnavailableError,
            ValueError,
            TypeError,
        ):
            raise BindingLifecycleUnavailableError from None
        page = rows[:limit]
        next_cursor = None
        if len(rows) > limit and page:
            last = page[-1]
            next_cursor = (int(last["occurred_at_us"]), str(last["event_id"]))
        return page, next_cursor


__all__ = ["PostgresAuditEvents", "PostgresBindingLifecycle"]
