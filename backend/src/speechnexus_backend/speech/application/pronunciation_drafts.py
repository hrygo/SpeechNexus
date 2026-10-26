"""Private pronunciation storage port; recovery performs no allocation."""

from typing import Protocol

from speechnexus_backend.speech.domain.pronunciation_drafts import (
    CreatePronunciationDraft,
    PronunciationDraft,
    PronunciationDraftResult,
)


class PronunciationReader(Protocol):
    def get(self, pronunciation_id: str, revision: str) -> PronunciationDraft | None: ...


class PronunciationDrafts(PronunciationReader, Protocol):
    def create(self, command: CreatePronunciationDraft) -> PronunciationDraftResult: ...

    def recover(self, command: CreatePronunciationDraft) -> PronunciationDraftResult: ...
