"""Catalogue SQL predicates are locked without requiring a database in unit tests."""

from __future__ import annotations

from typing import Any

import pytest

from speechnexus_backend.speech.infrastructure.model_catalog_sql import (
    list_published_bindings,
    read_catalog_snapshot,
    read_catalog_state,
)


class Cursor:
    def __init__(self, results: list[list[dict[str, Any]]]) -> None:
        self._results = iter(results)
        self.calls: list[tuple[str, tuple[object, ...]]] = []
        self._current: list[dict[str, Any]] = []

    def execute(self, query: str, parameters: tuple[object, ...] = ()) -> None:
        self.calls.append((query, parameters))
        self._current = next(self._results)

    def fetchall(self) -> list[dict[str, Any]]:
        return self._current


def binding_row(revision: int = 2, revision_id: str = "binding-rev-2") -> dict[str, Any]:
    return {
        "kind": "binding",
        "object_id": "binding-tts",
        "revision": str(revision),
        "revision_id": revision_id,
        "published_at_us": 1_700_000_000_000_000 + revision,
        "definition_json": "{}",
    }


def test_bindings_select_the_newest_revision_and_join_the_full_document_identity() -> None:
    cursor = Cursor([[binding_row()]])

    assert list_published_bindings(cursor) == [binding_row()]
    query, parameters = cursor.calls[0]
    assert "v.revision_id = p.revision_id" in query
    assert "speech_published_binding_revisions" in query
    assert "JOIN speech_binding_revisions" not in query
    assert "newer.revision::numeric > p.revision::numeric" in query
    assert "NOT EXISTS" in query
    assert "speech_publication_retirements" in query
    assert parameters == ()


def test_catalogue_state_requires_exactly_one_counter_row() -> None:
    cursor = Cursor([[{"catalog_revision": 7, "generated_at_us": 8}]])

    assert read_catalog_state(cursor) == {"catalog_revision": 7, "generated_at_us": 8}
    assert "FROM speech_catalog_state" in cursor.calls[0][0]
    assert "COUNT(*) + 1" not in cursor.calls[0][0]


def test_catalogue_state_rejects_an_ambiguous_counter() -> None:
    cursor = Cursor([[{"catalog_revision": 7}, {"catalog_revision": 8}]])

    with pytest.raises(ValueError):
        read_catalog_state(cursor)


def test_catalogue_snapshot_reads_bindings_then_its_counter() -> None:
    cursor = Cursor(
        [
            [binding_row()],
            [{"catalog_revision": 7, "generated_at_us": 8}],
        ]
    )

    rows, state = read_catalog_snapshot(cursor)

    assert rows == [binding_row()]
    assert state == {"catalog_revision": 7, "generated_at_us": 8}
    assert len(cursor.calls) == 2
