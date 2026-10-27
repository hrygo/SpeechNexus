"""Explicit publication of prepared revisions; drafts never become public alone."""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

from speechnexus_backend.speech.domain.publications import (
    Publication,
    PublicationKind,
    catalog_digest,
)


class PublicationConflictError(ValueError):
    """The same revision number already names another published document."""


class PublicationAbsentError(LookupError):
    """No publication exists for the requested identity."""


class PublicationStore(Protocol):
    def append(self, publication: Publication) -> bool:
        """Insert one publication; return whether this call created the row."""
        ...

    def get(self, kind: PublicationKind, object_id: str, revision: int) -> Publication | None: ...

    def list_kind(self, kind: PublicationKind) -> list[Publication]: ...


class Publications:
    """Append-only publication commands plus the catalog digest they define."""

    def __init__(
        self,
        store: PublicationStore,
        *,
        now_us: Callable[[], int],
    ) -> None:
        self._store = store
        self._now_us = now_us

    def publish(
        self,
        kind: PublicationKind,
        object_id: str,
        revision: int,
        revision_id: str,
    ) -> tuple[Publication, bool]:
        """Publish once; an identical republication is reported, not rewritten."""
        existing = self._store.get(kind, object_id, revision)
        if existing is not None:
            if existing.revision_id != revision_id:
                raise PublicationConflictError("publication revision already names a document")
            return existing, False
        publication = Publication(
            kind=kind,
            object_id=object_id,
            revision=revision,
            revision_id=revision_id,
            published_at_us=self._now_us(),
        )
        if self._store.append(publication):
            return publication, True
        # A concurrent writer inserted this revision first; report the stored fact.
        stored = self._store.get(kind, object_id, revision)
        if stored is None or stored.revision_id != revision_id:
            raise PublicationConflictError("publication revision already names a document")
        return stored, False

    def require(self, kind: PublicationKind, object_id: str, revision: int) -> Publication:
        publication = self._store.get(kind, object_id, revision)
        if publication is None:
            raise PublicationAbsentError("revision is not published")
        return publication

    def current_catalog_digest(self) -> str:
        return catalog_digest(
            item for kind in ("profile", "binding", "voice") for item in self._store.list_kind(kind)
        )
