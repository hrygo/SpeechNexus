"""Recovery preserves the original cancellation and never guesses a prior send."""

from speechnexus_gateway.application.abort_execution import ExecutionAbortService
from speechnexus_gateway.domain.abort_intent import AbortExecutionRequest
from speechnexus_gateway.domain.ports import AbortOutcome
from speechnexus_gateway.domain.submission import SubmissionIntent
from tests.unit.gateway.test_abort_receive_order import setup_abort


def test_received_abort_is_dispatched_after_restart() -> None:
    _, ledger, engine, ingress, _, request = setup_abort()
    ledger.receive_abort(request)
    restarted = ExecutionAbortService(
        ingress=ingress, ledger=ledger, abort=engine, durable_abort_enabled=True
    )
    restarted.recover_abort(request)
    assert len(engine.calls) == 1
    assert ledger.receipt(request.submit_intent_id) == request


def test_claimed_abort_without_safe_recovery_is_not_redispatched() -> None:
    _, ledger, engine, ingress, _, request = setup_abort()
    ledger.receive_abort(request)
    ledger.claim_abort(request)
    restarted = ExecutionAbortService(
        ingress=ingress, ledger=ledger, abort=engine, durable_abort_enabled=True
    )
    restarted.recover_abort(request)
    assert engine.calls == []


def test_sweep_resumes_due_receipts_and_continues_after_failure() -> None:
    from speechnexus_gateway.application.recover_aborts import AbortRecoveryService

    _, ledger, engine, ingress, _, request = setup_abort()
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

    service = ExecutionAbortService(
        ingress=ingress, ledger=ledger, abort=engine, durable_abort_enabled=True
    )
    assert AbortRecoveryService(ingress=ingress, ledger=Due(), abort=service).sweep(limit=2) == 1
    assert len(engine.calls) == 1


def test_claimed_abort_uses_only_explicit_safe_original_recovery() -> None:
    _, ledger, engine, ingress, original, request = setup_abort()
    ledger.receive_abort(request)
    claimed = ledger.claim_abort(request)
    assert claimed is not None

    class SafeRecovery:
        def __init__(self) -> None:
            self.calls: list[tuple[SubmissionIntent, str, int]] = []

        def recover_abort(
            self, intent: SubmissionIntent, *, reason: str, wait_timeout_ms: int
        ) -> AbortOutcome:
            self.calls.append((intent, reason, wait_timeout_ms))
            return engine.abort(intent, reason=reason, wait_timeout_ms=wait_timeout_ms)

    safe = SafeRecovery()
    restarted = ExecutionAbortService(
        ingress=ingress, ledger=ledger, abort=engine, durable_abort_enabled=True, recovery=safe
    )
    restarted.recover_abort(request)
    assert safe.calls == [(claimed, request.reason, request.wait_timeout_ms)]
    assert claimed.identity == original.identity
    assert claimed.request_digest == original.request_digest


def test_sweep_does_not_defer_unvisited_receipts_when_budget_expires() -> None:
    from speechnexus_gateway.application.recover_aborts import AbortRecoveryService

    _, ledger, engine, ingress, _, request = setup_abort()
    ledger.receive_abort(request)

    class Due:
        def __init__(self) -> None:
            self.calls = 0

        def take_due_aborts(
            self, *, gateway_id: str, host_node_id: str, limit: int
        ) -> list[AbortExecutionRequest]:
            self.calls += 1
            return [request]

    queue = Due()
    abort = ExecutionAbortService(
        ingress=ingress, ledger=ledger, abort=engine, durable_abort_enabled=True
    )
    service = AbortRecoveryService(ingress=ingress, ledger=queue, abort=abort)
    assert service.sweep(limit=10, should_stop=lambda: queue.calls >= 1) == 1
    assert queue.calls == 1
