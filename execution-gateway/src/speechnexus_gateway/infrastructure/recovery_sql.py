"""Ordinary row locks for quarantine; read-only snapshots never contact an engine."""

from collections.abc import Callable
from datetime import datetime

from speechnexus_gateway.domain.recovery import MarkExecutionUnknown, RejectExecutionBeforeEngine
from speechnexus_gateway.domain.submission import (
    ExecutionCleanupPendingError,
    ExecutionState,
    SubmissionIntent,
    SubmissionStoreError,
)
from speechnexus_gateway.infrastructure.submit_sql import (
    REJECT_UNSENT_INTENT,
    SubmitCursor,
    lock_submit_intent,
    read_submit_intent,
    save_submit_intent,
    to_unix_micros,
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
    """Close an executing intent whose request never reached the engine.

    The ordinary intent rewrite refuses to clear an already-allocated request
    identity or to enter ``not_required`` from ``executing``. This transition is
    the controlled exception: the stage still owns the request locally and has
    proved the engine was never called, so a dedicated guarded UPDATE may clear
    the unsent dispatch association and release capacity without stop evidence.
    """
    intent = lock_submit_intent(cursor, request.submit_intent_id)
    updated = request.apply(intent, at=clock())
    if updated is intent:
        return intent
    updated.validate()
    reason = updated.error_code
    if reason is None:
        raise SubmissionStoreError("an unsent rejection needs a deterministic reason")
    # ``SET terminated_at_us = NULL`` is defensive: an executing row has no
    # termination time under the current state matrix.
    cursor.execute(
        REJECT_UNSENT_INTENT,
        (
            reason.value,
            to_unix_micros(updated.updated_at),
            request.submit_intent_id,
            intent.version,
        ),
    )
    # Defensive: the intent row is locked and ``apply()`` already read the same
    # state, so a 0 rowcount is only reachable through a concurrent raw writer.
    if cursor.rowcount != 1:
        current = read_submit_intent(cursor, request.submit_intent_id)
        if (
            current is not None
            and current.execution_state is ExecutionState.FAILED
            and current.error_code is request.reason_code
        ):
            return current
        raise ExecutionCleanupPendingError("the execution changed before its unsent rejection")
    saved = read_submit_intent(cursor, request.submit_intent_id)
    if saved is None:
        raise SubmissionStoreError("the rejected execution disappeared")
    return saved
