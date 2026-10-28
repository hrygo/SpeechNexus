"""Insert-only publication statements; reads never create or repair a record."""

from __future__ import annotations

from typing import Any, Protocol

from speechnexus_backend.speech.domain.publications import Publication, PublicationKind
from speechnexus_backend.speech.infrastructure.catalog_revision_schema import RETIREMENT_TABLE


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


def append_publication(cursor: PublicationCursor, publication: Publication) -> bool:
    """Return whether this call inserted the row; a race resolves to no insert.

    An actual insert fires the catalogue-version trigger in this same
    transaction.  A conflict inserts nothing and therefore leaves the version
    unchanged.
    """
    cursor.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended("
        "'speechnexus:catalog:' || current_schema() || ':' || %s || ':' || %s, 0))",
        (publication.kind, publication.object_id),
    )
    cursor.execute(
        "INSERT INTO speech_publications "
        "(kind, object_id, revision, revision_id, published_at_us) "
        "VALUES (%s, %s, %s, %s, %s) "
        "ON CONFLICT (kind, object_id, revision) DO NOTHING",
        (
            publication.kind,
            publication.object_id,
            str(publication.revision),
            publication.revision_id,
            publication.published_at_us,
        ),
    )
    return cursor.rowcount == 1


def find_publication(
    cursor: PublicationCursor, kind: PublicationKind, object_id: str, revision: int
) -> Publication | None:
    cursor.execute(
        "SELECT kind, object_id, revision, revision_id, published_at_us "
        "FROM speech_publications "
        "WHERE kind = %s AND object_id = %s AND revision = %s",
        (kind, object_id, str(revision)),
    )
    rows = cursor.fetchall()
    if len(rows) > 1:
        raise ValueError("Publication identity is not unique")
    return _publication(rows[0]) if rows else None


def find_current_publication(
    cursor: PublicationCursor, kind: PublicationKind, object_id: str
) -> Publication | None:
    """Return the newest publication number, regardless of retirement state."""
    cursor.execute(
        "SELECT p.kind, p.object_id, p.revision, p.revision_id, p.published_at_us "
        "FROM speech_publications AS p "
        "WHERE p.kind = %s AND p.object_id = %s "
        "AND NOT EXISTS ("
        "SELECT 1 FROM speech_publications AS newer "
        "WHERE newer.kind = p.kind AND newer.object_id = p.object_id "
        "AND newer.revision::numeric > p.revision::numeric"
        ")",
        (kind, object_id),
    )
    rows = cursor.fetchall()
    if len(rows) > 1:
        raise ValueError("Current publication identity is not unique")
    return _publication(rows[0]) if rows else None


def retire_publication(
    cursor: PublicationCursor, publication: Publication, retired_at_us: int
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
        "'speechnexus:catalog:' || current_schema() || ':' || %s || ':' || %s, 0))",
        (publication.kind, publication.object_id),
    )
    current = find_current_publication(cursor, publication.kind, publication.object_id)
    if (
        current is None
        or current.revision != publication.revision
        or current.revision_id != publication.revision_id
    ):
        raise ValueError("Retirement must name the current published revision")
    cursor.execute(
        f"INSERT INTO {RETIREMENT_TABLE} "
        "(kind, object_id, revision, revision_id, retired_at_us) "
        "VALUES (%s, %s, %s, %s, %s) "
        "ON CONFLICT (kind, object_id, revision) DO NOTHING",
        (
            publication.kind,
            publication.object_id,
            str(publication.revision),
            publication.revision_id,
            retired_at_us,
        ),
    )
    return cursor.rowcount == 1


def find_binding_revision(cursor: PublicationCursor, binding_id: str, revision: int) -> str | None:
    """Return the stored revision ID for exactly this binding revision number.

    ``speech_publications`` has no foreign key to the draft history, so the
    write path must prove the target revision exists before promoting it.
    """
    cursor.execute(
        "SELECT revision_id FROM speech_binding_revisions WHERE binding_id = %s AND revision = %s",
        (binding_id, str(revision)),
    )
    rows = cursor.fetchall()
    if len(rows) > 1:
        raise ValueError("Binding revision identity is not unique")
    return rows[0]["revision_id"] if rows else None


def list_publications(cursor: PublicationCursor, kind: PublicationKind) -> list[Publication]:
    cursor.execute(
        "SELECT kind, object_id, revision, revision_id, published_at_us "
        "FROM speech_publications WHERE kind = %s ORDER BY object_id, revision",
        (kind,),
    )
    return [_publication(row) for row in cursor.fetchall()]
