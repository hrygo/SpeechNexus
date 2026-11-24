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
from speechnexus_portable_sql.keyed_lock import bucket_for_key, catalog_lock_key
from tests.support.keyed_lock_double import answer_keyed_lock, business_inserts


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
        answered = answer_keyed_lock(query, parameters)
        if answered is not None:
            # 目录身份键锁的保存点、懒建桶行与 FOR UPDATE:由共用替身回答,
            # 不消耗脚本化的业务结果,所以脚本仍按业务语句逐条对位。
            self._current, self.rowcount = answered, 0
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
    # 键锁的语句由共用替身回答,不占脚本位;脚本只按业务语句逐条对位。
    cursor = Cursor([[row()], [], []], [0, 1, 1])

    assert retire_publication(cursor, publication, 1_700_000_000_000_001) is True

    bucket = bucket_for_key(catalog_lock_key("binding", "binding-tts"))
    lock_calls = [call for call in cursor.calls if "coordination_keyed_locks" in call[0]]
    assert [query.split()[0] for query, _ in lock_calls] == ["INSERT", "SELECT"]
    assert [parameters for _, parameters in lock_calls] == [(bucket,), (bucket,)]
    assert lock_calls[1][0].endswith("FOR UPDATE")
    select_query, select_parameters = next(
        call for call in cursor.calls if "FROM speech_publication_heads AS h" in call[0]
    )
    savepoint_query = next(call[0] for call in cursor.calls if call[0].startswith("SAVEPOINT"))
    insert_query, insert_parameters = next(
        call for call in cursor.calls if call[0].startswith(f"INSERT INTO {RETIREMENT_TABLE}")
    )
    clear_query, clear_parameters = next(
        call for call in cursor.calls if "DELETE FROM speech_publication_heads" in call[0]
    )
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
    cursor = Cursor([[row()]], [0], duplicate_retirement=True)

    assert retire_publication(cursor, publication, 1_700_000_000_000_001) is False
    # 键锁四条(懒建桶行、`FOR UPDATE`,各配一条保存点控制)、读当前版本,再是
    # 保存点、插入(撞唯一索引)、回滚到保存点、释放保存点。
    assert len(cursor.calls) == 9
    insert_at = next(
        index
        for index, call in enumerate(cursor.calls)
        if call[0].startswith(f"INSERT INTO {RETIREMENT_TABLE}")
    )
    assert cursor.calls[insert_at + 1][0].startswith("ROLLBACK TO SAVEPOINT")


def test_retirement_must_name_the_current_exact_revision() -> None:
    publication = Publication("binding", "binding-tts", 1, "binding-rev-1", 1)
    superseded = Cursor([[row(2, "binding-rev-2")], []], [0, 0])

    with pytest.raises(ValueError, match="current published revision"):
        retire_publication(superseded, publication, 1_700_000_000_000_001)
    assert business_inserts(superseded.calls) == []

    mismatched = Cursor([[row(1, "binding-rev-other")], []], [0, 0])
    with pytest.raises(ValueError, match="current published revision"):
        retire_publication(mismatched, publication, 1_700_000_000_000_001)
    assert business_inserts(mismatched.calls) == []
