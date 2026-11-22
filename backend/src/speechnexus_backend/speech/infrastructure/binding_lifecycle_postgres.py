"""Atomic binding retirement, activation and append-only lifecycle evidence."""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import Callable
from typing import Any, Literal, cast

from speechnexus_backend.speech.application.binding_documents import (
    decode_document,
    require_published_identity,
)
from speechnexus_backend.speech.domain.binding_drafts import BindingDefinitionIdentityError
from speechnexus_backend.speech.domain.binding_lifecycle import (
    BindingActivationCommand,
    BindingActivationConflictError,
    BindingLifecycleConflictError,
    BindingLifecycleNotFoundError,
    BindingLifecycleReceipt,
    BindingLifecycleStatus,
    BindingLifecycleUnavailableError,
    BindingPublicationCommand,
    BindingPublicationEvidenceError,
    BindingPublicationHeadConflictError,
    BindingRetirementCommand,
)
from speechnexus_backend.speech.domain.binding_publication_evidence import (
    BindingPublicationEvidence,
)
from speechnexus_backend.speech.domain.publications import Publication
from speechnexus_backend.speech.infrastructure.binding_disable_barrier import lock_binding
from speechnexus_backend.speech.infrastructure.binding_publication_evidence_sql import (
    insert_evidence,
)
from speechnexus_backend.speech.infrastructure.postgres_pool import SpeechPool
from speechnexus_backend.speech.infrastructure.publication_lifecycle_schema import (
    HEAD_TABLE,
)
from speechnexus_backend.speech.infrastructure.publications_sql import (
    advance_publication_head,
    append_publication,
)
from speechnexus_portable_sql.clock import ClockReader, now_us
from speechnexus_portable_sql.transaction import is_savepoint_control
from speechnexus_portable_sql.vendor import PoolClosed, PoolTimeout, TooManyRequests, psycopg

type LifecycleOperation = Literal["publish", "retire", "disable", "activate"]
type LifecycleCommand = (
    BindingPublicationCommand | BindingRetirementCommand | BindingActivationCommand
)


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
        if is_savepoint_control(query):
            return self.raw.execute(query, parameters)
        self.raw.execute(
            "SELECT set_config('statement_timeout', ?, true)",
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

    def publish(self, command: BindingPublicationCommand) -> BindingLifecycleReceipt:
        return self._run(
            lambda cursor: self._publish(cursor, command),
            preserve=(
                BindingLifecycleConflictError,
                BindingLifecycleNotFoundError,
                BindingPublicationEvidenceError,
                BindingDefinitionIdentityError,
            ),
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
            "INSERT INTO speech_publication_retirements "
            "(kind, object_id, revision, revision_id, retired_at_us, mode, reason) "
            "VALUES ('binding', ?, ?, ?, ?, ?, ?)",
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
            "DELETE FROM speech_publication_heads WHERE kind='binding' AND object_id=? "
            "AND revision=? AND revision_id=?",
            (command.binding_id, command.revision, state["revision_id"]),
        )
        if cursor.rowcount != 1:
            raise BindingLifecycleUnavailableError
        policy_revision = self._catalog_revision(cursor)
        status = "retired" if command.mode == "retire" else "disabled"
        disabled_reason = command.reason if command.mode == "disable" else None
        event_id = self._record(
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
        if command.mode == "disable":
            cursor.execute(
                "INSERT INTO speech_binding_disable_intents (event_id) VALUES (?)",
                (event_id,),
            )
            if cursor.rowcount != 1:
                raise BindingLifecycleUnavailableError
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
            raise BindingActivationConflictError(current_revision)
        occurred_at_us = self._clock(cursor)
        changed = (
            head is None
            or head["revision"] != command.revision
            or head["revision_id"] != state["revision_id"]
        )
        if changed:
            advance_publication_head(
                cursor,
                Publication(
                    "binding",
                    command.binding_id,
                    int(command.revision),
                    state["revision_id"],
                    occurred_at_us,
                ),
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

    def _publish(
        self, cursor: _Cursor, command: BindingPublicationCommand
    ) -> BindingLifecycleReceipt:
        # The publication path never rewrites a publication row: a target that is
        # already published (or retired) is not a draft and cannot be re-published
        # under a new key. Only the original key replays.
        self._lock(cursor, command.binding_id)
        replay = self._replay(cursor, command, "publish")
        if replay is not None:
            return replay
        if self._state(cursor, command.binding_id, command.revision) is not None:
            raise BindingLifecycleConflictError("binding_not_draft")
        cursor.execute(
            "SELECT revision_id, definition_json, created_at_us FROM speech_binding_revisions "
            "WHERE binding_id=? AND revision=?",
            (command.binding_id, command.revision),
        )
        drafts = cursor.fetchall()
        if not drafts:
            raise BindingLifecycleNotFoundError
        if len(drafts) != 1:
            raise BindingLifecycleUnavailableError
        draft = drafts[0]
        revision_id = str(draft["revision_id"])
        definition = decode_document(str(draft["definition_json"]).encode("utf-8"))
        # A published revision is read as a contract document that names its own
        # binding; promoting one that does not would make the catalog unreadable.
        require_published_identity(definition, command.binding_id)
        recipe = definition.get("execution_recipe")
        declared = recipe.get("capacity_manifest_id") if isinstance(recipe, dict) else None
        if not isinstance(declared, str) or declared != command.capacity_manifest_id:
            raise BindingPublicationEvidenceError
        head = self._head(cursor, command.binding_id)
        current_revision = None if head is None else str(head["revision"])
        if current_revision != command.expected_current_revision:
            raise BindingPublicationHeadConflictError(current_revision)
        occurred_at_us = self._clock(cursor)
        publication = Publication(
            kind="binding",
            object_id=command.binding_id,
            revision=int(command.revision),
            revision_id=revision_id,
            published_at_us=occurred_at_us,
        )
        evidence = BindingPublicationEvidence(
            object_id=command.binding_id,
            revision=int(command.revision),
            revision_id=revision_id,
            capacity_manifest_id=command.capacity_manifest_id,
            qualification_report_id=command.qualification_report_id,
            evidence_source="publish_http",
            recorded_at_us=occurred_at_us,
        )
        # The head compare-and-swap is re-checked inside the append under the same
        # binding lock. The catalogue version is not a caller observation here, so
        # an unrelated publication must not turn this request into a conflict.
        if not append_publication(
            cursor,
            publication,
            expected_current_revision=command.expected_current_revision,
            verify_catalog=False,
        ):
            raise BindingLifecycleConflictError("binding_not_draft")
        if not insert_evidence(cursor, evidence):
            raise BindingLifecycleUnavailableError
        policy_revision = self._catalog_revision(cursor)
        self._record(
            cursor,
            command,
            action="model_binding.publish",
            result_status="published",
            retired_at_us=None,
            disabled_reason=None,
            occurred_at_us=occurred_at_us,
            policy_revision=policy_revision,
            change_summary={
                "changed_fields": ["/publication/status", "/publication/head_revision"],
                "reason": None,
            },
        )
        state = self._state(cursor, command.binding_id, command.revision)
        if state is None:
            raise BindingLifecycleUnavailableError
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
        command: LifecycleCommand,
        operation: LifecycleOperation,
    ) -> BindingLifecycleReceipt | None:
        if command.key_digest is None:
            return None
        cursor.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended("
            "'speechnexus:catalog:' || current_schema() || ':idempotency:' || ?, 0))",
            (command.key_digest,),
        )
        cursor.execute(
            "SELECT * FROM speech_publication_lifecycle_events WHERE idempotency_key_digest=?",
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
        command: LifecycleCommand,
        *,
        action: str,
        result_status: str,
        retired_at_us: int | None,
        disabled_reason: str | None,
        occurred_at_us: int,
        policy_revision: int,
        change_summary: dict[str, object],
    ) -> str:
        state = self._state(cursor, command.binding_id, command.revision)
        if state is None:
            raise BindingLifecycleUnavailableError
        event_id = "lifecycle-" + uuid.uuid4().hex
        cursor.execute(
            "INSERT INTO speech_publication_lifecycle_events "
            "(event_id, kind, action, binding_id, revision, revision_id, result_status, "
            " retired_at_us, disabled_reason, request_id, actor_type, credential_label, "
            " occurred_at_us, policy_revision, intent_digest, idempotency_key_digest, "
            " change_summary_json) "
            "VALUES (?, 'binding', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
            " CAST(? AS jsonb))",
            (
                event_id,
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
        if cursor.rowcount != 1:
            raise BindingLifecycleUnavailableError
        return event_id

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
            "LEFT JOIN speech_publication_retirements AS ret "
            "  ON ret.kind=p.kind AND ret.object_id=p.object_id AND ret.revision=p.revision "
            " AND ret.revision_id=p.revision_id "
            "WHERE p.kind='binding' AND p.object_id=? AND p.revision=?",
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
            "WHERE kind='binding' AND object_id=?",
            (binding_id,),
        )
        rows = cursor.fetchall()
        if not rows:
            return None
        if len(rows) != 1:
            raise BindingLifecycleUnavailableError
        return rows[0]

    def _lock(self, cursor: _Cursor, binding_id: str) -> None:
        lock_binding(cursor, binding_id)

    def _clock(self, cursor: _Cursor) -> int:
        """「现在」只从便携时钟读一次;语句里不再有品牌函数."""

        try:
            return now_us(cast(ClockReader, cursor))
        except (LookupError, TypeError) as error:
            raise BindingLifecycleUnavailableError from error

    def _catalog_revision(self, cursor: _Cursor) -> int:
        cursor.execute("SELECT revision FROM speech_catalog_state WHERE singleton IS TRUE")
        rows = cursor.fetchall()
        if len(rows) != 1:
            raise BindingLifecycleUnavailableError
        return int(rows[0]["revision"])

    def _advance_catalog(self, cursor: _Cursor, changed_at_us: int) -> None:
        cursor.execute(
            "UPDATE speech_catalog_state SET revision=revision+1, "
            "generated_at_us=GREATEST(generated_at_us, ?)",
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


__all__ = ["PostgresBindingLifecycle"]
