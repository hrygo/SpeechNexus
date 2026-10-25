"""Read-only fixed-revision UTF-8 downloads, independent of HTTP/export publication."""

from __future__ import annotations

from dataclasses import dataclass, field
from hashlib import sha256
from typing import Literal, Protocol

from speechnexus_backend.workbench.application.versions import ResourceNotFoundError
from speechnexus_backend.workbench.domain.versions import (
    RevisionStatus,
    SpeechSource,
    TranscriptRevision,
)


class TranscriptRevisionReader(Protocol):
    async def get_revision(self, transcript_id: str, revision: int) -> TranscriptRevision:
        """Return one immutable, visible revision; release its transaction before returning."""
        ...


class TranscriptExportError(Exception):
    """Safe local errors; a future transport maps them through its frozen contract."""

    def __init__(
        self,
        code: Literal[
            "invalid_revision", "invalid_content", "revision_mismatch", "source_unavailable"
        ],
    ) -> None:
        self.code = code
        super().__init__(f"workbench text export {code}")


@dataclass(frozen=True, slots=True)
class TranscriptTextExport:
    """Private download snapshot, not a wire DTO or a persisted ExportArtifact."""

    transcript_id: str
    revision: int
    status: RevisionStatus
    source_revision: int | None
    source: SpeechSource | None
    content: bytes = field(repr=False)
    sha256: str

    @property
    def content_length(self) -> int:
        return len(self.content)

    @property
    def filename(self) -> str:
        return "speech-transcript.txt"

    @property
    def media_type(self) -> str:
        return "text/plain; charset=utf-8"


class TranscriptTextExports:
    def __init__(self, reader: TranscriptRevisionReader) -> None:
        self._reader = reader

    async def export_revision(self, transcript_id: str, revision: int) -> TranscriptTextExport:
        if (
            isinstance(revision, bool)
            or not isinstance(revision, int)
            or not 1 <= revision < 10**19
        ):
            raise TranscriptExportError("invalid_revision")
        try:
            snapshot = await self._reader.get_revision(transcript_id, revision)
        except ResourceNotFoundError:
            raise
        except Exception:
            raise TranscriptExportError("source_unavailable") from None
        if snapshot.transcript_id != transcript_id or snapshot.number != revision:
            raise TranscriptExportError("revision_mismatch")
        text = snapshot.text
        if len(text) > 1_000_000 or "\x00" in text:
            raise TranscriptExportError("invalid_content")
        try:
            # The reader has already released its transaction; no writes or source HTTP here.
            content = text.encode("utf-8")
        except UnicodeEncodeError:
            raise TranscriptExportError("invalid_content") from None
        if len(content) > 4_000_000:
            raise TranscriptExportError("invalid_content")
        return TranscriptTextExport(
            transcript_id=transcript_id,
            revision=revision,
            status=snapshot.status,
            source_revision=snapshot.source_revision,
            source=snapshot.source,
            content=content,
            sha256="sha256:" + sha256(content).hexdigest(),
        )
