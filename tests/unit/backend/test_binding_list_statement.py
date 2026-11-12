"""The list query text is the ordering guarantee; no page may inherit row order.

The administrative list must not depend on the order PostgreSQL happens to
return rows in. These tests pin the composed statement: exactly one total
ordering over the unique keyset key, no tie-breaking term over a mutable column,
an over-read of one row so ``has_more`` is derivable, and a status predicate
that selects precisely the rows the derived status produces.
"""

from __future__ import annotations

import pytest

from speechnexus_backend.speech.domain.binding_summaries import (
    BINDING_PUBLICATION_STATUSES,
    BindingSummaryQuery,
    BindingSummaryUnavailableError,
)
from speechnexus_backend.speech.infrastructure.binding_summaries_postgres import (
    statement,
)


def order_clause(text: str) -> str:
    """The trailing ordering and limit, with the leading space trimmed."""
    assert " ORDER BY " in text, "the list statement must declare an explicit ordering"
    return text[text.index(" ORDER BY ") + 1 :]


def test_the_page_is_ordered_by_the_unique_keyset_key_and_nothing_else() -> None:
    text, _ = statement(BindingSummaryQuery(2))
    clause = order_clause(text)
    assert clause == "ORDER BY family.binding_id LIMIT %s"
    # A secondary term would make the order depend on equal keys, and a missing
    # ordering would let the physical scan order leak into the page.
    assert "," not in clause.split(" LIMIT ")[0]
    assert clause.count("ORDER BY") == 1
    # The frontier must be compared on the same key the page is ordered by.
    assert "WHERE family.binding_id > %s" in text


def test_binding_id_is_the_only_mutable_free_ordering_column() -> None:
    text, _ = statement(BindingSummaryQuery(1))
    clause = order_clause(text)
    for mutable_column in (
        "latest_revision",
        "current_revision",
        "publication_status",
        "logical_model",
        "release_tier",
        "created_at",
    ):
        assert mutable_column not in clause


def test_one_extra_row_is_requested_so_has_more_is_derivable() -> None:
    for limit in (1, 50, 100):
        _, parameters = statement(BindingSummaryQuery(limit))
        assert parameters == ("", limit + 1)


def test_the_frontier_is_the_recorded_key_or_the_empty_prefix() -> None:
    assert statement(BindingSummaryQuery(1, "asr-quality"))[1][0] == "asr-quality"
    assert statement(BindingSummaryQuery(1))[1][0] == ""


def test_only_the_declared_read_relations_are_selected() -> None:
    text, _ = statement(BindingSummaryQuery(1))
    for relation in (
        "speech_binding_families",
        "speech_binding_revisions",
        "speech_publications",
        "speech_publication_retirements",
    ):
        assert relation in text
    # Command, intent, idempotency and evidence relations are outside this read.
    for private_relation in (
        "speech_binding_commands",
        "speech_idempotency",
        "speech_binding_intents",
        "speech_binding_publication_evidence",
    ):
        assert private_relation not in text


def predicate_clause(text: str) -> str:
    """The filters between the keyset frontier and the ordered tail."""
    return text.split("WHERE", 1)[1].split(" ORDER BY ", 1)[0]


def test_an_unfiltered_page_has_no_status_predicate() -> None:
    text, _ = statement(BindingSummaryQuery(1))
    clause = predicate_clause(text)
    assert "publication.revision IS" not in clause
    assert "retired.mode" not in clause


@pytest.mark.parametrize("status", BINDING_PUBLICATION_STATUSES)
def test_each_status_predicate_is_exactly_the_derived_status(status: str) -> None:
    text, _ = statement(BindingSummaryQuery(1, "asr-a", status))  # type: ignore[arg-type]
    predicates = {
        "draft": "publication.revision IS NULL",
        "published": "publication.revision IS NOT NULL AND retired.mode IS NULL",
        "retired": "retired.mode = 'retire'",
        "disabled": "retired.mode = 'disable'",
    }
    assert predicates[status] in text
    # Exactly one filter form can be present, otherwise two statuses would leak
    # into one page.
    assert sum(text.count(shape) for shape in predicates.values()) == 1
    assert order_clause(text) == "ORDER BY family.binding_id LIMIT %s"


def test_a_foreign_query_object_is_refused() -> None:
    with pytest.raises(BindingSummaryUnavailableError):
        statement(object())  # type: ignore[arg-type]
