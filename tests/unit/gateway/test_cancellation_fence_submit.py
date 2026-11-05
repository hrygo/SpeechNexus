"""A cancellation fence refuses submit before any engine dispatch."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from speechnexus_gateway.application.submit_execution import SubmitExecutionService
from speechnexus_gateway.domain.canonical import JsonValue, command_digest
from speechnexus_gateway.domain.submission import (
    CancelRequestedError,
    ExecutionErrorCode,
    ExecutionState,
    SubmissionIdentity,
    SubmissionIntent,
    SubmitRequest,
)
from tests.fake.execution import InMemorySubmissionLedger, ScriptedEngine, fake_ingress

NOW = datetime(2026, 10, 24, tzinfo=UTC)
COMMAND: JsonValue = {"schema_version": "1", "task": "tts.synthesize", "input": {"text": "hi"}}


def identity() -> SubmissionIdentity:
    return SubmissionIdentity(
        submit_intent_id="intent-fence-1",
        lease_id="lease-1",
        operation_id="job-fence-1",
        attempt_id="attempt-1",
        stage_id="stage-1",
        workspace_id="ws-1",
        pool_id="tts-realtime",
        host_node_id="node-1",
        gateway_id="gateway-1",
        instance_id="instance-1",
        instance_generation=3,
        binding_revision_id="binding-rev-1",
        policy_revision_id="policy-rev-1",
        voice_revision_id=None,
        fencing_token=7,
        command_digest=command_digest(COMMAND),
        deadline_at=NOW + timedelta(minutes=5),
        max_output_audio_ms=2000,
        sample_rate_hz=24000,
    )


class FenceLedger(InMemorySubmissionLedger):
    """Stores the terminal fact the PostgreSQL adapter writes under the fence."""

    def __init__(self) -> None:
        super().__init__(now=lambda: NOW)
        self.refused = False

    def add(self, intent: SubmissionIntent, *, request_document: str) -> SubmissionIntent | None:
        refused = intent.refuse_for_cancellation(at=NOW)
        self._intents[refused.identity.submit_intent_id] = refused
        self.refused = True
        return refused


def service(ledger: InMemorySubmissionLedger, engine: ScriptedEngine) -> SubmitExecutionService:
    return SubmitExecutionService(
        ingress=fake_ingress(),
        ledger=ledger,
        dispatch=engine,
        observation=engine,
        now=lambda: NOW,
    )


def test_fence_refusal_is_terminal_and_never_dispatches() -> None:
    ledger = FenceLedger()
    engine = ScriptedEngine(now=lambda: NOW)
    request = SubmitRequest(identity=identity(), command=COMMAND, output_stream_id="tts-main")

    with pytest.raises(CancelRequestedError) as raised:
        service(ledger, engine).submit(request)

    assert raised.value.error_code is ExecutionErrorCode.CANCEL_REQUESTED
    assert engine.dispatch_count == 0
    stored = ledger.get("intent-fence-1")
    assert stored is not None
    assert stored.refused_by_cancellation
    assert stored.execution_state is ExecutionState.FAILED
    assert stored.error_code is ExecutionErrorCode.CANCEL_REQUESTED
    assert stored.dispatched_at is None
    assert stored.engine_request_id is None


def test_repeated_submit_returns_the_same_distinguishable_refusal() -> None:
    ledger = FenceLedger()
    engine = ScriptedEngine(now=lambda: NOW)
    request = SubmitRequest(identity=identity(), command=COMMAND, output_stream_id="tts-main")
    instance = service(ledger, engine)

    for _ in range(2):
        with pytest.raises(CancelRequestedError):
            instance.submit(request)

    assert engine.dispatch_count == 0
    assert ledger.refused


def test_unrelated_submit_still_dispatches_once() -> None:
    ledger = InMemorySubmissionLedger(now=lambda: NOW)
    engine = ScriptedEngine(now=lambda: NOW)
    request = SubmitRequest(identity=identity(), command=COMMAND, output_stream_id="tts-main")

    outcome = service(ledger, engine).submit(request)

    assert outcome.idempotent_replay is False
    assert engine.dispatch_count == 1
    assert outcome.intent.execution_state is ExecutionState.EXECUTING
