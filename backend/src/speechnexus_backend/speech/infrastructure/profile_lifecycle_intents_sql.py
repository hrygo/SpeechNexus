"""One immutable key/complete intent. No provider, resource or publication writes."""

from collections.abc import Callable

from speechnexus_backend.speech.application.profile_lifecycle import check_lifecycle_command
from speechnexus_backend.speech.domain.profile_drafts import ProfileUnavailableError
from speechnexus_backend.speech.domain.profile_lifecycle import ProfileLifecycleCommand
from speechnexus_backend.speech.domain.profile_lifecycle_intents import (
    ProfileLifecycleIntent,
    ProfileLifecycleIntentResult,
)
from speechnexus_backend.speech.infrastructure.profile_drafts_sql import ProfileCursor


def recover_intent(
    cursor: ProfileCursor, command: ProfileLifecycleCommand
) -> ProfileLifecycleIntentResult:
    check_lifecycle_command(command)
    cursor.execute(
        "SELECT * FROM speech_profile_lifecycle_intents WHERE key_digest=%s",
        (command.key_digest,),
    )
    rows = cursor.fetchall()
    if not rows:
        return ProfileLifecycleIntentResult("unknown")
    if len(rows) != 1:
        raise ProfileUnavailableError
    row = rows[0]
    original = ProfileLifecycleCommand(
        row["operation"],
        row["profile_id"],
        row["revision"],
        row["key_digest"],
        row["intent_json"].encode("utf-8"),
    )
    check_lifecycle_command(original)
    if original.request_digest != row["request_digest"]:
        raise ProfileUnavailableError
    intent = ProfileLifecycleIntent(original, row["recorded_at_us"])
    # Canonical bytes preserve JSON types, null, validation metadata and CAS.
    # A corrupt stored row is unknown, never an authoritative conflict/rejection.
    if original != command:
        return ProfileLifecycleIntentResult("conflict")
    return ProfileLifecycleIntentResult("replayed", intent)


def record_intent(
    cursor: ProfileCursor, command: ProfileLifecycleCommand, clock: Callable[[], int]
) -> ProfileLifecycleIntentResult:
    check_lifecycle_command(command)
    original = recover_intent(cursor, command)
    if original.outcome != "unknown":
        return original
    recorded = ProfileLifecycleIntent(command, clock())
    cursor.execute(
        "INSERT INTO speech_profile_lifecycle_intents "
        "(key_digest,operation,profile_id,revision,intent_json,request_digest,recorded_at_us) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (key_digest) DO NOTHING",
        (
            command.key_digest,
            command.operation,
            command.profile_id,
            command.revision,
            command.intent.decode("utf-8"),
            command.request_digest,
            recorded.recorded_at_us,
        ),
    )
    if cursor.rowcount == 1:
        return ProfileLifecycleIntentResult("recorded", recorded)
    if cursor.rowcount != 0:
        raise ProfileUnavailableError
    return recover_intent(cursor, command)
