"""Read published bindings with the exact document their revision names."""

from __future__ import annotations

from typing import Any, Protocol


class CatalogCursor(Protocol):
    def execute(self, query: str, parameters: tuple[object, ...] = ()) -> object: ...

    def fetchall(self) -> list[dict[str, Any]]: ...


def list_published_bindings(cursor: CatalogCursor) -> list[dict[str, Any]]:
    """Join on the full revision identity; a stale publication cannot list a model.

    Matching ``revision_id`` as well as the number keeps the catalogue honest when
    a publication and the stored document ever disagree.
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
        "ORDER BY p.published_at_us, p.object_id"
    )
    return cursor.fetchall()
