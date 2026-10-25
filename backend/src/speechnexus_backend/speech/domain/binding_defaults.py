"""Opaque immutable default declarations; no wire DTO or engine qualification."""

from dataclasses import dataclass
from typing import Literal

from speechnexus_backend.speech.domain.assets import identifier
from speechnexus_backend.speech.domain.binding_drafts import (
    DOCUMENT_VERSION,
    MAX_CREATED_AT_US,
    BindingDraft,
    BindingDraftResult,
)
from speechnexus_backend.speech.domain.configuration import document_digest, revision_number


@dataclass(frozen=True, slots=True)
class BindingDefaultsSnapshot:
    snapshot_id: str
    binding_id: str
    revision: str
    revision_id: str
    parameters: bytes
    created_at_us: int
    document_version: int = DOCUMENT_VERSION
    schema_version: str = "1.0.0"

    def __post_init__(self) -> None:
        for value in (self.snapshot_id, self.binding_id, self.revision_id):
            identifier(value)
        revision_number(self.revision)
        document_digest(self.parameters)
        if type(self.document_version) is not int or self.document_version != DOCUMENT_VERSION:
            raise ValueError("Unsupported default document version")
        if self.schema_version != "1.0.0":
            raise ValueError("Unsupported default schema version")
        if type(self.created_at_us) is not int or not 0 <= self.created_at_us <= MAX_CREATED_AT_US:
            raise ValueError("Invalid default creation time")


@dataclass(frozen=True, slots=True)
class BindingDefaultsDraftResult:
    binding: BindingDraftResult
    snapshot: BindingDefaultsSnapshot | None = None

    def __post_init__(self) -> None:
        if self.snapshot is not None:
            draft = self.binding.draft
            if draft is None or (
                draft.binding_id,
                draft.revision,
                draft.revision_id,
                draft.created_at_us,
            ) != (
                self.snapshot.binding_id,
                self.snapshot.revision,
                self.snapshot.revision_id,
                self.snapshot.created_at_us,
            ):
                raise ValueError("Default snapshot does not identify its original binding")

    @property
    def outcome(self) -> Literal["created", "replayed", "conflict", "unknown"]:
        return self.binding.outcome

    @property
    def draft(self) -> BindingDraft | None:
        return self.binding.draft

    @property
    def recovery(self) -> Literal["none", "query_original"]:
        return self.binding.recovery
