"""One stable execution code owns one dispatch/capacity shape (issue #569).

The gateway may not record the same error code on two terminals with different
dispatch or capacity facts. These guards cover every code the gateway itself can
persist, so a consumer can decide "possibly dispatched? still holding capacity?
needs coordination?" from the code alone.
"""

from __future__ import annotations

import pytest

from speechnexus_gateway.domain.ports import DispatchOutcome
from speechnexus_gateway.domain.recovery import MarkExecutionUnknown
from speechnexus_gateway.domain.submission import (
    HELD_EXECUTION_ERROR_CODES,
    UNSENT_EXECUTION_ERROR_CODES,
    CleanupState,
    DispatchResult,
    ExecutionErrorCode,
    ExecutionState,
    InvalidExecutionRequestError,
    SubmissionIntent,
    TerminalShape,
    execution_terminal_shape,
)
from tests.unit.gateway.test_submit_execution import NOW, intent_for

UNSENT = tuple(sorted(UNSENT_EXECUTION_ERROR_CODES, key=lambda code: code.value))
HELD = tuple(sorted(HELD_EXECUTION_ERROR_CODES, key=lambda code: code.value))
UNSENT_SHAPE = TerminalShape(False, False, False)
HELD_SHAPE = TerminalShape(True, True, True)


def pending_intent() -> SubmissionIntent:
    return intent_for()


def _assert_shape(intent: SubmissionIntent, shape: TerminalShape) -> None:
    assert intent.execution_state.holds_capacity is shape.holds_capacity
    assert (intent.cleanup_state is CleanupState.QUARANTINED) is shape.needs_coordination
    if not shape.possibly_dispatched:
        assert intent.dispatched_at is None


def test_unsent_and_held_reasons_are_disjoint_and_fully_classified() -> None:
    assert not UNSENT_EXECUTION_ERROR_CODES & HELD_EXECUTION_ERROR_CODES
    for code in UNSENT:
        assert execution_terminal_shape(code) == UNSENT_SHAPE
    for code in HELD:
        assert execution_terminal_shape(code) == HELD_SHAPE


def test_a_rejection_only_code_is_not_an_execution_terminal_reason() -> None:
    for code in (
        ExecutionErrorCode.STALE_FENCING_TOKEN,
        ExecutionErrorCode.SUBMIT_INTENT_CONFLICT,
        ExecutionErrorCode.INSTANCE_GENERATION_MISMATCH,
        ExecutionErrorCode.INSTANCE_DRAINING,
        ExecutionErrorCode.COMPONENT_CREDENTIAL_INVALID,
    ):
        with pytest.raises(ValueError):
            execution_terminal_shape(code)


@pytest.mark.parametrize("code", UNSENT, ids=lambda code: code.value)
def test_every_unsent_code_marks_the_same_not_dispatched_shape(code: ExecutionErrorCode) -> None:
    intent = pending_intent()
    intent.mark_not_dispatched_failure(code, at=NOW)

    assert intent.execution_state is ExecutionState.FAILED
    assert intent.cleanup_state is CleanupState.NOT_REQUIRED
    assert intent.error_code is code
    _assert_shape(intent, UNSENT_SHAPE)


@pytest.mark.parametrize("code", HELD, ids=lambda code: code.value)
def test_every_held_code_marks_the_same_quarantined_shape(code: ExecutionErrorCode) -> None:
    intent = pending_intent()
    intent.mark_dispatched("engine-request-intent-1", at=NOW)
    updated = MarkExecutionUnknown.for_intent(intent, reason_code=code, at=NOW).apply(
        intent, at=NOW
    )

    assert updated.execution_state is ExecutionState.UNKNOWN_QUARANTINED
    assert updated.cleanup_state is CleanupState.QUARANTINED
    assert updated.error_code is code
    _assert_shape(updated, HELD_SHAPE)


def test_a_held_reason_cannot_close_an_execution_that_never_dispatched() -> None:
    intent = pending_intent()
    with pytest.raises(InvalidExecutionRequestError):
        intent.mark_not_dispatched_failure(ExecutionErrorCode.ENGINE_TRANSPORT_UNKNOWN, at=NOW)
    assert intent.execution_state is ExecutionState.SUBMITTING


def test_an_unsent_reason_cannot_quarantine_an_execution() -> None:
    intent = pending_intent()
    with pytest.raises(InvalidExecutionRequestError):
        MarkExecutionUnknown.for_intent(
            intent, reason_code=ExecutionErrorCode.INVALID_EXECUTION_REQUEST, at=NOW
        )


@pytest.mark.parametrize(
    ("result", "code"),
    [
        (DispatchResult.NOT_SENT, None),
        (DispatchResult.REJECTED, None),
        (DispatchResult.NOT_SENT, ExecutionErrorCode.ENGINE_TRANSPORT_UNKNOWN),
        (DispatchResult.RESPONSE_LOST, ExecutionErrorCode.INVALID_EXECUTION_REQUEST),
        (DispatchResult.ACKNOWLEDGED, ExecutionErrorCode.INVALID_EXECUTION_REQUEST),
    ],
    ids=[
        "not-sent-without-reason",
        "rejected-without-reason",
        "held-reason-on-not-sent",
        "reason-on-lost-response",
        "reason-on-acknowledgement",
    ],
)
def test_a_dispatch_outcome_cannot_borrow_or_omit_its_reason(
    result: DispatchResult, code: ExecutionErrorCode | None
) -> None:
    with pytest.raises(ValueError):
        DispatchOutcome(
            result=result,
            observed_at=NOW,
            engine_request_id="engine-request-1" if result is DispatchResult.ACKNOWLEDGED else None,
            error_code=code,
        )


def test_a_deterministic_outcome_with_an_unsent_reason_is_accepted() -> None:
    for code in UNSENT:
        outcome = DispatchOutcome(DispatchResult.NOT_SENT, NOW, error_code=code)
        assert outcome.error_code is code
