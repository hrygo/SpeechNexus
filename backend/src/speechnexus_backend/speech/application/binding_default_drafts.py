"""A-owned original-key transaction for a binding and its optional default snapshot."""

from typing import Protocol

from speechnexus_backend.speech.domain.binding_defaults import (
    BindingDefaultsDraftResult,
    BindingDefaultsSnapshot,
)
from speechnexus_backend.speech.domain.binding_drafts import BindingDraft, CreateBindingDraft


class BindingDefaultDrafts(Protocol):
    def create(self, command: CreateBindingDraft) -> BindingDefaultsDraftResult: ...

    def recover(self, command: CreateBindingDraft) -> BindingDefaultsDraftResult: ...

    def get(self, binding_id: str, revision: str) -> BindingDraft | None: ...

    def get_defaults(self, binding_id: str, revision: str) -> BindingDefaultsSnapshot | None: ...
