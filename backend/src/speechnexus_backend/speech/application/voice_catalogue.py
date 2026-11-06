"""Read-only port over existing published voice revisions."""

from typing import Any, Protocol


class VoiceCatalogue(Protocol):
    def list_voices(
        self, *, language: str | None, limit: int, cursor: str | None
    ) -> dict[str, Any]: ...

    def get_revision(self, voice_id: str, revision: str) -> dict[str, Any] | None: ...
