"""Ordinary row locks for quarantine; read-only snapshots never contact an engine."""

from collections.abc import Callable
from datetime import datetime

from speechnexus_gateway.domain.recovery import MarkExecutionUnknown, RejectExecutionBeforeEngine
from speechnexus_gateway.domain.submission import SubmissionIntent
from speechnexus_gateway.infrastructure.submit_sql import (
    SubmitCursor,
    lock_submit_intent,
    save_submit_intent,
)


def mark_execution_unknown_in_transaction(
    cursor: SubmitCursor, request: MarkExecutionUnknown, *, clock: Callable[[], datetime]
) -> SubmissionIntent:
    intent = lock_submit_intent(cursor, request.submit_intent_id)
    updated = request.apply(intent, at=clock())
    if updated is not intent:
        # Reuse the frozen-fact/version guards. Reacquiring our own pool/submit locks
        # adds no new lock-order edge and keeps the conditional write in one place.
        save_submit_intent(cursor, updated, expected_version=intent.version)
    return updated


def reject_execution_before_engine_in_transaction(
    cursor: SubmitCursor,
    request: RejectExecutionBeforeEngine,
    *,
    clock: Callable[[], datetime],
) -> SubmissionIntent:
    """Close an executing intent whose request never reached the engine."""
    intent = lock_submit_intent(cursor, request.submit_intent_id)
    updated = request.apply(intent, at=clock())
    if updated is not intent:
        save_submit_intent(cursor, updated, expected_version=intent.version)
    return updated
