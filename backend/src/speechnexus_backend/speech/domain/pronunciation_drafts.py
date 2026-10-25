"""Immutable pronunciation drafts; no publication or execution authority."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Literal

from speechnexus_backend.speech.domain.assets import identifier
from speechnexus_backend.speech.domain.configuration import MAX_REVISION as MAX_REVISION
from speechnexus_backend.speech.domain.configuration import revision_number as revision_number

# The source permits 10,000 entries, each with 64/256 Unicode characters.
# Even six-byte canonical control escapes plus language/JSON framing fit here.
MAX_PRONUNCIATION_DOCUMENT_BYTES = 32 * 1024 * 1024


def document_digest(raw: bytes) -> str:
    if type(raw) is not bytes or not 1 <= len(raw) <= MAX_PRONUNCIATION_DOCUMENT_BYTES:
        raise ValueError("Invalid pronunciation document size")
    return "sha256:" + hashlib.sha256(raw).hexdigest()


class PronunciationUnavailableError(Exception):
    """Original facts cannot currently be established; never proof of absence."""


@dataclass(frozen=True, slots=True)
class CreatePronunciationDraft:
    pronunciation_id: str | None
    expected_revision: str | None
    key_digest: str
    intent: bytes
    definition: bytes

    def __post_init__(self) -> None:
        if (self.pronunciation_id is None) != (self.expected_revision is None):
            raise ValueError("Append needs the original family and revision")
        if self.pronunciation_id is not None:
            identifier(self.pronunciation_id)
        if self.expected_revision is not None:
            revision_number(self.expected_revision)
        if re.fullmatch(r"sha256:[0-9a-f]{64}", self.key_digest) is None:
            raise ValueError("Invalid pronunciation command identity")
        document_digest(self.intent)
        document_digest(self.definition)

    @property
    def request_digest(self) -> str:
        return document_digest(self.intent)


@dataclass(frozen=True, slots=True)
class PronunciationDraft:
    pronunciation_id: str
    revision: str
    definition: bytes
    created_at_us: int

    def __post_init__(self) -> None:
        identifier(self.pronunciation_id)
        revision_number(self.revision)
        document_digest(self.definition)
        if type(self.created_at_us) is not int or not 0 <= self.created_at_us <= 253402300799999999:
            raise ValueError("Invalid pronunciation observation time")


@dataclass(frozen=True, slots=True)
class PronunciationDraftResult:
    outcome: Literal["created", "replayed", "conflict", "rejected", "unknown"]
    draft: PronunciationDraft | None = None
    error_code: Literal["configuration_changed", "idempotency_conflict"] | None = None

    @property
    def recovery(self) -> Literal["none", "query_original"]:
        return "query_original" if self.outcome == "unknown" else "none"
