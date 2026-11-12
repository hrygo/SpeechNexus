"""Audit event invariants, keyset page boundaries and action-filter scope.

These are the rules the administrative audit listing may not bend: every emitted
field has a stored source, the page is strictly descending in the composite
``(occurred_at_us, event_id)`` frontier, an action filter cannot leak another
action, and the page is no larger than one over-read. Each violation fails closed
instead of projecting a guessed row or silently re-sorting the page.
"""

from __future__ import annotations

from typing import cast

import pytest

from speechnexus_backend.speech.application.audit_events import (
    AuditEventReads,
    AuditEventService,
)
from speechnexus_backend.speech.domain.audit_events import (
    AuditChangeSummary,
    AuditEvent,
    AuditEventPage,
    AuditEventPosition,
    AuditEventQuery,
    AuditEventUnavailableError,
)


def event(
    *,
    event_id: str = "lifecycle-00000000000000000000000000000001",
    occurred_at_us: int = 1_700_000_000_000_001,
    action: str = "model_binding.retire",
    **overrides: object,
) -> AuditEvent:
    values: dict[str, object] = {
        "event_id": event_id,
        "actor_type": "anonymous",
        "credential_label": None,
        "request_id": "request-audit-1",
        "action": action,
        "resource_type": "model_binding",
        "resource_id": "asr-quality",
        "occurred_at_us": occurred_at_us,
        "policy_revision": 7,
        "change_summary": AuditChangeSummary(("/publication/status",), None),
    }
    values.update(overrides)
    return AuditEvent(**values)  # type: ignore[arg-type]


class FakeReads:
    def __init__(self, rows: tuple[AuditEvent, ...]) -> None:
        self.rows = rows

    def scan(self, query: AuditEventQuery) -> tuple[AuditEvent, ...]:
        return self.rows


def test_every_event_field_is_validated_against_its_declared_domain() -> None:
    assert event().resource_type == "model_binding"
    for overrides in (
        {"event_id": "Not-An-Identifier"},
        {"actor_type": "synthetic"},
        {"credential_label": "x" * 129},
        {"request_id": ""},
        {"request_id": "x" * 129},
        {"action": ""},
        {"action": "x" * 129},
        {"resource_type": "profile"},
        {"resource_id": "Not-An-Identifier"},
        {"occurred_at_us": -1},
        {"occurred_at_us": 253_402_300_799_999_999 + 1},
        {"policy_revision": 0},
        {"policy_revision": 10_000_000_000_000_000_000},
        {"change_summary": "synthetic"},
    ):
        with pytest.raises(ValueError):
            event(**overrides)
    # An empty credential label is a stored value, not an indistinguishable
    # absence: the contract only bounds the length.
    assert event(credential_label="").credential_label == ""
    assert event(change_summary=None).change_summary is None


def test_change_summary_accepts_only_field_pointers_and_a_bounded_reason() -> None:
    assert AuditChangeSummary(("/publication/status",), "replaced").reason == "replaced"
    for fields in ((), ("status",), ("/",), ("/a//b",), ("/bad~2escape",), ("/x" * 200,)):
        with pytest.raises(ValueError):
            AuditChangeSummary(cast(tuple[str, ...], fields), None)
    with pytest.raises(ValueError):
        AuditChangeSummary(("/a",), "x" * 2001)
    with pytest.raises(ValueError):
        AuditChangeSummary(("/a", "/a"), None)


def test_query_bounds_and_the_page_cursor_are_closed() -> None:
    position = AuditEventPosition(1_700_000_000_000_001, "lifecycle-1")
    assert AuditEventQuery(1).after is None
    assert AuditEventQuery(100, position, "model_binding.disable").action == "model_binding.disable"
    # The pagination ceiling is enforced in the domain as well as at the HTTP
    # edge, so an internal caller cannot request an unbounded page.
    for limit in (0, 101, 102):
        with pytest.raises(ValueError):
            AuditEventQuery(limit)
    for invalid_limit in (True, "1", 1.0):
        with pytest.raises(ValueError):
            AuditEventQuery(cast(int, invalid_limit))
    with pytest.raises(ValueError):
        AuditEventQuery(1, cast(AuditEventPosition, object()))
    with pytest.raises(ValueError):
        AuditEventQuery(1, action="x" * 129)
    with pytest.raises(ValueError):
        AuditEventPosition(-1, "lifecycle-1")
    with pytest.raises(ValueError):
        AuditEventPage((), position)
    with pytest.raises(ValueError):
        AuditEventPage((event(),), AuditEventPosition(1_700_000_000_000_002, "lifecycle-1"))


def test_service_slices_limit_plus_one_and_names_the_last_emitted_row() -> None:
    rows = (
        event(event_id="lifecycle-3", occurred_at_us=3),
        event(event_id="lifecycle-2", occurred_at_us=2),
        event(event_id="lifecycle-1", occurred_at_us=1),
    )
    page = AuditEventService(FakeReads(rows)).list(AuditEventQuery(2))
    assert page.items == rows[:2]
    assert page.next_position == AuditEventPosition(2, "lifecycle-2")
    last = AuditEventService(FakeReads(rows)).list(AuditEventQuery(3))
    assert last.items == rows
    assert last.next_position is None
    assert AuditEventService(FakeReads(())).list(AuditEventQuery(1)) == AuditEventPage((), None)


@pytest.mark.parametrize(
    "rows",
    [
        # Ascending: the ordering was not applied.
        (
            event(event_id="lifecycle-1", occurred_at_us=1),
            event(event_id="lifecycle-2", occurred_at_us=2),
        ),
        # Equal timestamps must still descend by the unique event identity.
        (
            event(event_id="lifecycle-1", occurred_at_us=5),
            event(event_id="lifecycle-2", occurred_at_us=5),
        ),
        # Duplicate positions cannot be a page boundary.
        (
            event(event_id="lifecycle-1", occurred_at_us=5),
            event(event_id="lifecycle-1", occurred_at_us=5),
        ),
        # One row over the requested limit's over-read.
        (
            event(event_id="lifecycle-3", occurred_at_us=3),
            event(event_id="lifecycle-2", occurred_at_us=2),
            event(event_id="lifecycle-1", occurred_at_us=1),
        ),
    ],
)
def test_out_of_order_duplicate_or_over_budget_rows_fail_closed(
    rows: tuple[AuditEvent, ...],
) -> None:
    with pytest.raises(AuditEventUnavailableError):
        AuditEventService(FakeReads(rows)).list(AuditEventQuery(1))


def test_a_page_must_resume_strictly_after_the_recorded_position() -> None:
    frontier = AuditEventPosition(5, "lifecycle-3")
    service = AuditEventService(
        FakeReads(
            (
                event(event_id="lifecycle-3", occurred_at_us=5),
                event(event_id="lifecycle-2", occurred_at_us=4),
            )
        )
    )
    # The first row repeats the cursor position instead of resuming after it.
    with pytest.raises(AuditEventUnavailableError):
        service.list(AuditEventQuery(2, frontier))
    assert (
        AuditEventService(FakeReads((event(event_id="lifecycle-2", occurred_at_us=4),)))
        .list(AuditEventQuery(2, frontier))
        .items
    )


def test_a_filtered_page_may_not_leak_another_action() -> None:
    rows = (event(action="model_binding.retire"),)
    service = AuditEventService(FakeReads(rows))
    with pytest.raises(AuditEventUnavailableError):
        service.list(AuditEventQuery(1, action="model_binding.disable"))
    assert service.list(AuditEventQuery(1, action="model_binding.retire")).items == rows


def test_reads_returning_a_non_tuple_fail_closed() -> None:
    reads = cast(AuditEventReads, type("Reads", (), {"scan": lambda self, query: []})())
    with pytest.raises(AuditEventUnavailableError):
        AuditEventService(reads).list(AuditEventQuery(1))


def test_a_page_requires_an_immutable_tuple_of_audit_events() -> None:
    with pytest.raises(ValueError, match="Invalid audit event page"):
        AuditEventPage(cast(tuple[AuditEvent, ...], [event()]), None)
    with pytest.raises(ValueError, match="Invalid audit event page"):
        AuditEventPage((cast(AuditEvent, object()),), None)


def test_a_service_call_requires_a_validated_query_and_event_tuple() -> None:
    with pytest.raises(AuditEventUnavailableError):
        AuditEventService(FakeReads(())).list(cast(AuditEventQuery, object()))
    leaked = cast(tuple[AuditEvent, ...], ({"event_id": "synthetic"},))
    with pytest.raises(AuditEventUnavailableError):
        AuditEventService(FakeReads(leaked)).list(AuditEventQuery(1))
