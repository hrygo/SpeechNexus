"""Bounded PG lifecycle port, with no enabled production qualification provider."""

from speechnexus_backend.speech.application.profile_lifecycle import (
    ProfilePublicationVerifier,
    UnavailableProfilePublicationVerifier,
    check_lifecycle_command,
)
from speechnexus_backend.speech.domain.profile_drafts import ProfileUnavailableError
from speechnexus_backend.speech.domain.profile_lifecycle import ProfileLifecycleCommand
from speechnexus_backend.speech.domain.profile_publications import (
    ProfileLifecycleResult,
    PublishedProfile,
)
from speechnexus_backend.speech.infrastructure.postgres_pool import SpeechPool
from speechnexus_backend.speech.infrastructure.profile_drafts_postgres import (
    PostgresProfileDrafts,
    _Cursor,
)
from speechnexus_backend.speech.infrastructure.profile_lifecycle_intents_sql import recover_intent
from speechnexus_backend.speech.infrastructure.profile_publications_sql import (
    _original,
    list_publications,
    mutate_profile,
    read_publication,
)


class PostgresProfiles(PostgresProfileDrafts):
    def __init__(
        self,
        pool: SpeechPool,
        *,
        timeout_seconds: float = 3,
        verifier: ProfilePublicationVerifier | None = None,
    ) -> None:
        super().__init__(pool, timeout_seconds=timeout_seconds)
        self._verifier = (
            verifier if verifier is not None else UnavailableProfilePublicationVerifier()
        )

    def mutate(self, command: ProfileLifecycleCommand) -> ProfileLifecycleResult:
        check_lifecycle_command(command)
        try:
            return self._run(lambda c: mutate_profile(c, command, self._verifier, c.clock))
        except ProfileUnavailableError:
            return ProfileLifecycleResult("unknown")

    def recover_lifecycle(self, command: ProfileLifecycleCommand) -> ProfileLifecycleResult:
        check_lifecycle_command(command)

        def read(cursor: _Cursor) -> ProfileLifecycleResult:
            intent = recover_intent(cursor, command)
            if intent.outcome == "conflict":
                return ProfileLifecycleResult("conflict", error_code="idempotency_conflict")
            if intent.outcome == "unknown":
                return ProfileLifecycleResult("unknown")
            return _original(cursor, command)

        try:
            return self._run(read, read_only=True)
        except ProfileUnavailableError:
            return ProfileLifecycleResult("unknown")

    def get_publication(self, profile_id: str, revision: str) -> PublishedProfile | None:
        return self._run(lambda c: read_publication(c, profile_id, revision), read_only=True)

    def list_publications(
        self, *, limit: int = 50, after: tuple[str, str] | None = None
    ) -> tuple[list[tuple[PublishedProfile, str | None]], bool]:
        return self._run(lambda c: list_publications(c, limit=limit, after=after), read_only=True)
