"""Public-speech read boundaries; no transport DTOs, object or database state here."""

from __future__ import annotations

from dataclasses import dataclass
from typing import BinaryIO, Literal, Protocol

type SourceErrorCode = Literal[
    "source_unavailable",
    "source_invalid",
    "source_too_large",
    "source_unsupported",
    "source_expired",
]


class SpeechSourceError(Exception):
    """Stable public source failure; the code names a frozen ErrorResponse code."""

    def __init__(self, code: SourceErrorCode) -> None:
        super().__init__(code)
        self.code = code


@dataclass(slots=True)
class ArtifactCopy:
    """A bounded, digest-verified artifact body owned by exactly one caller.

    ``body`` is positioned at the first byte, is never shared, and stays private to the
    deployment; the caller closes it on every path, including a failed object write.
    """

    artifact_id: str
    receipt_id: str
    media_type: str
    bytes: int
    sha256: str
    body: BinaryIO

    def close(self) -> None:
        self.body.close()


class TranscriptSourceReader(Protocol):
    async def read(self, *, artifact_id: str, receipt_id: str, workspace_id: str) -> str:
        """Verify a ready, unexpired transcript's identity, source receipt and content.

        Return only bounded text copied from the specified workspace; no persistence,
        transaction, inference or source mutation belongs in this adapter.
        """
        ...


class SpeechArtifactSource(Protocol):
    async def copy(self, *, artifact_id: str, receipt_id: str, workspace_id: str) -> ArtifactCopy:
        """Copy artifact bytes after verifying identity, receipt, size and digest.

        The adapter reads only the public artifact routes, never mutates the source Job
        or artifact state, and returns bytes rather than a path or a second URL.
        """
        ...
