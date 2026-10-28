"""The final Job write must recheck its pinned binding under the lifecycle lock."""

from __future__ import annotations

import json
from typing import Any, cast

import pytest

from speechnexus_backend.speech.infrastructure.public_jobs_postgres import (
    _binding_is_still_selected,
)


class Connection:
    def __init__(self, row: dict[str, str] | None) -> None:
        self.row = row
        self.queries: list[tuple[str, tuple[Any, ...]]] = []

    def execute(self, sql: str, parameters: tuple[Any, ...]) -> Connection:
        self.queries.append((sql, parameters))
        return self

    def fetchone(self) -> dict[str, str] | None:
        return self.row


def test_guard_locks_original_binding_and_rechecks_the_frozen_revision() -> None:
    connection = Connection({"revision": "2"})
    document = json.dumps({"models": {"tts": {"id": "binding-1", "revision": "2"}}})
    assert _binding_is_still_selected(cast(Any, connection), "tts", "binding-1", document)
    assert "pg_advisory_xact_lock" in connection.queries[0][0]
    assert connection.queries[0][1] == ("binding-1",)
    assert "speech_publication_heads" in connection.queries[1][0]


@pytest.mark.parametrize("row", [None, {"revision": "3"}])
def test_guard_rejects_disabled_or_changed_head(row: dict[str, str] | None) -> None:
    connection = Connection(row)
    document = json.dumps({"models": {"tts": {"id": "binding-1", "revision": "2"}}})
    assert not _binding_is_still_selected(cast(Any, connection), "tts", "binding-1", document)


@pytest.mark.parametrize("document", ["{}", "[]", "not-json", '{"models":{"tts":{"id":"other"}}}'])
def test_guard_rejects_missing_or_mismatched_original_configuration(document: str) -> None:
    connection = Connection({"revision": "2"})
    assert not _binding_is_still_selected(cast(Any, connection), "tts", "binding-1", document)
    assert connection.queries == []
