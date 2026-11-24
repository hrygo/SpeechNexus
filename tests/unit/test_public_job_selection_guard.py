"""The final Job write must recheck its pinned binding under the lifecycle lock."""

from __future__ import annotations

import json
from typing import Any, cast

import pytest

from speechnexus_backend.speech.infrastructure.public_jobs_postgres import (
    _binding_is_still_selected,
)
from speechnexus_portable_sql.keyed_lock import binding_lock_key, bucket_for_key
from tests.support.keyed_lock_double import answer_keyed_lock


class Connection:
    def __init__(self, row: dict[str, str] | None) -> None:
        self.row = row
        self.rows: list[dict[str, Any]] = []
        self.queries: list[tuple[str, tuple[Any, ...]]] = []

    def execute(self, sql: str, parameters: tuple[Any, ...] = ()) -> Connection:
        self.queries.append((sql, parameters))
        self.rows = answer_keyed_lock(sql, parameters) or []
        return self

    def fetchall(self) -> list[dict[str, Any]]:
        return self.rows

    def fetchone(self) -> dict[str, str] | None:
        return self.row


def test_guard_locks_original_binding_and_rechecks_the_frozen_revision() -> None:
    connection = Connection({"revision": "2"})
    document = json.dumps({"models": {"tts": {"id": "binding-1", "revision": "2"}}})
    assert _binding_is_still_selected(cast(Any, connection), "tts", "binding-1", document)
    # 绑定行锁先取,再重读头指针:行是共享键推导出来的,不是另拼的字符串。
    lock = next(
        index
        for index, (sql, _) in enumerate(connection.queries)
        if "coordination_keyed_locks" in sql and "FOR UPDATE" in sql
    )
    heads = next(
        index
        for index, (sql, _) in enumerate(connection.queries)
        if "speech_publication_heads" in sql
    )
    assert lock < heads
    assert connection.queries[lock][1] == (bucket_for_key(binding_lock_key("binding-1")),)


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
