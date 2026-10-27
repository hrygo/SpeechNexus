"""Read committed artifacts through ownership-scoped metadata and object ports."""

from __future__ import annotations

from collections.abc import Generator
from contextlib import AbstractContextManager
from dataclasses import dataclass, replace
from typing import Protocol

from speechnexus_backend.speech.domain.artifacts import (
    ArtifactError,
    ArtifactRecord,
    artifact_range,
    download_filename,
)
from speechnexus_backend.speech.domain.assets import identifier


class ContentReader(Protocol):
    def read(self, size: int) -> bytes: ...


class ArtifactMetadata(Protocol):
    def get(self, artifact_id: str, workspace_id: str) -> tuple[ArtifactRecord | None, int]: ...


class ArtifactObjects(Protocol):
    def open(
        self, artifact: ArtifactRecord, offset: int, length: int
    ) -> AbstractContextManager[ContentReader]: ...


@dataclass(frozen=True, slots=True)
class ArtifactDownload:
    artifact: ArtifactRecord
    offset: int
    length: int
    partial: bool

    @property
    def etag(self) -> str:
        return '"' + self.artifact.sha256 + '"'


class ArtifactService:
    """Public metadata/content decisions; object I/O never runs during a transaction."""

    def __init__(self, metadata: ArtifactMetadata, objects: ArtifactObjects, workspace_id: str):
        identifier(workspace_id)
        self._metadata, self._objects = metadata, objects
        self.workspace_id = workspace_id

    def metadata(self, artifact_id: str) -> ArtifactRecord:
        artifact, _ = self._load(artifact_id)
        return artifact

    def _load(self, artifact_id: str) -> tuple[ArtifactRecord, int]:
        identifier(artifact_id)
        artifact, now = self._metadata.get(artifact_id, self.workspace_id)
        if artifact is None:
            raise ArtifactError("artifact_not_found")
        if (
            artifact.status == "ready"
            and artifact.expires_at_us is not None
            and artifact.expires_at_us <= now
        ):
            artifact = replace(artifact, status="expired")
        return artifact, now

    def download(self, artifact_id: str, range_header: str | None = None) -> ArtifactDownload:
        artifact, now = self._load(artifact_id)
        if not artifact.content_available(now):
            raise ArtifactError("artifact_unavailable")
        offset, length, partial = artifact_range(range_header, artifact.bytes)
        return ArtifactDownload(artifact, offset, length, partial)

    def content(self, download: ArtifactDownload) -> Generator[bytes]:
        remaining = download.length
        try:
            with self._objects.open(download.artifact, download.offset, download.length) as reader:
                while remaining:
                    chunk = reader.read(min(65536, remaining))
                    if not chunk or len(chunk) > remaining:
                        raise ArtifactError("artifact_unavailable")
                    remaining -= len(chunk)
                    yield chunk
        except ArtifactError:
            raise
        except Exception:
            raise ArtifactError("artifact_storage_unavailable") from None

    @staticmethod
    def filename(artifact: ArtifactRecord) -> str:
        return download_filename(artifact)


__all__ = [
    "ArtifactDownload",
    "ArtifactMetadata",
    "ArtifactObjects",
    "ArtifactService",
    "ContentReader",
]
