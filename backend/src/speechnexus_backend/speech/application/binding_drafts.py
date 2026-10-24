"""One original-key allocation port; no HTTP, model loading or publication."""

from typing import Protocol

from speechnexus_backend.speech.domain.binding_drafts import (
    BindingDraft,
    BindingDraftResult,
    CreateBindingDraft,
)


class BindingDrafts(Protocol):
    def create(self, command: CreateBindingDraft) -> BindingDraftResult: ...

    def recover(self, command: CreateBindingDraft) -> BindingDraftResult: ...

    def get(self, binding_id: str, revision: str) -> BindingDraft | None: ...
