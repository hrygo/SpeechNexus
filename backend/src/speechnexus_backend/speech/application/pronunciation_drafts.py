"""Private pronunciation storage ports; recovery performs no allocation."""

from typing import Protocol

from speechnexus_backend.speech.domain.pronunciation_drafts import (
    CreatePronunciationDraft,
    PronunciationDraft,
    PronunciationDraftResult,
    PronunciationSetPage,
)


class PronunciationReader(Protocol):
    def get(self, pronunciation_id: str, revision: str) -> PronunciationDraft | None: ...


class PronunciationSets(Protocol):
    """Bounded family-head listing over the same immutable revision facts."""

    def list_sets(self, *, limit: int, after: str | None) -> PronunciationSetPage: ...


class PronunciationWriter(Protocol):
    def create(self, command: CreatePronunciationDraft) -> PronunciationDraftResult: ...


class PronunciationDrafts(PronunciationReader, PronunciationWriter, Protocol):
    """The fixed-revision read plus draft-write port."""

    def create(self, command: CreatePronunciationDraft) -> PronunciationDraftResult: ...

    def recover(self, command: CreatePronunciationDraft) -> PronunciationDraftResult: ...
