"""Durable abort outcomes are counted, and the counts leave the process as one log line.

The gateway has no metrics endpoint, so the assertion target is the bounded aggregate
plus the structured log line; the receipt columns remain the authoritative export.
"""

import logging
from dataclasses import dataclass, replace
from datetime import datetime, timedelta

import pytest

from speechnexus_gateway.application.abort_execution import ExecutionAbortService
from speechnexus_gateway.application.dispatch_metrics import DispatchOutcomeCounters
from speechnexus_gateway.domain.abort_intent import (
    MAX_ABORT_DISPATCH_ATTEMPTS,
    AbortDispatchClaim,
    AbortDispatchOutcome,
    AbortExecutionRequest,
    abort_dispatch_lease_ms,
)
from speechnexus_gateway.domain.submission import StaleFencingTokenError
from speechnexus_gateway.infrastructure.recovery_worker import log_dispatch_outcome_change
from tests.fake.execution import InMemorySubmissionLedger, ScriptedAbort
from tests.unit.gateway.test_abort_receive_order import setup_abort
from tests.unit.gateway.test_submit_execution import NOW


class Clock:
    def __init__(self) -> None:
        self.at = NOW

    def __call__(self) -> datetime:
        return self.at

    def past_lease(self, wait_timeout_ms: int) -> None:
        self.at = self.at + timedelta(milliseconds=abort_dispatch_lease_ms(wait_timeout_ms) + 1)


@dataclass
class Undecided:
    """A received receipt whose adapter keeps answering "nothing learned yet"."""

    service: ExecutionAbortService
    ledger: InMemorySubmissionLedger
    request: AbortExecutionRequest
    clock: Clock


def undecided_service() -> Undecided:
    clock = Clock()
    _, ledger, _, ingress, _, request = setup_abort(now=clock)
    ledger.receive_abort(request)
    service = ExecutionAbortService(
        ingress=ingress, ledger=ledger, abort=ScriptedAbort(now=clock), durable_abort_enabled=True
    )
    return Undecided(service, ledger, request, clock)


def test_counts_grow_with_every_retry_and_end_in_one_exhausted(
    caplog: pytest.LogCaptureFixture,
) -> None:
    case = undecided_service()
    service = case.service
    counters = service.dispatch_outcome_counts
    for _ in range(MAX_ABORT_DISPATCH_ATTEMPTS):
        service.recover_abort(case.request)
        case.clock.past_lease(case.request.wait_timeout_ms)

    assert counters.snapshot() == {
        AbortDispatchOutcome.ACKNOWLEDGED.value: 0,
        AbortDispatchOutcome.UNCONFIRMED.value: MAX_ABORT_DISPATCH_ATTEMPTS - 1,
        AbortDispatchOutcome.EXHAUSTED.value: 1,
    }
    with caplog.at_level(logging.INFO, logger="speechnexus_gateway.infrastructure.recovery_worker"):
        log_dispatch_outcome_change(counters)
    assert "unconfirmed=7 exhausted=1" in caplog.text
    assert "submit-intent" not in caplog.text  # bounded labels, never an identity
    # Nothing moved, so a second pass emits nothing: the counters are the only state.
    caplog.clear()
    log_dispatch_outcome_change(counters)
    assert caplog.text == ""


def test_a_given_up_final_attempt_is_counted_where_it_is_written() -> None:
    """The crash-in-final-attempt give-up happens in the claim, and is still counted."""
    case = undecided_service()
    service = case.service
    for _ in range(MAX_ABORT_DISPATCH_ATTEMPTS):
        claim = case.ledger.claim_abort(
            case.request, attempt_id="abort-lost", lease_ms=abort_dispatch_lease_ms(1000)
        )
        assert isinstance(claim, AbortDispatchClaim)
        case.clock.past_lease(case.request.wait_timeout_ms)
    before = service.dispatch_outcome_counts.snapshot()
    service.recover_abort(case.request)
    after = service.dispatch_outcome_counts.snapshot()
    assert (
        after[AbortDispatchOutcome.EXHAUSTED.value]
        == before[AbortDispatchOutcome.EXHAUSTED.value] + 1
    )
    assert (
        after[AbortDispatchOutcome.UNCONFIRMED.value]
        == before[AbortDispatchOutcome.UNCONFIRMED.value]
    )


def test_drift_is_a_conflict_and_never_an_outcome() -> None:
    """Identity is immutable, so no counter exists for a durable fenced refusal."""
    clock = Clock()
    _, ledger, _, ingress, _, request = setup_abort(now=clock)
    ledger.receive_abort(request)
    current = ledger.get(request.submit_intent_id)
    assert current is not None
    ledger._intents[request.submit_intent_id] = replace(
        current, identity=replace(current.identity, fencing_token=2)
    )
    counters = DispatchOutcomeCounters()
    service = ExecutionAbortService(
        ingress=ingress,
        ledger=ledger,
        abort=ScriptedAbort(now=clock),
        durable_abort_enabled=True,
        counters=counters,
    )
    with pytest.raises(StaleFencingTokenError):
        service.recover_abort(request)
    assert set(counters.snapshot().values()) == {0}
