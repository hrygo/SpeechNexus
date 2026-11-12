"""The audit list query text is the ordering guarantee; no page inherits row order.

The administrative audit listing must not depend on the order PostgreSQL happens
to return rows in. These tests pin the composed statement: the recorded resource
predicate, the composite keyset frontier, an exact action predicate, exactly one
total descending ordering over ``(occurred_at_us, event_id)``, and an over-read of
one row so ``has_more`` is derivable.
"""

from __future__ import annotations

import pytest

from speechnexus_backend.speech.domain.audit_events import (
    AuditEventPosition,
    AuditEventQuery,
    AuditEventUnavailableError,
)
from speechnexus_backend.speech.infrastructure.audit_events_postgres import statement


def order_clause(text: str) -> str:
    """The trailing ordering and limit, with the leading space trimmed."""
    assert " ORDER BY " in text, "the audit list statement must declare an explicit ordering"
    return text[text.index(" ORDER BY ") + 1 :]


def test_the_page_is_ordered_by_the_stored_composite_key_and_nothing_else() -> None:
    text, _ = statement(AuditEventQuery(2))
    clause = order_clause(text)
    assert clause == "ORDER BY occurred_at_us DESC, event_id DESC LIMIT %s"
    # The composite key is total because event_id is unique; a third term or a
    # mutable column would make the order depend on tied rows.
    assert clause.count("ORDER BY") == 1
    assert "occurred_at_us DESC" in clause
    assert "event_id DESC" in clause
    for mutable_column in (
        "action",
        "binding_id",
        "request_id",
        "policy_revision",
        "result_status",
    ):
        assert mutable_column not in clause


def test_the_frontier_compares_the_same_composite_key_the_page_is_ordered_by() -> None:
    text, parameters = statement(
        AuditEventQuery(2, AuditEventPosition(1_700_000_000_000_001, "lifecycle-1"))
    )
    assert " AND (occurred_at_us, event_id) < (%s, %s)" in text
    # A row equal to the frontier is behind it, never repeated on the next page.
    assert "<" in text and "<=" not in text.split(" ORDER BY ")[0]
    assert parameters == (1_700_000_000_000_001, "lifecycle-1", 3)


def test_an_unfiltered_first_page_has_no_frontier_or_action_predicate() -> None:
    text, parameters = statement(AuditEventQuery(1))
    assert "occurred_at_us, event_id) <" not in text
    assert "action = %s" not in text
    assert parameters == (2,)


def test_the_resource_predicate_keeps_the_binding_event_family_read_only() -> None:
    text, _ = statement(AuditEventQuery(1))
    assert "speech_publication_lifecycle_events" in text
    assert "WHERE kind = 'binding'" in text
    # Command, idempotency, intent and disable-intent relations are outside
    # this read; the event table is the whole surface.
    for private_relation in (
        "speech_binding_disable_intents",
        "speech_publication_heads",
        "speech_publications",
        "speech_idempotency",
    ):
        assert private_relation not in text


@pytest.mark.parametrize("action", ["model_binding.retire", "model_binding.disable", ""])
def test_the_action_filter_is_one_exact_match(action: str) -> None:
    text, parameters = statement(AuditEventQuery(1, action=action))
    assert " AND action = %s" in text
    assert parameters == (action, 2)
    assert order_clause(text) == "ORDER BY occurred_at_us DESC, event_id DESC LIMIT %s"


def test_one_extra_row_is_requested_so_has_more_is_derivable() -> None:
    for limit in (1, 50, 100):
        _, parameters = statement(AuditEventQuery(limit))
        assert parameters == (limit + 1,)


def test_a_foreign_query_object_is_refused() -> None:
    with pytest.raises(AuditEventUnavailableError):
        statement(object())  # type: ignore[arg-type]
