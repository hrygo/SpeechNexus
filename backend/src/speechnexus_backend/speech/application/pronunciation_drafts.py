"""Private pronunciation storage port; recovery performs no allocation."""

from typing import Protocol

from speechnexus_backend.speech.domain.pronunciation_drafts import (
    CreatePronunciationDraft,
    PronunciationDraft,
    PronunciationDraftResult,
)


class PronunciationDrafts(Protocol):
    def create(self, command: CreatePronunciationDraft) -> PronunciationDraftResult: ...

    def recover(self, command: CreatePronunciationDraft) -> PronunciationDraftResult: ...

    def get(self, pronunciation_id: str, revision: str) -> PronunciationDraft | None: ...
