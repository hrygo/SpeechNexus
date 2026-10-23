"""Observe under ordinary pool/submit row locks, then write one guarded version."""

from collections.abc import Callable
from datetime import datetime

from speechnexus_gateway.domain.observation import RecordExecutionObservation
from speechnexus_gateway.domain.submission import SubmissionIntent
from speechnexus_gateway.infrastructure.submit_sql import (
    SubmitCursor,
    lock_submit_intent,
    save_submit_intent,
)


def record_execution_observation_in_transaction(
    cursor: SubmitCursor, request: RecordExecutionObservation, *, clock: Callable[[], datetime]
) -> SubmissionIntent:
    current = lock_submit_intent(cursor, request.observation.identity.submit_intent_id)
    updated = request.apply(current, at=clock())
    if updated is not current:
        save_submit_intent(cursor, updated, expected_version=request.expected_version)
    return updated
