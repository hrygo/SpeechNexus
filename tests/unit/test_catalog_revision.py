"""Catalogue version state, visibility tombstones and their provisioning surface."""

from __future__ import annotations

from typing import Any

import pytest

from speechnexus_backend.speech.domain.publications import Publication
from speechnexus_backend.speech.infrastructure.catalog_revision_schema import (
    CATALOG_REVISION_DDL,
    PUBLICATION_REVISION_IDENTITY,
    PUBLICATION_TRIGGER,
    RETIREMENT_TABLE,
    RETIREMENT_TRIGGER,
    STATE_TABLE,
)
from speechnexus_backend.speech.infrastructure.publication_schema import (
    model_catalog_reader_grants,
    publication_grants,
)
from speechnexus_backend.speech.infrastructure.publications_sql import retire_publication


class DuplicateRetirementError(Exception):
    """退休行已存在:适配器只看 sqlstate。"""

    sqlstate = "23505"


class Cursor:
    def __init__(
        self,
        results: list[list[dict[str, Any]]],
        rowcounts: list[int],
        *,
        duplicate_retirement: bool = False,
    ) -> None:
        self._results = iter(results)
        self._rowcounts = iter(rowcounts)
        self.duplicate_retirement = duplicate_retirement
        self._current: list[dict[str, Any]] = []
        self.rowcount = 0
        self.calls: list[tuple[str, tuple[object, ...]]] = []

    def execute(self, query: str, parameters: tuple[object, ...] = ()) -> None:
        self.calls.append((query, parameters))
        if self.duplicate_retirement and query.startswith("INSERT INTO speech_publication"):
            raise DuplicateRetirementError(query)
        if query.startswith(("SAVEPOINT ", "ROLLBACK TO SAVEPOINT", "RELEASE SAVEPOINT")):
            # 插入走保存点(受限方言没有 `ON CONFLICT`);控制语句不消耗脚本化的结果。
            self.rowcount = 0
            return
        self._current = next(self._results)
        self.rowcount = next(self._rowcounts)

    def fetchall(self) -> list[dict[str, Any]]:
        return self._current


def row(revision: int = 1, revision_id: str = "binding-rev-1") -> dict[str, Any]:
    return {
        "kind": "binding",
        "object_id": "binding-tts",
        "revision": str(revision),
        "revision_id": revision_id,
        "published_at_us": 1_700_000_000_000_000,
    }


def test_schema_seeds_the_counter_and_advances_it_from_visible_set_inserts() -> None:
    ddl = "\n".join(CATALOG_REVISION_DDL)

    assert f"CREATE TABLE {STATE_TABLE}" in ddl
    assert "SELECT TRUE, COUNT(*) + 1, COALESCE(MAX(published_at_us), 0)" in ddl
    assert f"CREATE TABLE {RETIREMENT_TABLE}" in ddl
    assert PUBLICATION_REVISION_IDENTITY in ddl
    assert "UNIQUE (kind, object_id, revision, revision_id)" in ddl
    assert "FOREIGN KEY (kind, object_id, revision, revision_id)" in ddl
    assert "AFTER INSERT ON speech_publications" in ddl
    assert f"AFTER INSERT ON {RETIREMENT_TABLE}" in ddl
    assert PUBLICATION_TRIGGER in ddl
    assert RETIREMENT_TRIGGER in ddl
    assert "SECURITY DEFINER" not in ddl
    assert "revision = revision + 1" in ddl


def test_reader_may_select_state_and_tombstones_but_never_write_them() -> None:
    grants = model_catalog_reader_grants("speechnexus_test", "catalogue_reader")
    joined = " ".join(grants)

    assert STATE_TABLE in joined
    assert RETIREMENT_TABLE in joined
    assert "SELECT" in joined
    assert not any(word in joined for word in ("INSERT", "UPDATE", "DELETE", "TRUNCATE"))


def test_publication_writer_can_insert_tombstones_and_advance_the_counter() -> None:
    grants = publication_grants("speechnexus_test", "publication_writer")

    joined = " ".join(grants)
    assert f'GRANT SELECT, INSERT ON "speechnexus_test".{RETIREMENT_TABLE}' in joined
    assert (
        'GRANT SELECT, UPDATE (revision, generated_at_us) ON "'
        f'speechnexus_test".{STATE_TABLE}' in joined
    )


def test_retirement_of_the_current_revision_inserts_one_visible_tombstone() -> None:
    publication = Publication("binding", "binding-tts", 1, "binding-rev-1", 1)
    cursor = Cursor([[], [row()], [], []], [0, 0, 1, 1])

    assert retire_publication(cursor, publication, 1_700_000_000_000_001) is True

    lock_query, lock_parameters = cursor.calls[0]
    select_query, select_parameters = cursor.calls[1]
    savepoint_query = cursor.calls[2][0]
    insert_query, insert_parameters = cursor.calls[3]
    clear_query, clear_parameters = cursor.calls[-1]
    assert "pg_advisory_xact_lock" in lock_query
    assert lock_parameters == ("binding", "binding-tts")
    assert "FROM speech_publication_heads AS h" in select_query
    assert "JOIN speech_publications AS p" in select_query
    assert select_parameters == ("binding", "binding-tts")
    # 受限方言没有 `ON CONFLICT`:插入包在保存点里,冲突靠回滚到保存点处理。
    assert savepoint_query.startswith("SAVEPOINT")
    assert f"INSERT INTO {RETIREMENT_TABLE}" in insert_query
    assert "ON CONFLICT" not in insert_query
    assert insert_parameters == (
        "binding",
        "binding-tts",
        "1",
        "binding-rev-1",
        1_700_000_000_000_001,
        "retire",
        None,
    )
    assert "DELETE FROM speech_publication_heads" in clear_query
    assert clear_parameters == ("binding", "binding-tts", "1", "binding-rev-1")


def test_repeating_a_retirement_does_not_insert_or_advance_again() -> None:
    publication = Publication("binding", "binding-tts", 1, "binding-rev-1", 1)
    cursor = Cursor([[], [row()], []], [0, 0, 0], duplicate_retirement=True)

    assert retire_publication(cursor, publication, 1_700_000_000_000_001) is False
    # 锁、读当前版本、保存点、插入(撞唯一索引)、回滚到保存点、释放保存点。
    assert len(cursor.calls) == 6
    assert cursor.calls[4][0].startswith("ROLLBACK TO SAVEPOINT")


def test_retirement_must_name_the_current_exact_revision() -> None:
    publication = Publication("binding", "binding-tts", 1, "binding-rev-1", 1)
    superseded = Cursor([[], [row(2, "binding-rev-2")], []], [0, 0, 0])

    with pytest.raises(ValueError, match="current published revision"):
        retire_publication(superseded, publication, 1_700_000_000_000_001)
    assert len(superseded.calls) == 3

    mismatched = Cursor([[], [row(1, "binding-rev-other")], []], [0, 0, 0])
    with pytest.raises(ValueError, match="current published revision"):
        retire_publication(mismatched, publication, 1_700_000_000_000_001)
    assert len(mismatched.calls) == 3
