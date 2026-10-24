"""Immutable configuration values, separate from publication and qualification."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from speechnexus_backend.speech.domain.assets import identifier
from speechnexus_backend.speech.domain.configuration import MAX_DOCUMENT_BYTES as MAX_DOCUMENT_BYTES
from speechnexus_backend.speech.domain.configuration import MAX_REVISION as MAX_REVISION
from speechnexus_backend.speech.domain.configuration import document_digest as document_digest
from speechnexus_backend.speech.domain.configuration import revision_number as revision_number


class ProfileUnavailableError(Exception):
    """The original configuration result cannot be established safely."""


@dataclass(frozen=True, slots=True)
class CreateProfileDraft:
    profile_id: str
    expected_latest_revision: str | None
    key_digest: str
    intent: bytes
    definition: bytes

    def __post_init__(self) -> None:
        identifier(self.profile_id)
        if self.expected_latest_revision is not None:
            revision_number(self.expected_latest_revision)
        if re.fullmatch(r"sha256:[0-9a-f]{64}", self.key_digest) is None:
            raise ValueError("Invalid profile command identity")
        document_digest(self.intent)
        document_digest(self.definition)

    @property
    def request_digest(self) -> str:
        return document_digest(self.intent)


@dataclass(frozen=True, slots=True)
class ProfileDraft:
    profile_id: str
    revision: str
    definition: bytes
    created_at_us: int

    def __post_init__(self) -> None:
        identifier(self.profile_id)
        revision_number(self.revision)
        document_digest(self.definition)
        if type(self.created_at_us) is not int or self.created_at_us < 0:
            raise ValueError("Invalid profile observation time")


@dataclass(frozen=True, slots=True)
class ProfileDraftResult:
    outcome: Literal["created", "replayed", "conflict", "rejected", "unknown"]
    draft: ProfileDraft | None = None
    error_code: Literal["configuration_changed", "idempotency_conflict"] | None = None

    @property
    def recovery(self) -> Literal["none", "query_original"]:
        return "query_original" if self.outcome == "unknown" else "none"
