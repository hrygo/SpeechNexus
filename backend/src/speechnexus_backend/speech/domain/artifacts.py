"""Committed artifact facts; object identities and storage SDKs stay outside."""

from __future__ import annotations

import builtins
import re
from dataclasses import dataclass
from typing import Literal

from speechnexus_backend.speech.domain.assets import (
    DIGEST,
    IDENTIFIER,
    counter,
    hash_bytes,
    identifier,
)

type ArtifactKind = Literal[
    "transcript", "audio", "preview", "validation_report", "export_manifest"
]
type ArtifactStatus = Literal["ready", "expired", "deleted"]

ARTIFACT_KINDS = frozenset(
    {"transcript", "audio", "preview", "validation_report", "export_manifest"}
)
ARTIFACT_STATUSES = frozenset({"ready", "expired", "deleted"})
_MEDIA_TYPE = re.compile(r"[!-~](?:[ -~]{0,126}[!-~])?\Z")
_STORAGE_VERSION = re.compile(r"[^\x00-\x1f\x7f]{1,1024}\Z")


class ArtifactError(Exception):
    """Safe artifact error; never carries object keys, endpoints or SDK text."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class ArtifactRecord:
    artifact_id: str
    workspace_id: str
    kind: ArtifactKind
    status: ArtifactStatus
    media_type: str
    bytes: int
    sha256: str
    receipt_id: str | None
    source_operation_id: str | None
    created_at_us: int
    expires_at_us: int | None
    object_key: str = ""
    storage_version_id: str = ""

    def __post_init__(self) -> None:
        identifier(self.artifact_id)
        identifier(self.workspace_id)
        if self.kind not in ARTIFACT_KINDS:
            raise ValueError("Invalid artifact kind")
        if self.status not in ARTIFACT_STATUSES:
            raise ValueError("Invalid artifact status")
        if not isinstance(self.media_type, str) or not _MEDIA_TYPE.fullmatch(self.media_type):
            raise ValueError("Invalid artifact media type")
        counter(self.bytes, minimum=1)
        if not DIGEST.fullmatch(self.sha256):
            raise ValueError("Invalid artifact digest")
        for value in (self.receipt_id, self.source_operation_id):
            if value is not None:
                identifier(value)
        counter(self.created_at_us)
        if self.expires_at_us is not None and self.expires_at_us <= self.created_at_us:
            raise ValueError("Artifact expiry must follow creation")
        if not isinstance(self.object_key, str) or not 1 <= len(self.object_key) <= 512:
            raise ValueError("Invalid artifact object identity")
        if not isinstance(self.storage_version_id, str) or not _STORAGE_VERSION.fullmatch(
            self.storage_version_id
        ):
            raise ValueError("Invalid artifact storage version")

    def content_available(self, now_us: int) -> bool:
        return self.status == "ready" and (
            self.expires_at_us is None or self.expires_at_us > now_us
        )


@dataclass(frozen=True, slots=True)
class ArtifactObjectWrite:
    """Bytes and identity of one immutable object before its storage version exists."""

    artifact_id: str
    workspace_id: str
    object_key: str
    media_type: str
    bytes: int
    sha256: str
    body: builtins.bytes

    def __post_init__(self) -> None:
        identifier(self.artifact_id)
        identifier(self.workspace_id)
        if not isinstance(self.object_key, str) or not 1 <= len(self.object_key) <= 512:
            raise ValueError("Invalid artifact object identity")
        if not isinstance(self.media_type, str) or not _MEDIA_TYPE.fullmatch(self.media_type):
            raise ValueError("Invalid artifact media type")
        counter(self.bytes, minimum=1)
        if not DIGEST.fullmatch(self.sha256):
            raise ValueError("Invalid artifact digest")
        if not isinstance(self.body, bytes):
            raise ValueError("Invalid artifact body")
        if len(self.body) != self.bytes or hash_bytes(self.body) != self.sha256:
            raise ValueError("Artifact body disagrees with its size or digest")


def artifact_range(value: str | None, size: int) -> tuple[int, int, bool]:
    """One bounded byte range; malformed, multiple and unsatisfiable values fail closed."""
    counter(size, minimum=1)
    if value is None:
        return 0, size, False
    if len(value) > 128 or not (match := re.fullmatch(r"bytes=([0-9]*)-([0-9]*)", value)):
        raise ArtifactError("artifact_range_not_satisfiable")
    first, last = match.groups()
    if not first:
        suffix = int(last) if last else 0
        if suffix < 1:
            raise ArtifactError("artifact_range_not_satisfiable")
        start = max(0, size - suffix)
        return start, size - start, True
    start = int(first)
    end = int(last) if last else size - 1
    if start >= size or end < start:
        raise ArtifactError("artifact_range_not_satisfiable")
    end = min(end, size - 1)
    return start, end - start + 1, True


def download_filename(artifact: ArtifactRecord) -> str:
    """A server-derived basename: no client path, slash or control character."""
    media_type = artifact.media_type.partition(";")[0].strip().lower()
    extension = {
        "application/json": "json",
        "application/octet-stream": "bin",
        "application/x-subrip": "srt",
        "audio/flac": "flac",
        "audio/mpeg": "mp3",
        "audio/ogg": "ogg",
        "audio/pcm": "pcm",
        "audio/wav": "wav",
        "text/plain": "txt",
        "text/vtt": "vtt",
    }.get(media_type, "bin")
    if not IDENTIFIER.fullmatch(artifact.artifact_id):
        raise ArtifactError("artifact_unavailable")
    return artifact.artifact_id + "." + extension


__all__ = [
    "ARTIFACT_KINDS",
    "ARTIFACT_STATUSES",
    "ArtifactError",
    "ArtifactKind",
    "ArtifactObjectWrite",
    "ArtifactRecord",
    "ArtifactStatus",
    "artifact_range",
    "download_filename",
]
