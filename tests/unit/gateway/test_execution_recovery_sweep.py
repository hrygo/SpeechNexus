"""Recovery sweep: adopt engine facts, close only on verified whole-instance termination."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from speechnexus_gateway.application.recover_executions import (
    MAX_SWEEP_LIMIT,
    ExecutionRecoveryService,
)
from speechnexus_gateway.application.submit_execution import SubmitExecutionService
from speechnexus_gateway.domain.canonical import JsonValue, command_digest
from speechnexus_gateway.domain.ports import DispatchOutcome, EngineObservation
from speechnexus_gateway.domain.recovery import (
    InstanceTerminationEvidence,
    MarkExecutionUnknown,
    whole_instance_stop_evidence,
)
from speechnexus_gateway.domain.submission import (
    CleanupState,
    DispatchResult,
    EngineObservedState,
    ExecutionErrorCode,
    ExecutionState,
    InvalidExecutionRequestError,
    StopEvidenceKind,
    SubmissionIdentity,
    SubmissionIntent,
    SubmitRequest,
)
from tests.fake.execution import InMemorySubmissionLedger, ScriptedEngine, fake_ingress

NOW = datetime(2026, 10, 24, tzinfo=UTC)
DIGEST = "sha256:" + "a" * 64
COMMAND: JsonValue = {"schema_version": "1", "task": "asr.transcribe", "input": {"asset": "a"}}


def identity_for(**overrides: object) -> SubmissionIdentity:
    fields: dict[str, object] = {
        "submit_intent_id": "intent-1",
        "lease_id": "lease-1",
        "operation_id": "op-1",
        "attempt_id": "attempt-1",
        "stage_id": "stage-1",
        "workspace_id": "ws-1",
        "pool_id": "tts-realtime",
        "host_node_id": "node-1",
        "gateway_id": "gateway-1",
        "instance_id": "instance-1",
        "instance_generation": 1,
        "binding_revision_id": "binding-rev-1",
        "policy_revision_id": "policy-rev-1",
        "voice_revision_id": None,
        "fencing_token": 1,
        "command_digest": command_digest(COMMAND),
        "deadline_at": NOW + timedelta(hours=1),
        "max_output_audio_ms": None,
        "sample_rate_hz": None,
    }
    fields.update(overrides)
    return SubmissionIdentity(**fields)  # type: ignore[arg-type]


def request_for(identity: SubmissionIdentity) -> SubmitRequest:
    return SubmitRequest(identity=identity, command=COMMAND)


class SpyLedger(InMemorySubmissionLedger):
    """Records which port methods a sweep uses, so an extra write becomes visible."""

    def __init__(self) -> None:
        super().__init__(now=lambda: NOW)
        self.used: list[str] = []

    def get(self, submit_intent_id: str) -> Any:
        self.used.append("get")
        return super().get(submit_intent_id)

    def query_outcome(self, submit_intent_id: str) -> Any:
        self.used.append("query_outcome")
        return super().query_outcome(submit_intent_id)

    def list_quarantined(self, **kwargs: Any) -> Any:
        self.used.append("list_quarantined")
        return super().list_quarantined(**kwargs)

    def mark_execution_unknown(self, request: Any) -> Any:
        self.used.append("mark_execution_unknown")
        return super().mark_execution_unknown(request)

    def record_execution_observation(self, request: Any) -> Any:
        self.used.append("record_execution_observation")
        return super().record_execution_observation(request)

    def record_stop(self, request: Any) -> Any:
        self.used.append("record_stop")
        return super().record_stop(request)


class ScriptedTerminations:
    """Reports only the whole-instance facts a test scripts; never invents one."""

    def __init__(self) -> None:
        self.evidence: dict[str, InstanceTerminationEvidence] = {}
        self.calls: list[str] = []

    def script(self, submit_intent_id: str, evidence: InstanceTerminationEvidence) -> None:
        self.evidence[submit_intent_id] = evidence

    def termination_evidence(self, intent: Any) -> InstanceTerminationEvidence | None:
        self.calls.append(intent.identity.submit_intent_id)
        return self.evidence.get(intent.identity.submit_intent_id)


def termination_evidence(**overrides: object) -> InstanceTerminationEvidence:
    fields: dict[str, object] = {
        "instance_id": "instance-1",
        "instance_generation": 1,
        "evidence_id": "stop-evidence-controller",
        "evidence_kind": StopEvidenceKind.INSTANCE_TERMINATED_BY_CONTROLLER,
        "evidence_digest": DIGEST,
        "observed_at": NOW,
        "process_exited": True,
        "resources_released": True,
        "reason": None,
    }
    fields.update(overrides)
    return InstanceTerminationEvidence(**fields)  # type: ignore[arg-type]


def quarantined(
    ledger: SpyLedger, *, identity: SubmissionIdentity | None = None
) -> tuple[SubmitExecutionService, ScriptedEngine, SubmissionIdentity]:
    """Submit once with a lost response, so the execution is durable but uncertain."""
    engine = ScriptedEngine(now=lambda: NOW)
    engine.script_outcome(DispatchOutcome(DispatchResult.RESPONSE_LOST, NOW))
    submissions = SubmitExecutionService(
        ingress=fake_ingress(node_id="node-1", gateway_id="gateway-1"),
        ledger=ledger,
        dispatch=engine,
        observation=engine,
        now=lambda: NOW,
    )
    resolved = identity or identity_for()
    intent = submissions.submit(request_for(resolved)).intent
    assert intent.execution_state is ExecutionState.UNKNOWN_QUARANTINED
    return submissions, engine, resolved


def sweep_for(
    ledger: SpyLedger,
    submissions: SubmitExecutionService,
    terminations: ScriptedTerminations,
) -> ExecutionRecoveryService:
    return ExecutionRecoveryService(
        ingress=fake_ingress(node_id="node-1", gateway_id="gateway-1"),
        ledger=ledger,
        submissions=submissions,
        terminations=terminations,
    )


def test_verified_termination_closes_the_execution_without_releasing_capacity() -> None:
    ledger = SpyLedger()
    submissions, _, identity = quarantined(ledger)
    terminations = ScriptedTerminations()
    terminations.script(identity.submit_intent_id, termination_evidence())
    before = ledger.get(identity.submit_intent_id)

    report = sweep_for(ledger, submissions, terminations).sweep()

    closed = ledger.get(identity.submit_intent_id)
    assert report.examined == 1
    assert (report.observed, report.closed, report.still_unknown) == (0, 1, 0)
    assert closed is not None and before is not None
    assert closed.execution_state is ExecutionState.ABORTED
    assert closed.cleanup_state is CleanupState.COMPLETE
    assert closed.stop_evidence_kind is StopEvidenceKind.INSTANCE_TERMINATED_BY_CONTROLLER
    assert closed.stop_evidence_digest == DIGEST
    # A response-lost submit keeps its recorded dispatch fact and never gains a request id.
    assert closed.dispatched_at == NOW and closed.engine_request_id is None
    assert closed.version == before.version + 1
    assert set(ledger.used) <= {
        "list_quarantined",
        "get",
        "query_outcome",
        "record_execution_observation",
        "record_stop",
    }


def test_one_termination_has_distinct_stable_evidence_for_each_execution() -> None:
    ledger = SpyLedger()
    _, _, first = quarantined(ledger)
    _, _, second = quarantined(
        ledger, identity=identity_for(submit_intent_id="intent-2", lease_id="lease-2")
    )
    first_intent, second_intent = (
        ledger.get(first.submit_intent_id),
        ledger.get(second.submit_intent_id),
    )
    assert first_intent is not None and second_intent is not None
    source = termination_evidence()

    first_stop = whole_instance_stop_evidence(first_intent, source)
    second_stop = whole_instance_stop_evidence(second_intent, source)

    assert first_stop is not None and second_stop is not None
    assert first_stop.evidence_id != second_stop.evidence_id
    assert first_stop == whole_instance_stop_evidence(first_intent, source)
    assert second_stop == whole_instance_stop_evidence(second_intent, source)
    assert first_stop.evidence_digest == second_stop.evidence_digest == source.evidence_digest
    assert first_stop.observed_at_us == second_stop.observed_at_us
    assert first_stop.submit_intent_id == first.submit_intent_id
    assert second_stop.submit_intent_id == second.submit_intent_id

    # An ID is an identity, not a payload hash. A changed fact for the same source
    # must still collide with its immutable record so record_stop rejects it.
    changed = whole_instance_stop_evidence(
        first_intent, replace(source, evidence_digest="sha256:" + "b" * 64)
    )
    assert changed is not None
    assert changed.evidence_id == first_stop.evidence_id
    assert changed != first_stop
    other_source = whole_instance_stop_evidence(
        first_intent, replace(source, evidence_id="stop-evidence-other")
    )
    assert other_source is not None
    assert other_source.evidence_id != first_stop.evidence_id


@pytest.mark.parametrize("source_id", ("BAD ID", "x", "a" * 129))
def test_a_derived_id_does_not_hide_an_invalid_source_evidence_identity(source_id: str) -> None:
    ledger = SpyLedger()
    _, _, identity = quarantined(ledger)
    intent = ledger.get(identity.submit_intent_id)
    assert intent is not None

    with pytest.raises(ValueError, match="identifiers must match"):
        whole_instance_stop_evidence(intent, termination_evidence(evidence_id=source_id))


@pytest.mark.parametrize(
    "overrides",
    [
        {"process_exited": False},
        {"resources_released": False},
        {"evidence_kind": StopEvidenceKind.ENGINE_ABORT_ACK},
        {"evidence_kind": StopEvidenceKind.UNCONFIRMED_TIMEOUT},
        {"instance_id": "instance-other"},
        {"instance_generation": 2},
        {"observed_at": NOW - timedelta(seconds=1)},
    ],
)
def test_incomplete_or_foreign_evidence_keeps_the_execution_quarantined(
    overrides: dict[str, object],
) -> None:
    ledger = SpyLedger()
    submissions, _, identity = quarantined(ledger)
    terminations = ScriptedTerminations()
    terminations.script(identity.submit_intent_id, termination_evidence(**overrides))
    before = ledger.get(identity.submit_intent_id)

    report = sweep_for(ledger, submissions, terminations).sweep()

    after = ledger.get(identity.submit_intent_id)
    assert (report.closed, report.still_unknown) == (0, 1)
    assert after == before
    assert after is not None
    assert after.execution_state is ExecutionState.UNKNOWN_QUARANTINED
    assert after.cleanup_state is CleanupState.QUARANTINED


def test_no_answer_from_the_producer_is_not_stop_evidence() -> None:
    ledger = SpyLedger()
    submissions, _, identity = quarantined(ledger)
    before = ledger.get(identity.submit_intent_id)

    report = sweep_for(ledger, submissions, ScriptedTerminations()).sweep()

    after = ledger.get(identity.submit_intent_id)
    assert (report.examined, report.closed, report.still_unknown) == (1, 0, 1)
    assert after == before


def test_a_late_engine_answer_is_adopted_without_closing_the_execution() -> None:
    ledger = SpyLedger()
    submissions, engine, identity = quarantined(ledger)
    engine.script_observation(
        identity.submit_intent_id,
        EngineObservation(
            engine_request_id="engine-request-1",
            state=EngineObservedState.EXECUTING,
            dispatched_at=None,
        ),
    )

    report = sweep_for(ledger, submissions, ScriptedTerminations()).sweep()

    adopted = ledger.get(identity.submit_intent_id)
    assert (report.observed, report.closed, report.still_unknown) == (1, 0, 0)
    assert adopted is not None
    assert adopted.execution_state is ExecutionState.EXECUTING
    assert adopted.engine_request_id == "engine-request-1"
    assert adopted.dispatched_at == NOW


def test_a_second_sweep_neither_repeats_nor_reopens_the_fact() -> None:
    ledger = SpyLedger()
    submissions, _, identity = quarantined(ledger)
    terminations = ScriptedTerminations()
    terminations.script(identity.submit_intent_id, termination_evidence())
    service = sweep_for(ledger, submissions, terminations)

    assert service.sweep().closed == 1
    settled = ledger.get(identity.submit_intent_id)

    second = service.sweep()

    assert second.examined == 0
    assert ledger.get(identity.submit_intent_id) == settled


def test_a_sweep_only_reads_this_gateway_and_its_own_bounded_batch() -> None:
    ledger = SpyLedger()
    for index in range(3):
        quarantined(ledger, identity=identity_for(submit_intent_id=f"intent-{index}"))
    foreign = identity_for(submit_intent_id="intent-foreign", gateway_id="gateway-other")
    foreign_request = request_for(foreign)
    ledger.add(
        SubmissionIntent(
            identity=foreign,
            recorded_at=NOW,
            updated_at=NOW,
            request_digest=foreign_request.request_digest,
        ),
        request_document=foreign_request.request_document,
    )
    foreign_intent = ledger.get("intent-foreign")
    assert foreign_intent is not None
    ledger.mark_execution_unknown(
        MarkExecutionUnknown.for_intent(
            foreign_intent, reason_code=ExecutionErrorCode.ENGINE_TRANSPORT_UNKNOWN, at=NOW
        )
    )
    submissions, _, _ = quarantined(ledger)
    terminations = ScriptedTerminations()

    report = sweep_for(ledger, submissions, terminations).sweep(limit=2)

    assert report.examined == 2
    assert report.still_unknown == 2
    assert "intent-foreign" not in terminations.calls


def test_a_sweep_limit_outside_the_frozen_bound_is_rejected() -> None:
    ledger = SpyLedger()
    submissions, _, _ = quarantined(ledger)
    service = sweep_for(ledger, submissions, ScriptedTerminations())

    for limit in (0, MAX_SWEEP_LIMIT + 1):
        with pytest.raises(InvalidExecutionRequestError, match="between 1 and"):
            service.sweep(limit=limit)
