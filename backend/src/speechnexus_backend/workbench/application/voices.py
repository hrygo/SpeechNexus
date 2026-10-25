"""Application boundary for the published voice catalogue: no HTTP or database state.

Casting a book to a voice needs the deployment's real published revisions, not a client's
claim about one. The speech base owns design, cloning, validation, publication and
retirement; this service only reads those published facts back through the base's protocol,
and never publishes, retires or promotes anything on the workbench's behalf.
"""

from __future__ import annotations

from typing import Protocol

from speechnexus_backend.workbench.domain.voices import (
    VoiceCataloguePage,
    VoiceRevision,
    validate_catalogue_scope,
    validate_voice_reference,
)

DEFAULT_LIST_LIMIT = 50


class VoiceCatalogueUnavailableError(Exception):
    """The speech base's published catalogue could not be read right now."""

    def __init__(self) -> None:
        super().__init__("workbench voice catalogue unavailable")


class VoiceCatalogue(Protocol):
    """Read-only base port; a missing revision raises ResourceNotFoundError."""

    async def list_summaries(
        self, *, workspace_id: str, language: str | None, limit: int, after: str | None
    ) -> VoiceCataloguePage:
        """List published revisions, forwarding the base's own opaque cursor."""
        ...

    async def get_revision(self, *, voice_id: str, revision: str) -> VoiceRevision:
        """Return one immutable published revision, or raise ResourceNotFoundError."""
        ...


class WorkbenchVoices:
    """Deployment-scoped read-through of the base's authoritative voice catalogue."""

    def __init__(self, catalogue: VoiceCatalogue) -> None:
        self._catalogue = catalogue

    async def list_voices(
        self,
        *,
        workspace_id: str,
        language: str | None = None,
        limit: int = DEFAULT_LIST_LIMIT,
        after: str | None = None,
    ) -> VoiceCataloguePage:
        validate_catalogue_scope(
            workspace_id=workspace_id, language=language, limit=limit, after=after
        )
        return await self._catalogue.list_summaries(
            workspace_id=workspace_id, language=language, limit=limit, after=after
        )

    async def get_revision(self, *, voice_id: str, revision: str) -> VoiceRevision:
        validate_voice_reference(voice_id=voice_id, revision=revision)
        return await self._catalogue.get_revision(voice_id=voice_id, revision=revision)
