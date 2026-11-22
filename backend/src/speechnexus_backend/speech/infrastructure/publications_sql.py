"""Insert-only publication statements; reads never create or repair a record."""

from __future__ import annotations

from typing import Any, Protocol

from speechnexus_backend.speech.application.binding_publication import (
    MISSING_HEAD,
    BindingHeadPreconditionError,
)
from speechnexus_backend.speech.domain.configuration import revision_number
from speechnexus_backend.speech.domain.publications import Publication, PublicationKind
from speechnexus_portable_sql.transaction import insert_if_absent


class PublicationCursor(Protocol):
    @property
    def rowcount(self) -> int: ...

    def execute(self, query: str, parameters: tuple[object, ...] = ()) -> object: ...

    def fetchall(self) -> list[dict[str, Any]]: ...


def _publication(row: dict[str, Any]) -> Publication:
    return Publication(
        kind=row["kind"],
        object_id=row["object_id"],
        revision=int(row["revision"]),
        revision_id=row["revision_id"],
        published_at_us=int(row["published_at_us"]),
    )


def append_publication(
    cursor: PublicationCursor,
    publication: Publication,
    *,
    expected_current_revision: object = MISSING_HEAD,
    expected_catalog_revision: object = MISSING_HEAD,
    verify_catalog: bool = True,
) -> bool:
    """Return whether this call inserted the row; a race resolves to no insert.

    An actual insert fires the catalogue-version trigger in this same
    transaction.  A conflict inserts nothing and therefore leaves the version
    unchanged.

    ``verify_catalog`` protects a caller that observed the catalogue version
    before its transaction. The HTTP publication path reads the selected head
    under the binding advisory lock in the same transaction, so it passes
    ``False``: the head CAS is its exact precondition and an unrelated object's
    publication must not turn its request into a spurious conflict.
    """
    cursor.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended("
        "'speechnexus:catalog:' || current_schema() || ':' || ? || ':' || ?, 0))",
        (publication.kind, publication.object_id),
    )
    if publication.kind == "binding":
        existing = find_publication(
            cursor, publication.kind, publication.object_id, publication.revision
        )
        if existing is not None:
            return False
        if expected_current_revision is MISSING_HEAD:
            raise BindingHeadPreconditionError("expected_current_revision_required")
        if verify_catalog:
            if type(expected_catalog_revision) is not int or expected_catalog_revision < 1:
                raise BindingHeadPreconditionError("expected_catalog_revision_required")
        elif expected_catalog_revision is not MISSING_HEAD:
            raise BindingHeadPreconditionError("unexpected_catalog_guard")
        if expected_current_revision is not None:
            if not isinstance(expected_current_revision, str):
                raise ValueError("Invalid configuration revision")
            revision_number(expected_current_revision)
        if verify_catalog:
            cursor.execute(
                "SELECT revision FROM speech_catalog_state WHERE singleton IS TRUE FOR UPDATE"
            )
            state = cursor.fetchall()
            if len(state) != 1:
                raise ValueError("Publication catalogue state is not unique")
            if state[0]["revision"] != expected_catalog_revision:
                raise BindingHeadPreconditionError("configuration_changed")
        cursor.execute(
            "SELECT revision FROM speech_publication_heads WHERE kind = ? AND object_id = ?",
            (publication.kind, publication.object_id),
        )
        rows = cursor.fetchall()
        if len(rows) > 1:
            raise ValueError("Publication head is not unique")
        current = str(rows[0]["revision"]) if rows else None
        if current != expected_current_revision:
            raise BindingHeadPreconditionError("configuration_changed")
    if not insert_if_absent(
        cursor,
        "INSERT INTO speech_publications "
        "(kind, object_id, revision, revision_id, published_at_us) "
        "VALUES (?, ?, ?, ?, ?)",
        (
            publication.kind,
            publication.object_id,
            str(publication.revision),
            publication.revision_id,
            publication.published_at_us,
        ),
    ):
        return False
    # Publication remains append-only.  The mutable head is a separate pointer
    # so a later explicit activation can select an older stored revision without
    # rewriting or republishing it.  The publication trigger already advanced
    # the catalogue version for this insert.
    _advance_head(cursor, publication)
    return True


_HEAD_UPDATE = (
    "UPDATE speech_publication_heads SET revision = ?, revision_id = ?, updated_at_us = ? "
    "WHERE kind = ? AND object_id = ?"
)
_HEAD_INSERT = (
    "INSERT INTO speech_publication_heads "
    "(kind, object_id, revision, revision_id, updated_at_us) VALUES (?, ?, ?, ?, ?)"
)


def _advance_head(cursor: PublicationCursor, publication: Publication) -> None:
    """把可变头指针移到这次发布的版本.

    受限方言没有 `ON CONFLICT … DO UPDATE`,也没有 `RETURNING`,所以用「先更新、再插入」:
    更新命中说明头行已存在(绝大多数路径);未命中说明是第一次发布,插入;若插入也失败,
    说明另一个写者刚好抢先插入,再更新一次即可。同一把 `pg_advisory_xact_lock` 在前面已经持有,
    所以这段不会被并发穿插。
    """

    head = (
        str(publication.revision),
        publication.revision_id,
        publication.published_at_us,
        publication.kind,
        publication.object_id,
    )
    cursor.execute(_HEAD_UPDATE, head)
    if cursor.rowcount == 1:
        return
    inserted = insert_if_absent(
        cursor,
        _HEAD_INSERT,
        (
            publication.kind,
            publication.object_id,
            str(publication.revision),
            publication.revision_id,
            publication.published_at_us,
        ),
    )
    if inserted:
        return
    cursor.execute(_HEAD_UPDATE, head)
    if cursor.rowcount != 1:
        raise ValueError("Publication head could not be advanced")


def find_publication(
    cursor: PublicationCursor, kind: PublicationKind, object_id: str, revision: int
) -> Publication | None:
    cursor.execute(
        "SELECT kind, object_id, revision, revision_id, published_at_us "
        "FROM speech_publications "
        "WHERE kind = ? AND object_id = ? AND revision = ?",
        (kind, object_id, str(revision)),
    )
    rows = cursor.fetchall()
    if len(rows) > 1:
        raise ValueError("Publication identity is not unique")
    return _publication(rows[0]) if rows else None


def find_retirement(
    cursor: PublicationCursor, kind: PublicationKind, object_id: str, revision: int
) -> dict[str, Any] | None:
    cursor.execute(
        "SELECT revision_id, mode, reason, retired_at_us FROM speech_publication_retirements "
        "WHERE kind = ? AND object_id = ? AND revision = ?",
        (kind, object_id, str(revision)),
    )
    rows = cursor.fetchall()
    if len(rows) > 1:
        raise ValueError("Publication retirement identity is not unique")
    return rows[0] if rows else None


def find_current_publication(
    cursor: PublicationCursor, kind: PublicationKind, object_id: str
) -> Publication | None:
    """Return the explicitly selected revision, or absence after retirement."""
    cursor.execute(
        "SELECT p.kind, p.object_id, p.revision, p.revision_id, p.published_at_us "
        "FROM speech_publication_heads AS h "
        "JOIN speech_publications AS p "
        "  ON p.kind = h.kind AND p.object_id = h.object_id "
        " AND p.revision = h.revision AND p.revision_id = h.revision_id "
        "WHERE h.kind = ? AND h.object_id = ? "
        "AND NOT EXISTS ("
        "SELECT 1 FROM speech_publication_retirements AS retired "
        "WHERE retired.kind = h.kind AND retired.object_id = h.object_id "
        "AND retired.revision = h.revision AND retired.revision_id = h.revision_id"
        ")",
        (kind, object_id),
    )
    rows = cursor.fetchall()
    if len(rows) > 1:
        raise ValueError("Current publication identity is not unique")
    return _publication(rows[0]) if rows else None


def retire_publication(
    cursor: PublicationCursor,
    publication: Publication,
    retired_at_us: int,
    *,
    mode: str = "retire",
    reason: str | None = None,
) -> bool:
    """Hide one current publication without rewriting its immutable row.

    The retirement insert fires the same catalogue-version trigger as a
    publication insert.  Repeating the exact retirement is a no-op and does not
    advance the version because no visible set changes.  Retiring a superseded
    or absent revision is rejected instead of manufacturing a pointless version.
    """
    # Publication and retirement paths take this same transaction-scoped lock,
    # so the current-revision check cannot be split by a newer publication.
    cursor.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended("
        "'speechnexus:catalog:' || current_schema() || ':' || ? || ':' || ?, 0))",
        (publication.kind, publication.object_id),
    )
    current = find_current_publication(cursor, publication.kind, publication.object_id)
    if (
        current is None
        or current.revision != publication.revision
        or current.revision_id != publication.revision_id
    ):
        repeated = find_retirement(
            cursor, publication.kind, publication.object_id, publication.revision
        )
        if repeated is not None and repeated["revision_id"] == publication.revision_id:
            return False
        raise ValueError("Retirement must name the current published revision")
    retired = insert_if_absent(
        cursor,
        "INSERT INTO speech_publication_retirements "
        "(kind, object_id, revision, revision_id, retired_at_us, mode, reason) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            publication.kind,
            publication.object_id,
            str(publication.revision),
            publication.revision_id,
            retired_at_us,
            mode,
            reason,
        ),
    )
    if not retired:
        return False
    cursor.execute(
        "DELETE FROM speech_publication_heads "
        "WHERE kind = ? AND object_id = ? AND revision = ? AND revision_id = ?",
        (
            publication.kind,
            publication.object_id,
            str(publication.revision),
            publication.revision_id,
        ),
    )
    if publication.kind == "voice":
        # Retiring the voice revision releases its material reference in this
        # same transaction; content deletion must not stay pinned forever by a
        # revision that can no longer be selected.
        cursor.execute(
            "UPDATE speech_asset_references SET released_at_us = ? "
            "WHERE consumer_kind = 'voice_revision' AND consumer_id = ? "
            "AND consumer_revision = ? AND released_at_us IS NULL",
            (retired_at_us, publication.object_id, str(publication.revision)),
        )
    return True


def find_binding_revision(cursor: PublicationCursor, binding_id: str, revision: int) -> str | None:
    """Return the stored revision ID for exactly this binding revision number.

    ``speech_publications`` has no foreign key to the draft history, so the
    write path must prove the target revision exists before promoting it.
    """
    cursor.execute(
        "SELECT revision_id FROM speech_binding_revisions WHERE binding_id = ? AND revision = ?",
        (binding_id, str(revision)),
    )
    rows = cursor.fetchall()
    if len(rows) > 1:
        raise ValueError("Binding revision identity is not unique")
    return rows[0]["revision_id"] if rows else None


def list_publications(cursor: PublicationCursor, kind: PublicationKind) -> list[Publication]:
    cursor.execute(
        "SELECT kind, object_id, revision, revision_id, published_at_us "
        "FROM speech_publications WHERE kind = ? ORDER BY object_id, revision",
        (kind,),
    )
    return [_publication(row) for row in cursor.fetchall()]
