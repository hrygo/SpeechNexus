"""Submission replay, conflict, quarantine and stop-evidence behaviour."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from speechnexus_gateway.application.submit_execution import SubmitExecutionService
from speechnexus_gateway.domain.canonical import JsonValue, command_digest
from speechnexus_gateway.domain.ports import DispatchOutcome, EngineObservation
from speechnexus_gateway.domain.submission import (
    CancellationUnconfirmedError,
    CleanupState,
    DispatchResult,
    EngineObservedState,
    ExecutionErrorCode,
    ExecutionState,
    InvalidExecutionRequestError,
    StopEvidenceKind,
    SubmissionIdentity,
    SubmissionIntent,
    SubmitIntentConflictError,
    SubmitRequest,
    VerifiedStopEvidence,
)
from tests.fake.execution import InMemorySubmissionLedger, ScriptedEngine

COMMAND: JsonValue = {
    "schema_version": "1",
    "task": "asr.transcribe",
    "input": {"artifact_id": "a-1"},
}
OTHER_COMMAND: JsonValue = {
    "schema_version": "1",
    "task": "tts.synthesize",
    "input": {"text": "hi"},
}
NOW = datetime(2026, 10, 24, tzinfo=UTC)


def identity_for(command: JsonValue, **overrides: object) -> SubmissionIdentity:
    fields: dict[str, object] = {
        "submit_intent_id": "intent-1",
        "lease_id": "lease-1",
        "operation_id": "op-1",
        "attempt_id": "attempt-1",
        "stage_id": "stage-1",
        "workspace_id": "ws-1",
        "pool_id": "asr-batch",
        "host_node_id": "node-1",
        "gateway_id": "gateway-1",
        "instance_id": "instance-1",
        "instance_generation": 3,
        "binding_revision_id": "binding-rev-1",
        "policy_revision_id": "policy-rev-1",
        "voice_revision_id": None,
        "fencing_token": 7,
        "command_digest": command_digest(command),
        "deadline_at": NOW + timedelta(minutes=5),
        "max_output_audio_ms": None,
        "sample_rate_hz": None,
    }
    fields.update(overrides)
    return SubmissionIdentity(**fields)  # type: ignore[arg-type]


def build_service(
    *, before_dispatch: object = None
) -> tuple[SubmitExecutionService, InMemorySubmissionLedger, ScriptedEngine]:
    ledger = InMemorySubmissionLedger()
    engine = ScriptedEngine(before_dispatch=before_dispatch)  # type: ignore[arg-type]
    service = SubmitExecutionService(
        ledger=ledger, dispatch=engine, observation=engine, now=lambda: NOW
    )
    return service, ledger, engine


def test_new_intent_is_persisted_before_the_engine_request() -> None:
    ledger = InMemorySubmissionLedger()
    seen: list[bool] = []
    engine = ScriptedEngine(
        before_dispatch=lambda intent: seen.append(
            ledger.get(intent.identity.submit_intent_id) is not None
        )
    )
    engine.script_outcome(
        DispatchOutcome(
            result=DispatchResult.ACKNOWLEDGED,
            observed_at=NOW,
            engine_request_id="engine-request-intent-1",
        )
    )
    service = SubmitExecutionService(
        ledger=ledger, dispatch=engine, observation=engine, now=lambda: NOW
    )

    outcome = service.submit(SubmitRequest(identity=identity_for(COMMAND), command=COMMAND))

    assert seen == [True]
    assert outcome.idempotent_replay is False
    assert outcome.intent.execution_state is ExecutionState.EXECUTING
    assert outcome.intent.cleanup_state is CleanupState.PENDING
    assert outcome.intent.dispatched_at == NOW
    assert engine.dispatch_count == 1
    stored = ledger.get("intent-1")
    assert stored is not None and stored.version == outcome.intent.version


def test_same_intent_same_command_replays_without_dispatching_again() -> None:
    service, _, engine = build_service()
    first = service.submit(SubmitRequest(identity=identity_for(COMMAND), command=COMMAND))
    second = service.submit(SubmitRequest(identity=identity_for(COMMAND), command=COMMAND))

    assert first.idempotent_replay is False
    assert second.idempotent_replay is True
    assert engine.dispatch_count == 1
    assert second.intent.recorded_at == first.intent.recorded_at
    assert second.intent.execution_state is ExecutionState.EXECUTING


def test_same_intent_with_another_command_is_a_conflict() -> None:
    service, _, engine = build_service()
    service.submit(SubmitRequest(identity=identity_for(COMMAND), command=COMMAND))

    with pytest.raises(SubmitIntentConflictError) as raised:
        service.submit(SubmitRequest(identity=identity_for(OTHER_COMMAND), command=OTHER_COMMAND))

    assert raised.value.error_code is ExecutionErrorCode.SUBMIT_INTENT_CONFLICT
    assert engine.dispatch_count == 1


def test_digest_mismatch_is_rejected_without_dispatching() -> None:
    service, _, engine = build_service()
    identity = identity_for(COMMAND, command_digest=command_digest(OTHER_COMMAND))

    with pytest.raises(InvalidExecutionRequestError):
        service.submit(SubmitRequest(identity=identity, command=COMMAND))

    assert engine.dispatch_count == 0


def test_lost_dispatch_confirmation_quarantines_and_holds_capacity() -> None:
    service, ledger, engine = build_service()
    engine.script_outcome(DispatchOutcome(result=DispatchResult.RESPONSE_LOST, observed_at=NOW))

    outcome = service.submit(SubmitRequest(identity=identity_for(COMMAND), command=COMMAND))

    assert outcome.intent.execution_state is ExecutionState.UNKNOWN_QUARANTINED
    assert outcome.intent.cleanup_state is CleanupState.QUARANTINED
    assert outcome.intent.error_code is ExecutionErrorCode.ENGINE_TRANSPORT_UNKNOWN
    assert outcome.intent.execution_state.holds_capacity is True
    assert outcome.intent.stop_evidence_kind is StopEvidenceKind.NONE

    replay = service.submit(SubmitRequest(identity=identity_for(COMMAND), command=COMMAND))
    assert replay.idempotent_replay is True
    assert replay.intent.execution_state is ExecutionState.UNKNOWN_QUARANTINED
    assert engine.dispatch_count == 1
    assert ledger.get("intent-1") is not None


def test_quarantined_intent_adopts_engine_fact_without_redispatching() -> None:
    service, _, engine = build_service()
    engine.script_outcome(DispatchOutcome(result=DispatchResult.RESPONSE_LOST, observed_at=NOW))
    service.submit(SubmitRequest(identity=identity_for(COMMAND), command=COMMAND))
    engine.script_observation(
        "intent-1",
        EngineObservation("engine-request-late", EngineObservedState.EXECUTING),
    )

    outcome = service.submit(SubmitRequest(identity=identity_for(COMMAND), command=COMMAND))

    assert outcome.intent.execution_state is ExecutionState.EXECUTING
    assert outcome.intent.engine_request_id == "engine-request-late"
    assert engine.dispatch_count == 1


def test_failure_before_send_releases_capacity_without_stop_evidence() -> None:
    service, _, engine = build_service()
    engine.script_outcome(DispatchOutcome(result=DispatchResult.NOT_SENT, observed_at=NOW))

    outcome = service.submit(SubmitRequest(identity=identity_for(COMMAND), command=COMMAND))

    assert outcome.intent.execution_state is ExecutionState.FAILED
    assert outcome.intent.cleanup_state is CleanupState.NOT_REQUIRED
    assert outcome.intent.dispatched_at is None
    assert outcome.intent.engine_request_id is None
    assert outcome.intent.stop_evidence_kind is StopEvidenceKind.NONE
    assert outcome.intent.execution_state.holds_capacity is False


def test_stop_requires_stop_and_release_confirmation() -> None:
    service, _, engine = build_service()
    service.submit(SubmitRequest(identity=identity_for(COMMAND), command=COMMAND))
    partial = stop_evidence(execution_stopped=True, resources_released=False)

    with pytest.raises(CancellationUnconfirmedError):
        service.record_stop(partial)

    assert engine.dispatch_count == 1
    still_active = service.query("intent-1")
    assert still_active is not None
    assert still_active.execution_state is ExecutionState.EXECUTING
    assert still_active.execution_state.holds_capacity is True

    released = service.record_stop(stop_evidence(execution_stopped=True, resources_released=True))
    assert released.execution_state is ExecutionState.ABORTED
    assert released.cleanup_state is CleanupState.COMPLETE
    assert released.stop_evidence_kind is StopEvidenceKind.ENGINE_ABORT_ACK
    assert released.execution_state.holds_capacity is False


def test_unconfirmed_timeout_evidence_never_verifies() -> None:
    with pytest.raises(CancellationUnconfirmedError):
        service, _, _ = build_service()
        service.submit(SubmitRequest(identity=identity_for(COMMAND), command=COMMAND))
        service.record_stop(
            stop_evidence(
                execution_stopped=True,
                resources_released=True,
                kind=StopEvidenceKind.UNCONFIRMED_TIMEOUT,
            )
        )


def test_stop_evidence_for_another_intent_is_rejected_by_the_domain() -> None:
    intent = SubmissionIntent(identity=identity_for(COMMAND), recorded_at=NOW, updated_at=NOW)
    intent.mark_dispatched("engine-request-1", at=NOW)

    with pytest.raises(ValueError, match="another submit intent"):
        intent.apply_stop_evidence(
            stop_evidence(submit_intent_id="intent-2"),
            terminal_state=ExecutionState.ABORTED,
            at=NOW,
        )


def test_stop_evidence_for_unknown_intent_is_invalid() -> None:
    service, _, _ = build_service()

    with pytest.raises(InvalidExecutionRequestError, match="unknown submit intent"):
        service.record_stop(stop_evidence(submit_intent_id="intent-2"))


def test_stop_evidence_requires_a_dispatched_intent() -> None:
    service, _, engine = build_service()
    engine.script_outcome(DispatchOutcome(result=DispatchResult.NOT_SENT, observed_at=NOW))
    service.submit(SubmitRequest(identity=identity_for(COMMAND), command=COMMAND))

    with pytest.raises(ValueError, match="dispatched intent"):
        service.record_stop(stop_evidence())


def test_unknown_intent_has_no_state() -> None:
    service, _, _ = build_service()
    assert service.query("intent-missing") is None


def test_success_records_the_engine_terminal_response() -> None:
    service, _, _ = build_service()
    service.submit(SubmitRequest(identity=identity_for(COMMAND), command=COMMAND))
    intent = service.query("intent-1")
    assert intent is not None

    intent.complete_successfully(at=NOW)

    assert intent.execution_state is ExecutionState.SUCCEEDED
    assert intent.cleanup_state is CleanupState.COMPLETE
    assert intent.stop_evidence_kind is StopEvidenceKind.ENGINE_TERMINAL_RESPONSE
    assert intent.error_code is None


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"command_digest": "sha256:short"}, "command_digest"),
        ({"instance_generation": 0}, "instance_generation"),
        ({"fencing_token": 0}, "fencing_token"),
        ({"max_output_audio_ms": 5000}, "sample_rate_hz"),
        ({"max_output_audio_ms": 5000, "sample_rate_hz": 8000}, "sample_rate_hz"),
    ],
)
def test_identity_validation(overrides: dict[str, object], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        identity_for(COMMAND, **overrides)


@pytest.mark.parametrize(
    ("state", "cleanup"),
    [
        (ExecutionState.SUCCEEDED, CleanupState.PENDING),
        (ExecutionState.ABORTED, CleanupState.QUARANTINED),
        (ExecutionState.UNKNOWN_QUARANTINED, CleanupState.PENDING),
        (ExecutionState.EXECUTING, CleanupState.COMPLETE),
    ],
)
def test_state_matrix_rejects_illegal_combinations(
    state: ExecutionState, cleanup: CleanupState
) -> None:
    with pytest.raises(ValueError, match="cleanup_state"):
        SubmissionIntent(
            identity=identity_for(COMMAND),
            recorded_at=NOW,
            updated_at=NOW,
            execution_state=state,
            cleanup_state=cleanup,
        )


def test_failed_intent_requires_error_code_and_no_dispatch_when_not_required() -> None:
    with pytest.raises(ValueError, match="error code"):
        SubmissionIntent(
            identity=identity_for(COMMAND),
            recorded_at=NOW,
            updated_at=NOW,
            execution_state=ExecutionState.FAILED,
            cleanup_state=CleanupState.NOT_REQUIRED,
        )
    with pytest.raises(ValueError, match="requires a dispatch time"):
        SubmissionIntent(
            identity=identity_for(COMMAND),
            recorded_at=NOW,
            updated_at=NOW,
            execution_state=ExecutionState.FAILED,
            cleanup_state=CleanupState.COMPLETE,
            error_code=ExecutionErrorCode.ENGINE_TRANSPORT_UNKNOWN,
        )
    with pytest.raises(ValueError, match="no dispatch time"):
        SubmissionIntent(
            identity=identity_for(COMMAND),
            recorded_at=NOW,
            updated_at=NOW,
            execution_state=ExecutionState.FAILED,
            cleanup_state=CleanupState.NOT_REQUIRED,
            error_code=ExecutionErrorCode.ENGINE_TRANSPORT_UNKNOWN,
            dispatched_at=NOW,
        )


def test_successful_intent_rejects_error_codes() -> None:
    with pytest.raises(ValueError, match="error code"):
        SubmissionIntent(
            identity=identity_for(COMMAND),
            recorded_at=NOW,
            updated_at=NOW,
            execution_state=ExecutionState.SUCCEEDED,
            cleanup_state=CleanupState.COMPLETE,
            dispatched_at=NOW,
            error_code=ExecutionErrorCode.ENGINE_TRANSPORT_UNKNOWN,
            stop_evidence_kind=StopEvidenceKind.ENGINE_TERMINAL_RESPONSE,
        )


def test_stop_evidence_digest_is_validated() -> None:
    with pytest.raises(ValueError, match="evidence_digest"):
        stop_evidence(evidence_digest="not-a-digest")


def stop_evidence(
    *,
    submit_intent_id: str = "intent-1",
    execution_stopped: bool = True,
    resources_released: bool = True,
    kind: StopEvidenceKind = StopEvidenceKind.ENGINE_ABORT_ACK,
    evidence_digest: str = "sha256:" + "a" * 64,
) -> VerifiedStopEvidence:
    return VerifiedStopEvidence(
        evidence_id="evidence-1",
        submit_intent_id=submit_intent_id,
        evidence_kind=kind,
        evidence_digest=evidence_digest,
        observed_at_us=1_800_000_000_000_000,
        execution_stopped=execution_stopped,
        resources_released=resources_released,
        reason="unit-test",
    )
