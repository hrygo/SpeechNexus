"""Immutable pronunciation revisions and their publication identity.

A revision is written once. ``publish`` selects whether the same transaction also
appends the publication ledger row and advances the selected head; a revision
without a publication stays a private draft.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Literal

from speechnexus_backend.speech.domain.assets import identifier
from speechnexus_backend.speech.domain.configuration import MAX_REVISION as MAX_REVISION
from speechnexus_backend.speech.domain.configuration import revision_number as revision_number

PronunciationEncoding = Literal["pinyin", "ipa"]
PRONUNCIATION_ENCODINGS: tuple[PronunciationEncoding, ...] = ("pinyin", "ipa")
MAX_PRONUNCIATION_ENTRIES = 10000

# The source permits 10,000 entries, each with 64/256 Unicode characters.
# Even six-byte canonical control escapes plus language/JSON framing fit here.
MAX_PRONUNCIATION_DOCUMENT_BYTES = 32 * 1024 * 1024


def document_digest(raw: bytes) -> str:
    if type(raw) is not bytes or not 1 <= len(raw) <= MAX_PRONUNCIATION_DOCUMENT_BYTES:
        raise ValueError("Invalid pronunciation document size")
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def revision_id_for(pronunciation_id: str, revision: str) -> str:
    """The stable publication identity of one immutable pronunciation revision.

    ``speech_publications`` needs a revision identity that is never generated
    twice for the same revision. The pronunciation table deliberately has no
    separate mutable identity column, so the value is derived from the fixed
    pair; publishing the same revision again computes the same identity.
    """
    identifier(pronunciation_id)
    revision_number(revision)
    digest = hashlib.sha256(
        f"pronunciation-revision\0{pronunciation_id}\0{revision}".encode()
    ).hexdigest()
    return "prv-" + digest


class PronunciationUnavailableError(Exception):
    """Original facts cannot currently be established; never proof of absence."""


@dataclass(frozen=True, slots=True)
class CreatePronunciationDraft:
    pronunciation_id: str | None
    expected_revision: str | None
    publish: bool
    key_digest: str
    intent: bytes
    definition: bytes

    def __post_init__(self) -> None:
        if (self.pronunciation_id is None) != (self.expected_revision is None):
            raise ValueError("Append needs the original family and revision")
        if type(self.publish) is not bool:
            raise ValueError("Invalid pronunciation publication intent")
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
    published: bool = False

    def __post_init__(self) -> None:
        identifier(self.pronunciation_id)
        revision_number(self.revision)
        document_digest(self.definition)
        if type(self.created_at_us) is not int or not 0 <= self.created_at_us <= 253402300799999999:
            raise ValueError("Invalid pronunciation observation time")
        if type(self.published) is not bool:
            raise ValueError("Invalid pronunciation publication state")


@dataclass(frozen=True, slots=True)
class PronunciationSummary:
    """One family head with its immutable facts and that exact revision's state."""

    pronunciation_id: str
    head_revision: str
    encoding: PronunciationEncoding
    entry_count: int
    published: bool

    def __post_init__(self) -> None:
        identifier(self.pronunciation_id)
        revision_number(self.head_revision)
        if self.encoding not in PRONUNCIATION_ENCODINGS:
            raise ValueError("Invalid pronunciation encoding")
        if (
            type(self.entry_count) is not int
            or not 1 <= self.entry_count <= MAX_PRONUNCIATION_ENTRIES
        ):
            raise ValueError("Invalid pronunciation entry count")
        if type(self.published) is not bool:
            raise ValueError("Invalid pronunciation publication state")


@dataclass(frozen=True, slots=True)
class PronunciationSetPage:
    summaries: tuple[PronunciationSummary, ...]
    has_more: bool

    def __post_init__(self) -> None:
        if type(self.has_more) is not bool:
            raise ValueError("Invalid pronunciation page state")
        if self.has_more and not self.summaries:
            raise ValueError("A continuation requires at least one summary")


@dataclass(frozen=True, slots=True)
class PronunciationDraftResult:
    outcome: Literal["created", "replayed", "conflict", "rejected", "unknown"]
    draft: PronunciationDraft | None = None
    error_code: Literal["configuration_changed", "idempotency_conflict"] | None = None

    @property
    def recovery(self) -> Literal["none", "query_original"]:
        return "query_original" if self.outcome == "unknown" else "none"
