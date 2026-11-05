"""Recovery re-sends the original cancellation; it never guesses or invents a stop."""

from collections.abc import Callable
from dataclasses import replace
from datetime import datetime, timedelta
from typing import Any

from speechnexus_gateway.application.abort_execution import ExecutionAbortService
from speechnexus_gateway.application.ingress import GatewayIngress
from speechnexus_gateway.domain.abort_intent import (
    AbortDispatchClaim,
    AbortDispatchOutcome,
    AbortExecutionRequest,
    abort_dispatch_lease_ms,
)
from speechnexus_gateway.domain.ports import AbortOutcome
from speechnexus_gateway.domain.submission import (
    AbortAcknowledgement,
    ExecutionState,
    SubmissionIntent,
)
from tests.fake.execution import ScriptedAbort
from tests.unit.gateway.test_abort_receive_order import setup_abort
from tests.unit.gateway.test_submit_execution import NOW


class Clock:
    def __init__(self) -> None:
        self.at = NOW

    def __call__(self) -> datetime:
        return self.at

    def advance(self, **delta: float) -> None:
        self.at = self.at + timedelta(**delta)


class RecordingAbort(ScriptedAbort):
    def __init__(self, now: Callable[[], datetime]) -> None:
        super().__init__(now=now)
        self.intents: list[SubmissionIntent] = []

    def abort(self, intent: SubmissionIntent, *, reason: str, wait_timeout_ms: int) -> AbortOutcome:
        self.intents.append(intent)
        return super().abort(intent, reason=reason, wait_timeout_ms=wait_timeout_ms)


def service_for(ledger: Any, ingress: GatewayIngress, engine: Any) -> ExecutionAbortService:
    return ExecutionAbortService(
        ingress=ingress, ledger=ledger, abort=engine, durable_abort_enabled=True
    )


def test_received_abort_is_dispatched_after_restart() -> None:
    clock = Clock()
    _, ledger, _, ingress, original, request = setup_abort(now=clock)
    ledger.receive_abort(request)
    engine = RecordingAbort(now=clock)
    service_for(ledger, ingress, engine).recover_abort(request)
    assert len(engine.intents) == 1
    assert engine.intents[0].identity == original.identity
    assert engine.intents[0].request_digest == original.request_digest
    assert ledger.receipt(request.submit_intent_id) == request
    state = ledger.abort_dispatch_state(request.submit_intent_id)
    assert state is not None and state["outcome"] == AbortDispatchOutcome.UNCONFIRMED.value


def test_live_attempt_lease_blocks_a_second_sender_until_it_expires() -> None:
    clock = Clock()
    _, ledger, _, ingress, original, request = setup_abort(now=clock)
    ledger.receive_abort(request)
    ledger.claim_abort(
        request, attempt_id="abort-first", lease_ms=abort_dispatch_lease_ms(request.wait_timeout_ms)
    )
    engine = RecordingAbort(now=clock)
    restarted = service_for(ledger, ingress, engine)
    restarted.recover_abort(request)
    assert engine.calls == []
    clock.advance(milliseconds=abort_dispatch_lease_ms(request.wait_timeout_ms) + 1)
    restarted.recover_abort(request)
    assert len(engine.intents) == 1
    assert engine.intents[0].identity == original.identity
    assert engine.intents[0].request_digest == original.request_digest
    stored = ledger.get(request.submit_intent_id)
    assert stored is not None and engine.intents[0].engine_request_id == stored.engine_request_id
    state = ledger.abort_dispatch_state(request.submit_intent_id)
    assert state is not None and state["attempt_id"] != "abort-first"


def test_decided_engine_answer_ends_the_attempts_of_the_receipt() -> None:
    clock = Clock()
    _, ledger, _, ingress, _, request = setup_abort(now=clock)
    ledger.receive_abort(request)
    engine = RecordingAbort(now=clock)
    stored = ledger.get(request.submit_intent_id)
    assert stored is not None
    engine.script_outcome(
        AbortOutcome(
            observed_at=clock(),
            engine_request_id=stored.engine_request_id,
            acknowledgement=AbortAcknowledgement.ALREADY_TERMINAL,
        )
    )
    restarted = service_for(ledger, ingress, engine)
    restarted.recover_abort(request)
    assert len(engine.calls) == 1
    clock.advance(minutes=5)
    restarted.recover_abort(request)
    assert len(engine.calls) == 1
    state = ledger.abort_dispatch_state(request.submit_intent_id)
    assert state is not None and state["outcome"] == AbortDispatchOutcome.ACKNOWLEDGED.value


def test_terminal_execution_is_never_redispatched() -> None:
    clock = Clock()
    _, ledger, _, ingress, _, request = setup_abort(now=clock)
    ledger.receive_abort(request)
    ledger._intents[request.submit_intent_id].execution_state = ExecutionState.FAILED
    engine = RecordingAbort(now=clock)
    service_for(ledger, ingress, engine).recover_abort(request)
    assert engine.calls == []


def test_stale_authority_is_refused_with_a_durable_outcome() -> None:
    clock = Clock()
    _, ledger, _, ingress, _, request = setup_abort(now=clock)
    ledger.receive_abort(request)
    current = ledger.get(request.submit_intent_id)
    assert current is not None
    ledger._intents[request.submit_intent_id] = replace(
        current, identity=replace(current.identity, fencing_token=2)
    )
    engine = RecordingAbort(now=clock)
    restarted = service_for(ledger, ingress, engine)
    restarted.recover_abort(request)
    assert engine.calls == []
    state = ledger.abort_dispatch_state(request.submit_intent_id)
    assert state is not None and state["outcome"] == AbortDispatchOutcome.REJECTED_FENCED.value
    clock.advance(minutes=5)
    restarted.recover_abort(request)
    assert engine.calls == []


def test_http_abort_with_stale_fencing_is_still_a_rejection() -> None:
    import pytest

    from speechnexus_gateway.domain.submission import StaleFencingTokenError

    clock = Clock()
    _, ledger, _, ingress, _, request = setup_abort(now=clock)
    ledger.receive_abort(request)
    current = ledger.get(request.submit_intent_id)
    assert current is not None
    ledger._intents[request.submit_intent_id] = replace(
        current, identity=replace(current.identity, fencing_token=2)
    )
    engine = RecordingAbort(now=clock)
    with pytest.raises(StaleFencingTokenError):
        service_for(ledger, ingress, engine).abort(request)
    assert engine.calls == []


def test_sweep_resumes_due_receipts_and_continues_after_failure() -> None:
    from speechnexus_gateway.application.recover_aborts import AbortRecoveryService

    clock = Clock()
    _, ledger, _, ingress, _, request = setup_abort(now=clock)
    ledger.receive_abort(request)

    class Due:
        def __init__(self) -> None:
            self.taken = False

        def take_due_aborts(
            self, *, gateway_id: str, host_node_id: str, limit: int
        ) -> list[AbortExecutionRequest]:
            assert (gateway_id, host_node_id, limit) == ("gateway-1", "node-1", 1)
            if self.taken:
                return []
            self.taken = True
            return [request]

    engine = RecordingAbort(now=clock)
    service = service_for(ledger, ingress, engine)
    assert AbortRecoveryService(ingress=ingress, ledger=Due(), abort=service).sweep(limit=2) == 1
    assert len(engine.calls) == 1


def test_unknown_claim_commit_keeps_the_attempt_and_retries_after_the_lease() -> None:
    import pytest

    from speechnexus_gateway.domain.submission import SubmissionOutcomeUnknownError

    clock = Clock()
    _, ledger, _, ingress, _, request = setup_abort(now=clock)
    original_claim = ledger.claim_abort

    def claim_then_lose_reply(
        abort_request: AbortExecutionRequest, *, attempt_id: str, lease_ms: int
    ) -> AbortDispatchClaim | None:
        original_claim(abort_request, attempt_id=attempt_id, lease_ms=lease_ms)
        raise SubmissionOutcomeUnknownError("synthetic lost commit acknowledgement")

    ledger.claim_abort = claim_then_lose_reply  # type: ignore[method-assign,assignment]
    engine = RecordingAbort(now=clock)
    service = service_for(ledger, ingress, engine)
    with pytest.raises(SubmissionOutcomeUnknownError):
        service.recover_abort(request)
    assert engine.calls == []
    ledger.claim_abort = original_claim  # type: ignore[method-assign]
    # The committed claim still owns a live lease, so no second sender appears.
    service.recover_abort(request)
    assert engine.calls == []
    clock.advance(milliseconds=abort_dispatch_lease_ms(request.wait_timeout_ms) + 1)
    service.recover_abort(request)
    assert len(engine.calls) == 1
