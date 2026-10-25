"""Private lifecycle intent and evidence observations, never publication authority."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal

from speechnexus_backend.speech.domain.assets import identifier
from speechnexus_backend.speech.domain.configuration import document_digest, revision_number

type ProfileLifecycleOperation = Literal["publish", "activate"]


def _digest(value: str) -> None:
    if not isinstance(value, str) or re.fullmatch(r"sha256:[0-9a-f]{64}", value) is None:
        raise ValueError("Invalid profile lifecycle digest")


@dataclass(frozen=True, slots=True)
class ProfileLifecycleCommand:
    operation: ProfileLifecycleOperation
    profile_id: str
    revision: str
    key_digest: str
    intent: bytes = field(repr=False)

    def __post_init__(self) -> None:
        if self.operation not in ("publish", "activate"):
            raise ValueError("Invalid profile lifecycle operation")
        identifier(self.profile_id)
        revision_number(self.revision)
        _digest(self.key_digest)
        document_digest(self.intent)

    @property
    def request_digest(self) -> str:
        return document_digest(self.intent)


@dataclass(frozen=True, slots=True)
class ProfilePublicationObservation:
    """Bound provider observation, not a signed token or permission to commit.

    P2 must look up the original command result first and recheck authoritative
    facts within its publication/CAS boundary. Construction proves only shape.
    """

    profile_id: str
    revision: str
    definition_digest: str
    intent_digest: str
    authority_digest: str

    def __post_init__(self) -> None:
        identifier(self.profile_id)
        revision_number(self.revision)
        for value in (self.definition_digest, self.intent_digest, self.authority_digest):
            _digest(value)


class ProfilePublicationIneligibleError(ValueError):
    """A trusted provider established ineligibility, distinct from unreadable facts."""
