"""Read-only keyset projection of the append-only lifecycle audit events.

The projection reads only ``speech_publication_lifecycle_events``, whose CHECK
constraint keeps every row at ``kind=binding`` and whose recorded action is one
of the model binding lifecycle commands. The statement is the whole query
surface: the resource predicate, the composite cursor frontier, the optional
exact action filter, one total descending ordering and an over-read of one row.

Ordering is ``occurred_at_us DESC, event_id DESC``, which is the order of
``speech_publication_lifecycle_events_audit_order`` and a total order because
``event_id`` is unique. A page boundary is therefore a position in that frontier,
not an offset into a sort over a mutable column. The action filter is an exact
match; a value no row carries selects nothing and returns an empty page.
"""

from __future__ import annotations

import time
from typing import Any

import psycopg
from psycopg_pool import PoolClosed, PoolTimeout, TooManyRequests

from speechnexus_backend.speech.domain.audit_events import (
    AUDIT_RESOURCE_TYPE,
    AuditChangeSummary,
    AuditEvent,
    AuditEventQuery,
    AuditEventUnavailableError,
)
from speechnexus_backend.speech.infrastructure.postgres_pool import SpeechPool
from speechnexus_backend.speech.infrastructure.publication_lifecycle_schema import EVENT_TABLE

_COLUMNS = (
    "event_id, kind, action, binding_id, request_id, actor_type, credential_label, "
    "occurred_at_us, policy_revision, change_summary_json"
)
_BASE = f"SELECT {_COLUMNS} FROM {EVENT_TABLE} WHERE kind = 'binding'"
_ORDER = " ORDER BY occurred_at_us DESC, event_id DESC LIMIT %s"
_SUMMARY_KEYS = frozenset({"changed_fields", "reason"})


def statement(query: AuditEventQuery) -> tuple[str, tuple[object, ...]]:
    """Compose the single statement this reader may run for one page.

    The returned text carries the resource predicate, the keyset frontier, the
    optional action predicate and exactly one ordering. A caller cannot widen
    it, and the ordering stays the stored composite key so no page can depend on
    the database's incidental row order.
    """
    if not isinstance(query, AuditEventQuery):
        raise AuditEventUnavailableError
    text = _BASE
    parameters: list[object] = []
    if query.after is not None:
        text += " AND (occurred_at_us, event_id) < (%s, %s)"
        parameters.extend((query.after.occurred_at_us, query.after.event_id))
    if query.action is not None:
        text += " AND action = %s"
        parameters.append(query.action)
    text += _ORDER
    parameters.append(query.limit + 1)
    return text, tuple(parameters)


class PostgresAuditEventReads:
    def __init__(self, pool: SpeechPool, *, timeout_seconds: float = 3) -> None:
        if isinstance(timeout_seconds, bool) or not 0 < timeout_seconds <= 30:
            raise ValueError("Audit event reads require bounded deadlines")
        self._pool, self._timeout = pool, timeout_seconds

    def scan(self, query: AuditEventQuery) -> tuple[AuditEvent, ...]:
        text, parameters = statement(query)
        deadline = time.monotonic() + self._timeout
        try:
            with self._pool.connection(timeout=self._timeout) as connection:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise AuditEventUnavailableError
                connection.execute("SET TRANSACTION READ ONLY")
                connection.execute(
                    "SELECT set_config('statement_timeout', %s, true)",
                    (str(max(1, int(remaining * 1000))),),
                )
                records = connection.execute(text, parameters).fetchall()
                result = tuple(_event(row) for row in records)
                if time.monotonic() >= deadline:
                    raise AuditEventUnavailableError
            return result
        except (
            psycopg.Error,
            PoolClosed,
            PoolTimeout,
            TooManyRequests,
            AuditEventUnavailableError,
            ValueError,
            TypeError,
            KeyError,
        ):
            raise AuditEventUnavailableError from None


def _event(row: dict[str, Any]) -> AuditEvent:
    if row["kind"] != "binding":
        # The contract states this surface records binding events only; another
        # kind means the projection would label a row with the wrong resource.
        raise AuditEventUnavailableError
    return AuditEvent(
        event_id=row["event_id"],
        actor_type=row["actor_type"],
        credential_label=row["credential_label"],
        request_id=row["request_id"],
        action=row["action"],
        resource_type=AUDIT_RESOURCE_TYPE,
        resource_id=row["binding_id"],
        occurred_at_us=row["occurred_at_us"],
        policy_revision=row["policy_revision"],
        change_summary=_change_summary(row["change_summary_json"]),
    )


def _change_summary(value: Any) -> AuditChangeSummary | None:
    """Project only the contract's summary fields, never a stored payload."""
    if value is None:
        return None
    if not isinstance(value, dict) or not set(value) <= _SUMMARY_KEYS:
        # Unknown keys could carry before/after values the contract forbids.
        raise AuditEventUnavailableError
    changed_fields = value.get("changed_fields")
    reason = value.get("reason")
    if not isinstance(changed_fields, list):
        raise AuditEventUnavailableError
    return AuditChangeSummary(changed_fields=tuple(changed_fields), reason=reason)
