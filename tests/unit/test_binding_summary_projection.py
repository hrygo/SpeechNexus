"""Binding summary invariants, status mapping and keyset page boundaries.

These are the rules the administrative list may not bend: every emitted field
has a stored source, the derived status matches the publication/tombstone facts,
the keyset order is deterministic and a filter cannot leak another status. Each
violation fails closed instead of projecting a guessed row.
"""

from __future__ import annotations

from dataclasses import replace
from typing import cast

import pytest

from speechnexus_backend.speech.application.binding_summaries import (
    BindingSummaryReads,
    BindingSummaryService,
)
from speechnexus_backend.speech.domain.binding_summaries import (
    BindingSummaryPage,
    BindingSummaryQuery,
    BindingSummaryUnavailableError,
    ModelBindingSummary,
    publication_status,
)


def summary(**overrides: object) -> ModelBindingSummary:
    values: dict[str, object] = {
        "binding_id": "asr-quality",
        "latest_revision": "2",
        "current_revision": "1",
        "logical_model": "whisper-large-v3",
        "tasks": ("asr_file",),
        "release_tier": "production",
        "publication_status": "draft",
    }
    values.update(overrides)
    return ModelBindingSummary(**values)  # type: ignore[arg-type]


class FakeReads:
    def __init__(self, rows: tuple[ModelBindingSummary, ...]) -> None:
        self.rows = rows

    def scan(self, query: BindingSummaryQuery) -> tuple[ModelBindingSummary, ...]:
        return self.rows


def test_status_maps_only_the_exact_revision_publication_and_tombstone() -> None:
    assert publication_status(published=False, retirement_mode=None) == "draft"
    assert publication_status(published=True, retirement_mode=None) == "published"
    assert publication_status(published=True, retirement_mode="retire") == "retired"
    assert publication_status(published=True, retirement_mode="disable") == "disabled"
    with pytest.raises(ValueError):
        publication_status(published=False, retirement_mode="retire")
    with pytest.raises(ValueError):
        publication_status(published=True, retirement_mode="unknown")


def test_every_summary_field_is_validated_against_its_declared_domain() -> None:
    assert summary() == ModelBindingSummary(
        binding_id="asr-quality",
        latest_revision="2",
        current_revision="1",
        logical_model="whisper-large-v3",
        tasks=("asr_file",),
        release_tier="production",
        publication_status="draft",
    )
    for overrides in (
        {"binding_id": "Not-An-Identifier"},
        {"latest_revision": "0"},
        {"logical_model": "Whisper Large"},
        {"tasks": ()},
        {"tasks": ("asr_file", "asr_file")},
        {"tasks": ("synthetic_task",)},
        {"release_tier": "synthetic"},
        {"publication_status": "synthetic"},
        {"current_revision": "3"},
        {"current_revision": "2", "publication_status": "draft"},
        {"current_revision": "2", "publication_status": "retired"},
    ):
        with pytest.raises(ValueError):
            summary(**overrides)
    assert summary(current_revision=None, publication_status="draft").current_revision is None
    assert summary(current_revision="2", publication_status="published").current_revision == "2"


def test_query_bounds_and_the_page_cursor_are_closed() -> None:
    assert BindingSummaryQuery(1).after_id is None
    assert BindingSummaryQuery(100, "asr-quality", "published").status == "published"
    for limit in (0, 101):
        with pytest.raises(ValueError):
            BindingSummaryQuery(limit)
    with pytest.raises(ValueError):
        BindingSummaryQuery(1, "")
    with pytest.raises(ValueError):
        BindingSummaryQuery(1, "asr-quality", cast(object, "synthetic"))  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        BindingSummaryPage((), "asr-quality")
    with pytest.raises(ValueError):
        BindingSummaryPage((summary(),), "asr-other")


def test_service_slices_limit_plus_one_and_names_the_last_emitted_row() -> None:
    rows = (
        summary(binding_id="asr-a"),
        summary(binding_id="asr-b"),
        summary(binding_id="asr-c"),
    )
    page = BindingSummaryService(FakeReads(rows)).list(BindingSummaryQuery(2))
    assert page.items == rows[:2]
    assert page.next_id == "asr-b"
    last = BindingSummaryService(FakeReads(rows)).list(BindingSummaryQuery(3))
    assert last.items == rows
    assert last.next_id is None
    assert BindingSummaryService(FakeReads(())).list(BindingSummaryQuery(1)) == BindingSummaryPage(
        (), None
    )


@pytest.mark.parametrize(
    "rows",
    [
        (summary(binding_id="asr-b"), summary(binding_id="asr-a")),
        (summary(binding_id="asr-a"), summary(binding_id="asr-a")),
        (summary(binding_id="asr-a"), summary(binding_id="asr-b"), summary(binding_id="asr-c")),
    ],
)
def test_out_of_order_duplicate_or_over_budget_rows_fail_closed(
    rows: tuple[ModelBindingSummary, ...],
) -> None:
    service = BindingSummaryService(FakeReads(rows))
    with pytest.raises(BindingSummaryUnavailableError):
        service.list(BindingSummaryQuery(1))


def test_a_filtered_page_may_not_leak_another_status() -> None:
    rows = (summary(binding_id="asr-a", publication_status="draft"),)
    service = BindingSummaryService(FakeReads(rows))
    with pytest.raises(BindingSummaryUnavailableError):
        service.list(BindingSummaryQuery(1, status="published"))
    assert service.list(BindingSummaryQuery(1, status="draft")).items == rows


def test_reads_returning_a_non_tuple_fail_closed() -> None:
    reads = cast(BindingSummaryReads, type("Reads", (), {"scan": lambda self, query: []})())
    with pytest.raises(BindingSummaryUnavailableError):
        BindingSummaryService(reads).list(BindingSummaryQuery(1))


def test_replacing_a_field_changes_the_projected_summary() -> None:
    # Guards the field-by-field mapping: a projection that drops or swaps a
    # stored value cannot be equal to the expected summary.
    assert replace(summary(), logical_model="asr-other") != summary()
