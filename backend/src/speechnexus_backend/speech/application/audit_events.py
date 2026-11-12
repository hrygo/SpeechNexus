"""Read-only audit event listing; no lifecycle command, intent or evidence write."""

from __future__ import annotations

from typing import Protocol

from speechnexus_backend.speech.domain.audit_events import (
    AuditEvent,
    AuditEventPage,
    AuditEventPosition,
    AuditEventQuery,
    AuditEventUnavailableError,
    position,
)


class AuditEventReads(Protocol):
    def scan(self, query: AuditEventQuery) -> tuple[AuditEvent, ...]:
        """Return rows strictly after the composite keyset, bounded to limit + 1."""
        ...


class AuditEventService:
    """The projection is validated on read; storage never paginates silently.

    A page is accepted only when it is strictly descending in the ``(occurred_at,
    id)`` frontier, when every row still matches the selected action, and when it
    is no larger than one over-read. Any of those failing means the statement,
    the predicate or the cursor did not select what the contract promises, so
    the whole page is refused rather than silently re-sorted or trimmed.
    """

    def __init__(self, reads: AuditEventReads) -> None:
        self._reads = reads

    def list(self, query: AuditEventQuery) -> AuditEventPage:
        if not isinstance(query, AuditEventQuery):
            raise AuditEventUnavailableError
        rows = self._reads.scan(query)
        if not isinstance(rows, tuple) or len(rows) > query.limit + 1:
            raise AuditEventUnavailableError
        previous = query.after
        for record in rows:
            if not isinstance(record, AuditEvent):
                raise AuditEventUnavailableError
            if previous is not None and not (
                record.occurred_at_us < previous.occurred_at_us
                or (
                    record.occurred_at_us == previous.occurred_at_us
                    and record.event_id < previous.event_id
                )
            ):
                raise AuditEventUnavailableError
            previous = AuditEventPosition(record.occurred_at_us, record.event_id)
            if query.action is not None and record.action != query.action:
                # The predicate and the projection disagree; a filtered page
                # that leaks another action cannot be trusted.
                raise AuditEventUnavailableError
        items = rows[: query.limit]
        return AuditEventPage(
            items,
            position(items[-1]) if len(rows) > query.limit else None,
        )
