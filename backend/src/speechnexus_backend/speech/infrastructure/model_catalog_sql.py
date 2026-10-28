"""Read published bindings with the exact document their revision names."""

from __future__ import annotations

from typing import Any, Protocol

from speechnexus_backend.speech.infrastructure.catalog_revision_schema import (
    RETIREMENT_TABLE,
    STATE_TABLE,
)


class CatalogCursor(Protocol):
    def execute(self, query: str, parameters: tuple[object, ...] = ()) -> object: ...

    def fetchall(self) -> list[dict[str, Any]]: ...


def list_published_bindings(cursor: CatalogCursor) -> list[dict[str, Any]]:
    """Read the published-only view and return each binding's newest revision.

    Matching ``revision_id`` as well as the number keeps the catalogue honest when
    a publication and the stored document ever disagree. A newer publication that
    does not name a stored document hides older revisions rather than silently
    falling back and presenting a stale model as current. The revision document
    comes only from the security-barrier view, so a leaked reader credential
    cannot select unpublished draft revisions.

    A retirement is a visibility tombstone, not a rewrite of the publication row.
    It hides its exact revision without selecting an older revision in its place.
    """
    cursor.execute(
        "SELECT p.kind, p.object_id, p.revision, p.revision_id, p.published_at_us, "
        "v.definition_json "
        "FROM speech_publications AS p "
        "JOIN speech_published_binding_revisions AS v "
        "  ON v.binding_id = p.object_id "
        " AND v.revision = p.revision "
        " AND v.revision_id = p.revision_id "
        "WHERE p.kind = 'binding' "
        "AND NOT EXISTS ("
        "SELECT 1 FROM speech_publications AS newer "
        "WHERE newer.kind = 'binding' AND newer.object_id = p.object_id "
        "AND newer.revision::numeric > p.revision::numeric"
        ") "
        "AND NOT EXISTS ("
        f"SELECT 1 FROM {RETIREMENT_TABLE} AS retired "
        "WHERE retired.kind = 'binding' AND retired.object_id = p.object_id "
        "AND retired.revision = p.revision AND retired.revision_id = p.revision_id"
        ") "
        "ORDER BY p.published_at_us DESC, p.object_id"
    )
    return cursor.fetchall()


def read_catalog_state(cursor: CatalogCursor) -> dict[str, Any]:
    """Return the transactional catalogue version and its latest fact time.

    The state row advances inside the same transaction that inserts a visible
    publication or retirement.  It is not a row count, so removing a model from
    the default catalogue still changes the version and every ETag that covers
    it.  The migration seeds the counter at ``COUNT(*) + 1`` so previously
    observed revisions never move backwards.  A single-row state table also lets
    the read-only catalogue role avoid sequence privileges.
    """
    cursor.execute(
        "SELECT revision AS catalog_revision, generated_at_us "
        f"FROM {STATE_TABLE} WHERE singleton IS TRUE"
    )
    rows = cursor.fetchall()
    if len(rows) != 1:
        raise ValueError("Publication catalog state is not unique")
    return rows[0]


def read_catalog_snapshot(cursor: CatalogCursor) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Read bindings and the counter in one repeatable-read transaction."""
    return list_published_bindings(cursor), read_catalog_state(cursor)
