"""Narrow lifecycle and published catalogue port, alongside immutable draft allocation."""

from typing import Protocol

from speechnexus_backend.speech.application.profile_drafts import ProfileDrafts
from speechnexus_backend.speech.domain.profile_lifecycle import ProfileLifecycleCommand
from speechnexus_backend.speech.domain.profile_publications import (
    ProfileLifecycleResult,
    PublishedProfile,
)


class ProfilePublications(ProfileDrafts, Protocol):
    def mutate(self, command: ProfileLifecycleCommand) -> ProfileLifecycleResult: ...

    def get_publication(self, profile_id: str, revision: str) -> PublishedProfile | None: ...

    def list_publications(
        self, *, limit: int = 50, after: tuple[str, str] | None = None
    ) -> tuple[list[tuple[PublishedProfile, str | None]], bool]: ...
