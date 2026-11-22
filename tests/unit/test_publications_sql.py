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

    def execute(self, query: str, parameters: tuple[object, ...] = ()) -> None:
        self.calls.append((query, parameters))
        if self.duplicate and query.lstrip().startswith("INSERT INTO speech_publications"):
            raise DuplicateKeyError(self.duplicate)

    def fetchall(self) -> list[dict[str, Any]]:
        return self.rows


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
    lock_query, lock_parameters = cursor.calls[0]
    savepoint_query, _ = cursor.calls[1]
    insert_query, insert_parameters = cursor.calls[2]
    assert "pg_advisory_xact_lock" in lock_query
    assert lock_parameters == ("voice", "binding-tts")
    # 受限方言没有 `ON CONFLICT`:插入包在保存点里,冲突靠回滚到保存点处理。
    assert savepoint_query.startswith("SAVEPOINT")
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
    assert not any("INSERT INTO" in sql for sql, _ in missing.calls)

    class HeadCursor(Cursor):
        def fetchall(self) -> list[dict[str, Any]]:
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
    assert not any("INSERT INTO" in sql for sql, _ in stale.calls)


def test_existing_binding_replay_precedes_cas_and_never_changes_head() -> None:
    publication = Publication("binding", "binding-tts", 1, "binding-rev-1", 1)
    cursor = Cursor([row()])
    assert not append_publication(cursor, publication, expected_current_revision="9")
    assert not any(
        "speech_publication_heads" in sql or "INSERT INTO" in sql for sql, _ in cursor.calls
    )


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
