"""Original abort receipt and one bounded, recoverable external dispatch attempt.

A receipt is accepted once and never rewritten. Each external abort takes a lease
recorded in the same transaction: an attempt identity, its start and its deadline.
The lease is what makes the dispatch recoverable - a crash between the claim and
the engine call, or between the engine answer and the local record, leaves an
expired or undecided attempt instead of a permanent one-shot flag.
"""

from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Any

from speechnexus_gateway.domain.abort_intent import (
    ABORT_RETRY_BASE_MS,
    ABORT_RETRY_MAX_MS,
    MAX_ABORT_DISPATCH_ATTEMPTS,
    MAX_ABORT_DISPATCH_LEASE_MS,
    AbortDispatchClaim,
    AbortDispatchOutcome,
    AbortDispatchResult,
    AbortExecutionRequest,
    abort_dispatch_lease_ms,
)
from speechnexus_gateway.domain.recovery import (
    ExecutionObservationConflictError,
    MarkExecutionUnknown,
)
from speechnexus_gateway.domain.submission import (
    MAX_ORIGINAL_COMMAND_BYTES,
    ExecutionErrorCode,
    ExecutionState,
    InvalidExecutionRequestError,
    SubmissionIntent,
)
from speechnexus_gateway.infrastructure.original_command_sql import read_original_request
from speechnexus_gateway.infrastructure.original_command_state import read_original_document_state
from speechnexus_gateway.infrastructure.recovery_sql import mark_execution_unknown_in_transaction
from speechnexus_gateway.infrastructure.submit_sql import (
    SubmitCursor,
    from_unix_micros,
    lock_submit_intent,
    to_unix_micros,
)

_RECEIPT_COLUMNS = (
    "submit_intent_id, operation_id, attempt_id, fencing_token, reason, wait_timeout_ms, "
    "received_at_us, dispatched_at_us, instance_id, instance_generation, "
    "dispatch_attempt_id, dispatch_started_at_us, dispatch_deadline_at_us, "
    "dispatch_outcome, dispatch_outcome_at_us, dispatch_attempts"
)

#: Outcomes that end the attempts of this receipt: no later caller may send again.
_DECIDED_OUTCOMES = (
    AbortDispatchOutcome.ACKNOWLEDGED.value,
    AbortDispatchOutcome.REJECTED_FENCED.value,
    AbortDispatchOutcome.EXHAUSTED.value,
)


def _check(intent: SubmissionIntent, request: AbortExecutionRequest) -> None:
    identity = intent.identity
    if (identity.operation_id, identity.attempt_id, identity.fencing_token) != (
        request.operation_id,
        request.attempt_id,
        request.fencing_token,
    ):
        raise InvalidExecutionRequestError("abort identity disagrees with original execution")


def _row_drifted(row: Mapping[str, Any], intent: SubmissionIntent) -> bool:
    """True when the receipt no longer describes the authoritative execution."""
    identity = intent.identity
    if (row["operation_id"], row["attempt_id"], row["fencing_token"]) != (
        identity.operation_id,
        identity.attempt_id,
        identity.fencing_token,
    ):
        return True
    return (row["instance_id"], row["instance_generation"]) != (
        identity.instance_id,
        identity.instance_generation,
    )


def _receipt(cursor: SubmitCursor, request: AbortExecutionRequest) -> dict[str, Any] | None:
    cursor.execute(
        f"SELECT {_RECEIPT_COLUMNS} FROM gateway_abort_intents "
        "WHERE submit_intent_id=%s FOR UPDATE",
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
    # Check the original abort intent first. A receipt can survive legal content
    # deletion; its replay may use a validated tombstone, but a new receipt may not.
    if _receipt(cursor, request) is not None:
        read_original_document_state(cursor, intent)
        return
    read_original_request(cursor, intent, max_document_bytes=MAX_ORIGINAL_COMMAND_BYTES)
    identity = intent.identity
    cursor.execute(
        "INSERT INTO gateway_abort_intents (submit_intent_id, operation_id, attempt_id, "
        "fencing_token, reason, wait_timeout_ms, received_at_us, instance_id, "
        "instance_generation) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
        (
            request.submit_intent_id,
            request.operation_id,
            request.attempt_id,
            request.fencing_token,
            request.reason,
            request.wait_timeout_ms,
            to_unix_micros(clock()),
            identity.instance_id,
            identity.instance_generation,
        ),
    )


def _start_attempt(
    cursor: SubmitCursor,
    request: AbortExecutionRequest,
    *,
    attempt_id: str,
    started_at_us: int,
    deadline_at_us: int,
    outcome: AbortDispatchOutcome | None,
    count: bool = True,
) -> None:
    """Write one attempt lease, or its terminal refusal, in the caller transaction.

    Only an attempt that may reach the engine increments the counter; a refusal or
    the exhausted terminal write records its decision without inflating attempts.
    """
    cursor.execute(
        "UPDATE gateway_abort_intents SET dispatched_at_us=COALESCE(dispatched_at_us,%s), "
        "dispatch_attempt_id=%s, dispatch_started_at_us=%s, dispatch_deadline_at_us=%s, "
        "dispatch_outcome=%s, dispatch_outcome_at_us=%s, "
        "dispatch_attempts=dispatch_attempts+%s WHERE submit_intent_id=%s",
        (
            started_at_us,
            attempt_id,
            started_at_us,
            deadline_at_us,
            None if outcome is None else outcome.value,
            None if outcome is None else started_at_us,
            1 if count else 0,
            request.submit_intent_id,
        ),
    )
    if cursor.rowcount != 1:
        raise ExecutionObservationConflictError("abort attempt changed")


def claim_abort(
    cursor: SubmitCursor,
    request: AbortExecutionRequest,
    *,
    attempt_id: str,
    lease_ms: int,
    clock: Callable[[], datetime],
) -> AbortDispatchClaim | None:
    """Take the one bounded lease that authorizes exactly one external abort call."""
    if not 1 <= len(attempt_id) <= 128:
        raise ValueError("an abort attempt requires a bounded identity")
    if type(lease_ms) is not int or not 100 <= lease_ms <= MAX_ABORT_DISPATCH_LEASE_MS:
        raise ValueError("an abort dispatch lease must stay within its bounds")
    intent = lock_submit_intent(cursor, request.submit_intent_id)
    receipt = _receipt(cursor, request)
    if receipt is None:
        raise ExecutionObservationConflictError("abort receipt is absent")
    if receipt["dispatch_outcome"] in _DECIDED_OUTCOMES or intent.execution_state.is_terminal:
        return None
    now = clock()
    now_us = to_unix_micros(now)
    deadline_us = now_us + lease_ms * 1_000
    if _row_drifted(receipt, intent):
        # The receipt is preserved, but it no longer names the authoritative execution.
        # Record the refusal durably instead of resending against a different origin.
        _start_attempt(
            cursor,
            request,
            attempt_id=attempt_id,
            started_at_us=now_us,
            deadline_at_us=deadline_us,
            outcome=AbortDispatchOutcome.REJECTED_FENCED,
            count=False,
        )
        return None
    current_deadline = receipt["dispatch_deadline_at_us"]
    if current_deadline is not None and current_deadline > now_us:
        # Another attempt holds a live lease; two gateways never send the same abort.
        return None
    attempts = receipt["dispatch_attempts"]
    if attempts >= MAX_ABORT_DISPATCH_ATTEMPTS:
        # The bounded attempts are spent, and no outcome was recorded for the last
        # one (a crash inside it). Give the receipt up instead of retrying forever.
        _start_attempt(
            cursor,
            request,
            attempt_id=attempt_id,
            started_at_us=now_us,
            deadline_at_us=deadline_us,
            outcome=AbortDispatchOutcome.EXHAUSTED,
            count=False,
        )
        return None
    _start_attempt(
        cursor,
        request,
        attempt_id=attempt_id,
        started_at_us=now_us,
        deadline_at_us=deadline_us,
        outcome=None,
    )
    if intent.execution_state is ExecutionState.UNKNOWN_QUARANTINED:
        claimed = intent
    else:
        claimed = mark_execution_unknown_in_transaction(
            cursor,
            MarkExecutionUnknown.for_intent(
                intent, reason_code=ExecutionErrorCode.CANCELLATION_UNCONFIRMED, at=now
            ),
            clock=clock,
        )
    return AbortDispatchClaim(
        intent=claimed,
        attempt_id=attempt_id,
        started_at=now,
        deadline_at=from_unix_micros(deadline_us),
        attempts=attempts + 1,
    )


def record_abort_dispatch(cursor: SubmitCursor, result: AbortDispatchResult) -> bool:
    """Record what the attempt that produced it learned; never overwrite a later one."""
    if result.outcome is AbortDispatchOutcome.REJECTED_FENCED:
        raise ValueError("a fenced refusal is recorded when the claim is refused")
    observed_us = to_unix_micros(result.observed_at)
    # An undecided attempt is retried with exponential backoff, not on the lease
    # cadence: a crash must not turn the receipt into a permanent tight retry loop.
    # The delay starts at this transaction's database time; the adapter observation
    # is only an audit fact and never a scheduling base.
    cursor.execute(
        "UPDATE gateway_abort_intents SET dispatch_outcome=%s, dispatch_outcome_at_us=%s, "
        "next_check_at_us=CASE WHEN %s=%s THEN "
        "CAST(EXTRACT(EPOCH FROM clock_timestamp()) * 1000000 AS BIGINT) + LEAST("
        "%s * POWER(2, GREATEST(dispatch_attempts - 1, 0)), %s)::BIGINT "
        "ELSE next_check_at_us END "
        "WHERE submit_intent_id=%s AND dispatch_attempt_id=%s AND dispatch_outcome IS NULL",
        (
            result.outcome.value,
            observed_us,
            result.outcome.value,
            AbortDispatchOutcome.UNCONFIRMED.value,
            ABORT_RETRY_BASE_MS * 1_000,
            ABORT_RETRY_MAX_MS * 1_000,
            result.submit_intent_id,
            result.attempt_id,
        ),
    )
    return cursor.rowcount == 1


def reject_abort(
    cursor: SubmitCursor,
    request: AbortExecutionRequest,
    *,
    attempt_id: str,
    clock: Callable[[], datetime],
) -> bool:
    """Durably refuse to resend when the authoritative execution no longer matches."""
    if not 1 <= len(attempt_id) <= 128:
        raise ValueError("an abort attempt requires a bounded identity")
    intent = lock_submit_intent(cursor, request.submit_intent_id)
    receipt = _receipt(cursor, request)
    if receipt is None:
        raise ExecutionObservationConflictError("abort receipt is absent")
    if receipt["dispatch_outcome"] in _DECIDED_OUTCOMES:
        return False
    if not _row_drifted(receipt, intent):
        return False
    now_us = to_unix_micros(clock())
    lease_ms = abort_dispatch_lease_ms(request.wait_timeout_ms)
    _start_attempt(
        cursor,
        request,
        attempt_id=attempt_id,
        started_at_us=now_us,
        deadline_at_us=now_us + lease_ms * 1_000,
        outcome=AbortDispatchOutcome.REJECTED_FENCED,
        count=False,
    )
    return True
