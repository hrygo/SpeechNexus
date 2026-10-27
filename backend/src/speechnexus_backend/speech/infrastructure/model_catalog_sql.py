"""Read published bindings with the exact document their revision names."""

from __future__ import annotations

from typing import Any, Protocol


class CatalogCursor(Protocol):
    def execute(self, query: str, parameters: tuple[object, ...] = ()) -> object: ...

    def fetchall(self) -> list[dict[str, Any]]: ...


def list_published_bindings(cursor: CatalogCursor) -> list[dict[str, Any]]:
    """Join on the full revision identity and return only each binding's newest revision.

    Matching ``revision_id`` as well as the number keeps the catalogue honest when
    a publication and the stored document ever disagree. A newer publication that
    does not name a stored document hides older revisions rather than silently
    falling back and presenting a stale model as current.
    """
    cursor.execute(
        "SELECT p.kind, p.object_id, p.revision, p.revision_id, p.published_at_us, "
        "r.definition_json "
        "FROM speech_publications AS p "
        "JOIN speech_binding_revisions AS r "
        "  ON r.binding_id = p.object_id "
        " AND r.revision = p.revision "
        " AND r.revision_id = p.revision_id "
        "WHERE p.kind = 'binding' "
        "AND NOT EXISTS ("
        "SELECT 1 FROM speech_publications AS newer "
        "WHERE newer.kind = 'binding' AND newer.object_id = p.object_id "
        "AND newer.revision::numeric > p.revision::numeric"
        ") "
        "ORDER BY p.published_at_us DESC, p.object_id"
    )
    return cursor.fetchall()


def read_catalog_state(cursor: CatalogCursor) -> dict[str, Any]:
    """Return the monotonic publication counter and its latest fact time.

    Publication rows are insert-only.  The count therefore never decreases and
    advances by exactly one for each new visible revision, including profile and
    voice publications that do not otherwise alter the model list.  Adding one
    keeps the empty catalog a valid contract ``Revision`` (the contract requires
    revisions to start at one).
    """
    cursor.execute(
        "SELECT COUNT(*) + 1 AS catalog_revision, "
        "COALESCE(MAX(published_at_us), 0) AS generated_at_us "
        "FROM speech_publications"
    )
    rows = cursor.fetchall()
    if len(rows) != 1:
        raise ValueError("Publication catalog state is not unique")
    return rows[0]


def read_catalog_snapshot(cursor: CatalogCursor) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Read bindings and the counter in one repeatable-read transaction."""
    return list_published_bindings(cursor), read_catalog_state(cursor)
