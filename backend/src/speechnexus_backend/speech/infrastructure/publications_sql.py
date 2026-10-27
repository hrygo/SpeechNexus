"""Insert-only publication statements; reads never create or repair a record."""

from __future__ import annotations

from typing import Any, Protocol

from speechnexus_backend.speech.domain.publications import Publication, PublicationKind


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
    """Return whether this call inserted the row; a race resolves to no insert."""
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
