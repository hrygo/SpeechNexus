"""Stop identity, complete-fact replay and terminal-status safety."""

from dataclasses import replace

import pytest

from speechnexus_gateway.domain.ports import DispatchOutcome, EngineObservation
from speechnexus_gateway.domain.submission import (
    DispatchResult,
    EngineObservedState,
    ExecutionCleanupPendingError,
    ExecutionErrorCode,
    ExecutionState,
    InvalidExecutionRequestError,
    StopEvidenceConflictError,
    StopEvidenceKind,
    SubmitRequest,
)
from tests.unit.gateway.test_submit_execution import (
    COMMAND,
    NOW,
    build_service,
    identity_for,
    stop_evidence,
)


def test_identical_stop_replay_returns_the_original_version_and_time() -> None:
    service, _, engine = build_service()
    service.submit(SubmitRequest(identity_for(COMMAND), COMMAND))
    first = service.record_stop(stop_evidence())
    second = service.record_stop(stop_evidence())
    assert second == first
    assert second.version == 3
    assert second.terminated_at == NOW
    assert engine.dispatch_count == 1


@pytest.mark.parametrize(
    "changes",
    [
        {"evidence_id": "evidence-other"},
        {"evidence_digest": "sha256:" + "b" * 64},
        {"reason": "another reason"},
        {"observed_at_us": int(NOW.timestamp()) * 1_000_000 - 1},
        {"evidence_kind": StopEvidenceKind.INSTANCE_TERMINATED_BY_CONTROLLER},
    ],
)
def test_same_submit_with_any_changed_stop_fact_conflicts(changes: dict[str, object]) -> None:
    service, _, _ = build_service()
    service.submit(SubmitRequest(identity_for(COMMAND), COMMAND))
    original = service.record_stop(stop_evidence())
    with pytest.raises(StopEvidenceConflictError):
        service.record_stop(replace(stop_evidence(), **changes))  # type: ignore[arg-type]
    assert service.query("intent-1") == original


@pytest.mark.parametrize(
    ("state", "kind", "error"),
    [
        (ExecutionState.SUCCEEDED, StopEvidenceKind.ENGINE_ABORT_ACK, None),
        (ExecutionState.FAILED, StopEvidenceKind.ENGINE_TERMINAL_RESPONSE, None),
        (
            ExecutionState.ABORTED,
            StopEvidenceKind.ENGINE_ABORT_ACK,
            ExecutionErrorCode.ENGINE_TRANSPORT_UNKNOWN,
        ),
        (ExecutionState.EXECUTING, StopEvidenceKind.ENGINE_ABORT_ACK, None),
    ],
)
def test_invalid_terminal_matrix_keeps_the_execution_active(
    state: ExecutionState, kind: StopEvidenceKind, error: ExecutionErrorCode | None
) -> None:
    service, _, _ = build_service()
    initial = service.submit(SubmitRequest(identity_for(COMMAND), COMMAND)).intent
    with pytest.raises(InvalidExecutionRequestError):
        service.record_stop(stop_evidence(kind=kind), terminal_state=state, error_code=error)
    assert service.query("intent-1") == initial


@pytest.mark.parametrize("state", [EngineObservedState.SUCCEEDED, EngineObservedState.FAILED])
def test_a_terminal_engine_status_without_release_evidence_keeps_quarantine(
    state: EngineObservedState,
) -> None:
    service, ledger, engine = build_service()
    engine.script_outcome(DispatchOutcome(DispatchResult.RESPONSE_LOST, NOW))
    initial = service.submit(SubmitRequest(identity_for(COMMAND), COMMAND)).intent
    engine.script_observation("intent-1", EngineObservation("engine-request-late", state))
    assert service.query("intent-1") == initial
    replay = service.submit(SubmitRequest(identity_for(COMMAND), COMMAND))
    assert replay.intent == initial
    assert ledger.get("intent-1") == initial
    assert initial.execution_state.holds_capacity
    assert engine.dispatch_count == 1


@pytest.mark.parametrize(
    "changes",
    [
        {"observed_at_us": -1},
        {"observed_at_us": 2**53},
        {"observed_at_us": True},
        {"reason": ""},
        {"reason": "  "},
        {"reason": "x" * 2001},
        {"evidence_kind": StopEvidenceKind.OPERATOR_VERIFIED_INSTANCE_TERMINATED, "reason": None},
        {"evidence_id": "INVALID ID"},
    ],
)
def test_stop_evidence_declaration_matches_its_contract_bounds(changes: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        replace(stop_evidence(), **changes)  # type: ignore[arg-type]


def test_unresolved_dispatch_needs_abort_or_reconciliation_before_success() -> None:
    service, _, engine = build_service()
    engine.script_outcome(DispatchOutcome(DispatchResult.RESPONSE_LOST, NOW))
    original = service.submit(SubmitRequest(identity_for(COMMAND), COMMAND)).intent
    with pytest.raises(ExecutionCleanupPendingError):
        service.record_stop(
            stop_evidence(kind=StopEvidenceKind.ENGINE_TERMINAL_RESPONSE),
            terminal_state=ExecutionState.SUCCEEDED,
        )
    assert service.query("intent-1") == original
    closed = service.record_stop(stop_evidence())
    assert closed.execution_state is ExecutionState.ABORTED
