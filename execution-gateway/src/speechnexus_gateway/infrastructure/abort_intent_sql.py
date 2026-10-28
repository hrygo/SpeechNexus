"""Original abort receipt and one bounded external-dispatch claim."""

from collections.abc import Callable
from datetime import datetime

from speechnexus_gateway.domain.abort_intent import AbortExecutionRequest
from speechnexus_gateway.domain.recovery import (
    ExecutionObservationConflictError,
    MarkExecutionUnknown,
)
from speechnexus_gateway.domain.submission import (
    ExecutionErrorCode,
    ExecutionState,
    InvalidExecutionRequestError,
    SubmissionIntent,
)
from speechnexus_gateway.infrastructure.recovery_sql import mark_execution_unknown_in_transaction
from speechnexus_gateway.infrastructure.submit_sql import (
    SubmitCursor,
    lock_submit_intent,
    to_unix_micros,
)


def _check(intent: SubmissionIntent, request: AbortExecutionRequest) -> None:
    identity = intent.identity
    if (identity.operation_id, identity.attempt_id, identity.fencing_token) != (
        request.operation_id,
        request.attempt_id,
        request.fencing_token,
    ):
        raise InvalidExecutionRequestError("abort identity disagrees with original execution")


def _receipt(cursor: SubmitCursor, request: AbortExecutionRequest) -> dict[str, object] | None:
    cursor.execute(
        "SELECT operation_id, attempt_id, fencing_token, reason, wait_timeout_ms, dispatched_at_us "
        "FROM gateway_abort_intents WHERE submit_intent_id=%s FOR UPDATE",
        (request.submit_intent_id,),
    )
    rows = cursor.fetchall()
    if not rows:
        return None
    if len(rows) != 1:
        raise ExecutionObservationConflictError("duplicate abort receipt")
    row = rows[0]
    if tuple(
        row[name]
        for name in ("operation_id", "attempt_id", "fencing_token", "reason", "wait_timeout_ms")
    ) != (
        request.operation_id,
        request.attempt_id,
        request.fencing_token,
        request.reason,
        request.wait_timeout_ms,
    ):
        raise ExecutionObservationConflictError("abort identity has another intent")
    return row


def receive_abort(
    cursor: SubmitCursor, request: AbortExecutionRequest, *, clock: Callable[[], datetime]
) -> None:
    intent = lock_submit_intent(cursor, request.submit_intent_id)
    _check(intent, request)
    if _receipt(cursor, request) is not None:
        return
    cursor.execute(
        "INSERT INTO gateway_abort_intents (submit_intent_id, operation_id, attempt_id, "
        "fencing_token, reason, wait_timeout_ms, received_at_us) VALUES (%s,%s,%s,%s,%s,%s,%s)",
        (
            request.submit_intent_id,
            request.operation_id,
            request.attempt_id,
            request.fencing_token,
            request.reason,
            request.wait_timeout_ms,
            to_unix_micros(clock()),
        ),
    )


def claim_abort(
    cursor: SubmitCursor, request: AbortExecutionRequest, *, clock: Callable[[], datetime]
) -> SubmissionIntent | None:
    intent = lock_submit_intent(cursor, request.submit_intent_id)
    _check(intent, request)
    receipt = _receipt(cursor, request)
    if receipt is None:
        raise ExecutionObservationConflictError("abort receipt is absent")
    if receipt["dispatched_at_us"] is not None or intent.execution_state.is_terminal:
        return None
    now = clock()
    cursor.execute(
        "UPDATE gateway_abort_intents SET dispatched_at_us=%s "
        "WHERE submit_intent_id=%s AND dispatched_at_us IS NULL",
        (to_unix_micros(now), request.submit_intent_id),
    )
    if cursor.rowcount != 1:
        raise ExecutionObservationConflictError("abort claim changed")
    if intent.execution_state is ExecutionState.UNKNOWN_QUARANTINED:
        return intent
    return mark_execution_unknown_in_transaction(
        cursor,
        MarkExecutionUnknown.for_intent(
            intent, reason_code=ExecutionErrorCode.CANCELLATION_UNCONFIRMED, at=now
        ),
        clock=clock,
    )
