"""The file worker closes an engine-free completion with the stage's own record.

`test_file_execution_worker.py` pins both halves of the old shape: a total attestation
closes success, and a missing terminal fact keeps capacity. What it could not express is
the live shape that held leases forever -- a stage that finished with a private result and
had never called the engine at all. These tests drive that through the real worker, so
removing the convergence, or widening it to a dispatched run, fails here.
"""

from __future__ import annotations

from datetime import UTC, datetime

from speechnexus_gateway.domain.submission import (
    ExecutionErrorCode,
    ExecutionState,
    StopEvidenceKind,
)
from speechnexus_gateway.domain.terminal import DispatchAttestation
from tests.fake.execution_command import original_request
from tests.unit.gateway.test_file_execution_worker import (
    Pipeline,
    configured,
    stored,
    wait_for,
)


class UndispatchedPipeline(Pipeline):
    """A stage that ran entirely locally: a private result and zero engine requests."""

    def __init__(self, *, dispatched: int = 0) -> None:
        super().__init__()
        self.attest = False
        self.dispatched = dispatched
        self.dispatch_attestation: DispatchAttestation | None = None

    async def stage_result(self, request, *, store, cancelled):  # type: ignore[no-untyped-def]
        outcome = await super().stage_result(request, store=store, cancelled=cancelled)
        assert outcome.terminal is None
        self.dispatch_attestation = DispatchAttestation(
            self.dispatched,
            0,
            datetime.now(UTC),
        )
        return outcome.__class__(outcome.result, None, self.dispatch_attestation)


async def test_a_stage_that_never_dispatched_closes_success_and_releases() -> None:
    pipeline = UndispatchedPipeline()
    with configured(pipeline=pipeline) as (ledger, _, results, worker, submit, _, _):
        request = original_request()
        submit.submit(request)
        await wait_for(lambda: stored(ledger, request).execution_state is ExecutionState.SUCCEEDED)

        current = stored(ledger, request)
        assert current.staging_result is not None and results.calls == 1
        assert current.stop_evidence_kind is StopEvidenceKind.NOT_DISPATCHED
        assert current.cleanup_state.value == "complete"
        assert not current.execution_state.holds_capacity
        evidence = ledger._evidence[request.identity.submit_intent_id]
        assert evidence.evidence_kind is StopEvidenceKind.NOT_DISPATCHED
        assert evidence.execution_stopped and evidence.resources_released
        assert worker.running


async def test_a_dispatched_stage_without_a_terminal_still_holds_capacity() -> None:
    """One request left this process, so the undispatched proof must not apply."""
    pipeline = UndispatchedPipeline(dispatched=1)
    with configured(pipeline=pipeline) as (ledger, _, _, worker, submit, _, _):
        request = original_request()
        submit.submit(request)
        await wait_for(
            lambda: stored(ledger, request).execution_state is ExecutionState.UNKNOWN_QUARANTINED
        )

        current = stored(ledger, request)
        assert current.error_code is ExecutionErrorCode.EXECUTION_CLEANUP_PENDING
        assert current.execution_state.holds_capacity
        assert not ledger._evidence
        assert worker.running


async def test_a_stage_without_an_attestation_still_holds_capacity() -> None:
    pipeline = Pipeline()
    pipeline.attest = False
    with configured(pipeline=pipeline) as (ledger, _, _, _, submit, _, _):
        request = original_request()
        submit.submit(request)
        await wait_for(
            lambda: stored(ledger, request).execution_state is ExecutionState.UNKNOWN_QUARANTINED
        )

        assert not ledger._evidence
        assert stored(ledger, request).execution_state.holds_capacity
