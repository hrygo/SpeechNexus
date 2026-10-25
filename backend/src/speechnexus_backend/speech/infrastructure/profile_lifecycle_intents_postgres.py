"""Private PG journal using the existing bounded profile transaction driver."""

from speechnexus_backend.speech.application.profile_lifecycle import check_lifecycle_command
from speechnexus_backend.speech.domain.profile_drafts import ProfileUnavailableError
from speechnexus_backend.speech.domain.profile_lifecycle import ProfileLifecycleCommand
from speechnexus_backend.speech.domain.profile_lifecycle_intents import (
    ProfileLifecycleIntentResult,
)
from speechnexus_backend.speech.infrastructure.postgres_pool import SpeechPool
from speechnexus_backend.speech.infrastructure.profile_drafts_postgres import PostgresProfileDrafts
from speechnexus_backend.speech.infrastructure.profile_lifecycle_intents_sql import (
    record_intent,
    recover_intent,
)


class PostgresProfileLifecycleIntents:
    def __init__(self, pool: SpeechPool, *, timeout_seconds: float = 3) -> None:
        self._driver = PostgresProfileDrafts(pool, timeout_seconds=timeout_seconds)

    def record(self, command: ProfileLifecycleCommand) -> ProfileLifecycleIntentResult:
        check_lifecycle_command(command)
        try:
            return self._driver._run(lambda cursor: record_intent(cursor, command, cursor.clock))
        except ProfileUnavailableError:
            return ProfileLifecycleIntentResult("unknown")

    def recover(self, command: ProfileLifecycleCommand) -> ProfileLifecycleIntentResult:
        check_lifecycle_command(command)
        try:
            return self._driver._run(lambda cursor: recover_intent(cursor, command), read_only=True)
        except ProfileUnavailableError:
            return ProfileLifecycleIntentResult("unknown")
