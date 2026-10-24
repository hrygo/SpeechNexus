"""Narrow configuration ports; recovery never creates or publishes a revision."""

from __future__ import annotations

from typing import Protocol

from speechnexus_backend.speech.domain.profile_drafts import (
    CreateProfileDraft,
    ProfileDraft,
    ProfileDraftResult,
)


class ProfileDrafts(Protocol):
    def create(self, command: CreateProfileDraft) -> ProfileDraftResult: ...

    def recover(self, command: CreateProfileDraft) -> ProfileDraftResult: ...

    def get(self, profile_id: str, revision: str) -> ProfileDraft | None: ...
