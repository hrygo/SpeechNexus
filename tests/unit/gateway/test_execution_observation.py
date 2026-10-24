"""Observed facts cannot mutate identities, metering, stop proof or dispatch history."""

from dataclasses import replace
from datetime import timedelta
from typing import Any

import pytest
from speechnexus_contracts import ContractValidationError
from speechnexus_contracts.validation import validate_coordination

from speechnexus_gateway.application.observe_execution import ExecutionObservationService
from speechnexus_gateway.application.submit_execution import SubmitExecutionService
from speechnexus_gateway.domain.observation import (
    ExecutionObservation,
    RecordExecutionObservation,
)
from speechnexus_gateway.domain.ports import EngineObservation
from speechnexus_gateway.domain.recovery import ExecutionSnapshot
from speechnexus_gateway.domain.submission import (
    CleanupState,
    EngineObservedState,
    ExecutionCleanupPendingError,
    ExecutionErrorCode,
    ExecutionState,
    InvalidExecutionRequestError,
    StaleSubmissionVersionError,
    StopEvidenceKind,
    SubmissionOutcomeUnknownError,
)
from speechnexus_gateway.interfaces.observation_transactions import ExecutionObservationBoundary
from tests.fake.execution import InMemorySubmissionLedger, ScriptedEngine, fake_ingress
from tests.unit.gateway.test_execution_recovery import UNKNOWN, pending
from tests.unit.gateway.test_submit_execution import NOW
from tests.unit.gateway.test_submit_ledger_sql import INTENT

LATER = NOW + timedelta(seconds=2)


def executing(current: Any = INTENT, **changes: Any) -> ExecutionObservation:
    return replace(
        ExecutionObservation.from_intent(current),
        **(
            {
                "execution_state": ExecutionState.EXECUTING,
                "cleanup_state": CleanupState.PENDING,
                "dispatched_at": current.recorded_at,
                "engine_request_id": "engine-request-1",
                "observed_at": current.updated_at,
            }
            | changes
        ),
    )


def test_full_observation_keeps_original_identity_and_uses_the_transaction_clock() -> None:
    observed = executing()
    updated = RecordExecutionObservation(observed, 1).apply(INTENT, at=LATER)
    assert updated.version == 2 and updated.updated_at == LATER
    assert updated.identity == INTENT.identity and updated.request_digest == INTENT.request_digest
    assert updated.dispatched_at == NOW and INTENT.dispatched_at is None
    same = replace(observed, observed_at=LATER)
    assert RecordExecutionObservation(same, 2).apply(updated, at=LATER) is updated
    with pytest.raises(StaleSubmissionVersionError):
        RecordExecutionObservation(observed, 1).apply(updated, at=LATER)


@pytest.mark.parametrize(
    "field,value",
    [
        ("gateway_id", "gateway-other"),
        ("host_node_id", "node-other"),
        ("submit_intent_id", "intent-other"),
        ("lease_id", "lease-other"),
        ("operation_id", "operation-other"),
        ("attempt_id", "attempt-other"),
        ("stage_id", "stage-other"),
        ("workspace_id", "workspace-other"),
        ("pool_id", "tts-batch"),
        ("instance_id", "instance-other"),
        ("binding_revision_id", "binding-other"),
        ("instance_generation", 2),
        ("fencing_token", 2),
        ("policy_revision_id", "policy-other"),
        ("voice_revision_id", "voice-other"),
        ("command_digest", "sha256:" + "b" * 64),
        ("deadline_at", NOW + timedelta(minutes=9)),
        ("max_output_audio_ms", 3000),
    ],
)
def test_observation_cannot_change_frozen_identity(field: str, value: Any) -> None:
    observed = replace(executing(), identity=replace(INTENT.identity, **{field: value}))
    with pytest.raises(InvalidExecutionRequestError):
        RecordExecutionObservation(observed, 1).apply(INTENT, at=LATER)


@pytest.mark.parametrize(
    "field,value", [("generated_pcm_samples", 1), ("output_stream_id", "tts-other")]
)
def test_observation_cannot_rewrite_the_output_owners_facts(field: str, value: Any) -> None:
    with pytest.raises(InvalidExecutionRequestError):
        RecordExecutionObservation(replace(executing(), **{field: value}), 1).apply(
            INTENT, at=LATER
        )


@pytest.mark.parametrize(
    "changes",
    [
        {"dispatched_at": NOW - timedelta(seconds=1)},
        {"observed_at": NOW - timedelta(seconds=1)},
        {"observed_at": LATER + timedelta(seconds=1)},
        {"dispatched_at": LATER, "observed_at": NOW},
    ],
)
def test_observation_rejects_impossible_or_stale_time(changes: dict[str, Any]) -> None:
    with pytest.raises(InvalidExecutionRequestError):
        RecordExecutionObservation(executing(**changes), 1).apply(INTENT, at=LATER)


@pytest.mark.parametrize(
    "state", [ExecutionState.SUCCEEDED, ExecutionState.ABORTED, ExecutionState.FAILED]
)
def test_status_does_not_manufacture_terminal_stop_and_release_evidence(
    state: ExecutionState,
) -> None:
    observed = executing(
        execution_state=state,
        cleanup_state=CleanupState.COMPLETE,
        stop_evidence_kind=StopEvidenceKind.ENGINE_TERMINAL_RESPONSE,
        error_code=ExecutionErrorCode.ENGINE_TRANSPORT_UNKNOWN
        if state is ExecutionState.FAILED
        else None,
    )
    with pytest.raises(ExecutionCleanupPendingError):
        RecordExecutionObservation(observed, 1).apply(INTENT, at=LATER)


def test_a_verified_unsent_failure_has_no_staging_or_output_and_does_not_create_stop() -> None:
    unsent = executing(
        execution_state=ExecutionState.FAILED,
        cleanup_state=CleanupState.NOT_REQUIRED,
        engine_request_id=None,
        dispatched_at=None,
        error_code=ExecutionErrorCode.ENGINE_TRANSPORT_UNKNOWN,
    )
    closed = RecordExecutionObservation(unsent, 1).apply(INTENT, at=LATER)
    assert closed.cleanup_state is CleanupState.NOT_REQUIRED
    assert closed.stop_evidence_digest is None and closed.terminated_at is None
    with pytest.raises(ExecutionCleanupPendingError):
        RecordExecutionObservation(unsent, 1).apply(
            replace(INTENT, has_staging_output=True), at=LATER
        )
    with pytest.raises(InvalidExecutionRequestError):
        RecordExecutionObservation(unsent, 1).apply(
            replace(INTENT, generated_pcm_samples=1), at=LATER
        )


def test_known_dispatch_and_stopping_cannot_be_overwritten_by_late_execution() -> None:
    current = RecordExecutionObservation(executing(), 1).apply(INTENT, at=NOW)
    for change in (
        {"engine_request_id": "engine-other"},
        {"dispatched_at": LATER},
    ):
        with pytest.raises(InvalidExecutionRequestError):
            RecordExecutionObservation(executing(current, observed_at=LATER, **change), 2).apply(
                current, at=LATER
            )
    current.mark_stopping(at=NOW)
    with pytest.raises(ExecutionCleanupPendingError):
        RecordExecutionObservation(executing(current, observed_at=LATER), 3).apply(
            current, at=LATER
        )


def test_reconciliation_does_not_invent_a_dispatch_time_after_a_crash() -> None:
    ledger = InMemorySubmissionLedger(now=lambda: LATER)
    pending(ledger)
    ledger.mark_execution_unknown(UNKNOWN)
    engine = ScriptedEngine(now=lambda: LATER)
    service = SubmitExecutionService(
        ledger=ledger,
        ingress=fake_ingress(),
        dispatch=engine,
        observation=engine,
        now=lambda: LATER,
    )
    engine.script_observation(
        "intent-1", EngineObservation("engine-1", EngineObservedState.EXECUTING)
    )
    unknown = service.query("intent-1")
    assert unknown is not None and unknown.execution_state is ExecutionState.UNKNOWN_QUARANTINED
    assert unknown.dispatched_at is None and unknown.version == 2
    engine.script_observation(
        "intent-1", EngineObservation("engine-1", EngineObservedState.EXECUTING, dispatched_at=NOW)
    )
    recovered = service.query("intent-1")
    assert recovered is not None and recovered.execution_state is ExecutionState.EXECUTING
    assert recovered.dispatched_at == NOW and recovered.version == 3
    assert engine.dispatch_count == 0


def boundary_for(ledger: InMemorySubmissionLedger) -> ExecutionObservationBoundary:
    return ExecutionObservationBoundary(
        ExecutionObservationService(ingress=fake_ingress(), ledger=ledger)
    )


def test_boundary_translates_generated_status_and_returns_full_typed_results() -> None:
    ledger = InMemorySubmissionLedger(now=lambda: LATER)
    original = pending(ledger)
    observed = replace(original)
    observed.mark_dispatched("engine-1", at=NOW)
    request = {
        "observation": ExecutionSnapshot(observed).status(),
        "expected_version": "1",
    }
    boundary = boundary_for(ledger)
    result = boundary.record_execution_observation(request)
    assert result["outcome"] == "committed" and result["version"] == "2"
    validate_coordination("TransactionResult", result)
    conflict = boundary.record_execution_observation(request)
    assert conflict["error_code"] == "version_conflict"
    assert conflict["recovery"] == "query_current_authority"
    replay = boundary.record_execution_observation(
        {"observation": result["value"]["execution"], "expected_version": "2"}
    )
    assert replay["outcome"] == "replayed" and replay["version"] == "2"
    with pytest.raises(ContractValidationError):
        boundary.record_execution_observation(request | {"expected_version": 1})


def test_unknown_commit_is_not_reported_as_a_retryable_observation() -> None:
    class LostLedger(InMemorySubmissionLedger):
        def record_execution_observation(self, request: RecordExecutionObservation) -> Any:
            raise SubmissionOutcomeUnknownError

    ledger = LostLedger(now=lambda: LATER)
    original = pending(ledger)
    result = boundary_for(ledger).record_execution_observation(
        {"observation": ExecutionSnapshot(original).status(), "expected_version": "1"}
    )
    assert result["outcome"] == "unknown"
    assert result["recovery"] == "query_original" and "value" not in result


def test_foreign_gateway_observation_is_rejected_before_writing_any_fact() -> None:
    ledger = InMemorySubmissionLedger(now=lambda: LATER)
    original = pending(ledger, gateway_id="gateway-other")
    result = boundary_for(ledger).record_execution_observation(
        {"observation": ExecutionSnapshot(original).status(), "expected_version": "1"}
    )
    assert result["outcome"] == "rejected"
    assert result["error_code"] == "invalid_execution_request"
    assert result["recovery"] == "none"
    assert ledger.get("intent-1") == original


def test_schema_valid_success_is_not_accepted_as_verified_physical_stop() -> None:
    ledger = InMemorySubmissionLedger(now=lambda: LATER)
    original = pending(ledger)
    claimed = replace(original)
    claimed.mark_dispatched("engine-1", at=NOW)
    claimed.complete_successfully(at=LATER)
    result = boundary_for(ledger).record_execution_observation(
        {"observation": ExecutionSnapshot(claimed).status(), "expected_version": "1"}
    )
    assert result["outcome"] == "rejected"
    assert result["error_code"] == "execution_cleanup_pending"
    assert result["recovery"] == "reconcile"
    assert ledger.get("intent-1") == original


@pytest.mark.parametrize("version", [True, 0, -1, 2**63])
def test_domain_version_cannot_be_coerced_or_exceed_the_storage_domain(version: int) -> None:
    with pytest.raises(InvalidExecutionRequestError):
        RecordExecutionObservation(executing(), version)


@pytest.mark.parametrize(
    "changes",
    [
        {"engine_request_id": "engine-preclaimed"},
        {"version": 2},
        {"version": True},
        {"updated_at": LATER},
        {"generated_pcm_samples": 1},
        {"has_staging_output": True},
        {"has_output_intervals": True},
    ],
)
def test_creation_does_not_import_existing_observation_facts(changes: dict[str, Any]) -> None:
    with pytest.raises(InvalidExecutionRequestError):
        replace(INTENT, **changes).validate_new()
