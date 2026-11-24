"""Publication SQL adapters are exercised without a live PostgreSQL service."""

from __future__ import annotations

from typing import Any

import pytest

from speechnexus_backend.speech.application.binding_publication import BindingHeadPreconditionError
from speechnexus_backend.speech.domain.publications import Publication
from speechnexus_backend.speech.infrastructure.publications_sql import (
    append_publication,
    find_binding_revision,
    find_publication,
    list_publications,
)
from speechnexus_portable_sql.keyed_lock import bucket_for_key, catalog_lock_key
from tests.support.keyed_lock_double import answer_keyed_lock, business_inserts


class DuplicateKeyError(Exception):
    """唯一冲突的最小替身:适配器只看落在这个异常上的约束信息。"""

    sqlstate = "23505"


class Cursor:
    def __init__(
        self,
        rows: list[dict[str, Any]] | None = None,
        rowcount: int = 0,
        *,
        duplicate: bool = False,
    ) -> None:
        self.rows = rows or []
        self.rowcount = rowcount
        self.duplicate = duplicate
        self.calls: list[tuple[str, tuple[object, ...]]] = []
        self._current: list[dict[str, Any]] | None = None

    def execute(self, query: str, parameters: tuple[object, ...] = ()) -> None:
        self.calls.append((query, parameters))
        answered = answer_keyed_lock(query, parameters)
        if answered is not None:
            # 键锁的保存点、懒建桶行与 FOR UPDATE 由共用替身回答,不消耗业务行。
            self._current = answered
            return
        self._current = None
        if self.duplicate and query.lstrip().startswith("INSERT INTO speech_publications"):
            raise DuplicateKeyError(self.duplicate)

    def fetchall(self) -> list[dict[str, Any]]:
        return self.rows if self._current is None else self._current


def row(revision: int = 1, revision_id: str = "binding-rev-1") -> dict[str, Any]:
    return {
        "kind": "binding",
        "object_id": "binding-tts",
        "revision": str(revision),
        "revision_id": revision_id,
        "published_at_us": 1_700_000_000_000_000,
    }


def test_append_uses_insert_if_absent_and_reports_whether_it_inserted() -> None:
    publication = Publication("voice", "binding-tts", 1, "binding-rev-1", 1)
    cursor = Cursor(rowcount=1)

    assert append_publication(cursor, publication) is True
    # 目录身份互斥是键锁表上的一行:先懒建桶行,再锁它,两条语句同一个桶号。
    bucket = bucket_for_key(catalog_lock_key("voice", "binding-tts"))
    lock_calls = [call for call in cursor.calls if "coordination_keyed_locks" in call[0]]
    assert [query.split()[0] for query, _ in lock_calls] == ["INSERT", "SELECT"]
    assert [parameters for _, parameters in lock_calls] == [(bucket,), (bucket,)]
    assert lock_calls[1][0].endswith("FOR UPDATE")
    # 受限方言没有 `ON CONFLICT`:插入包在保存点里,冲突靠回滚到保存点处理。
    savepoint_query, _ = next(call for call in cursor.calls if call[0].startswith("SAVEPOINT"))
    assert savepoint_query.startswith("SAVEPOINT")
    insert_query, insert_parameters = next(
        call for call in cursor.calls if "INSERT INTO speech_publications" in call[0]
    )
    assert insert_query.count("INSERT INTO speech_publications") == 1
    assert "ON CONFLICT" not in insert_query
    assert insert_parameters[2] == "1"

    duplicate = Cursor(duplicate=True)
    assert append_publication(duplicate, publication) is False


def test_new_binding_requires_observed_head_and_rejects_stale_before_writes() -> None:
    publication = Publication("binding", "binding-tts", 1, "binding-rev-1", 1)
    missing = Cursor()
    with pytest.raises(BindingHeadPreconditionError, match="expected_current_revision_required"):
        append_publication(missing, publication)
    assert business_inserts(missing.calls) == []

    class HeadCursor(Cursor):
        def fetchall(self) -> list[dict[str, Any]]:
            if self._current is not None:
                return self._current
            if "speech_catalog_state" in self.calls[-1][0]:
                return [{"revision": 1}]
            if "speech_publication_heads" in self.calls[-1][0]:
                return [{"revision": "2"}]
            return []

    stale = HeadCursor()
    with pytest.raises(BindingHeadPreconditionError, match="configuration_changed"):
        append_publication(
            stale, publication, expected_current_revision=None, expected_catalog_revision=1
        )
    assert business_inserts(stale.calls) == []


def test_existing_binding_replay_precedes_cas_and_never_changes_head() -> None:
    publication = Publication("binding", "binding-tts", 1, "binding-rev-1", 1)
    cursor = Cursor([row()])
    assert not append_publication(cursor, publication, expected_current_revision="9")
    assert not any("speech_publication_heads" in sql for sql, _ in cursor.calls)
    assert business_inserts(cursor.calls) == []


def test_find_publication_returns_the_exact_full_identity() -> None:
    cursor = Cursor([row()])

    assert find_publication(cursor, "binding", "binding-tts", 1) == Publication(
        "binding", "binding-tts", 1, "binding-rev-1", 1_700_000_000_000_000
    )
    assert "revision = ?" in cursor.calls[0][0]
    assert cursor.calls[0][1] == ("binding", "binding-tts", "1")


def test_find_publication_is_absent_not_defaulted() -> None:
    assert find_publication(Cursor(), "binding", "binding-tts", 1) is None


def test_find_binding_revision_reads_the_exact_stored_triple() -> None:
    cursor = Cursor([{"revision_id": "binding-rev-1"}])

    assert find_binding_revision(cursor, "binding-tts", 1) == "binding-rev-1"
    assert "FROM speech_binding_revisions" in cursor.calls[0][0]
    assert cursor.calls[0][1] == ("binding-tts", "1")


def test_find_binding_revision_is_absent_not_invented() -> None:
    assert find_binding_revision(Cursor(), "binding-tts", 1) is None


def test_duplicate_publication_identity_is_rejected() -> None:
    with pytest.raises(ValueError):
        find_publication(Cursor([row(), row()]), "binding", "binding-tts", 1)


def test_list_publications_reads_one_kind_in_deterministic_order() -> None:
    cursor = Cursor([row(1), row(2, "binding-rev-2")])

    assert [item.revision for item in list_publications(cursor, "binding")] == [1, 2]
    assert "WHERE kind = ? ORDER BY object_id, revision" in cursor.calls[0][0]
    assert cursor.calls[0][1] == ("binding",)
