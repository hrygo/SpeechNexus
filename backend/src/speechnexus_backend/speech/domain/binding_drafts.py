"""Draft binding identity and immutable history; no publication or runtime facts."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from speechnexus_backend.speech.domain.assets import identifier
from speechnexus_backend.speech.domain.configuration import document_digest, revision_number

DOCUMENT_VERSION = 1
MAX_CREATED_AT_US = 253402300799999999


class BindingUnavailableError(Exception):
    """An original result or fixed revision cannot be established safely."""


@dataclass(frozen=True, slots=True)
class CreateBindingDraft:
    binding_id: str
    key_digest: str
    intent: bytes
    definition: bytes
    document_version: int = DOCUMENT_VERSION
    parameter_defaults: bytes | None = None

    def __post_init__(self) -> None:
        identifier(self.binding_id)
        if re.fullmatch(r"sha256:[0-9a-f]{64}", self.key_digest) is None:
            raise ValueError("Invalid binding command identity")
        if type(self.document_version) is not int or self.document_version != DOCUMENT_VERSION:
            raise ValueError("Unsupported binding document version")
        document_digest(self.intent)
        document_digest(self.definition)
        if self.parameter_defaults is not None:
            document_digest(self.parameter_defaults)

    @property
    def request_digest(self) -> str:
        return document_digest(self.intent)


@dataclass(frozen=True, slots=True)
class BindingDraft:
    binding_id: str
    revision: str
    revision_id: str
    definition: bytes
    created_at_us: int
    document_version: int = DOCUMENT_VERSION

    def __post_init__(self) -> None:
        identifier(self.binding_id)
        revision_number(self.revision)
        identifier(self.revision_id)
        document_digest(self.definition)
        if type(self.created_at_us) is not int or not 0 <= self.created_at_us <= MAX_CREATED_AT_US:
            raise ValueError("Invalid binding creation time")
        if type(self.document_version) is not int or self.document_version != DOCUMENT_VERSION:
            raise ValueError("Unsupported binding document version")


@dataclass(frozen=True, slots=True)
class BindingDraftResult:
    outcome: Literal["created", "replayed", "conflict", "unknown"]
    draft: BindingDraft | None = None
    error_code: Literal["idempotency_conflict"] | None = None

    def __post_init__(self) -> None:
        if self.outcome in {"created", "replayed"}:
            if self.draft is None or self.error_code is not None:
                raise ValueError("Invalid binding creation result")
        elif self.outcome == "conflict":
            if self.draft is not None or self.error_code != "idempotency_conflict":
                raise ValueError("Invalid binding conflict result")
        elif self.outcome != "unknown" or self.draft is not None or self.error_code is not None:
            raise ValueError("Invalid binding unknown result")

    @property
    def recovery(self) -> Literal["none", "query_original"]:
        return "query_original" if self.outcome == "unknown" else "none"
