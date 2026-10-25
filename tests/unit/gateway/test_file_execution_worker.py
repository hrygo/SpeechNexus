"""Real worker lifecycle with synthetic inference and no model or network access."""

import asyncio
import copy
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any

import pytest

from speechnexus_gateway.application.abort_execution import (
    AbortExecutionRequest,
    ExecutionAbortService,
)
from speechnexus_gateway.application.command_dispatch import OriginalCommandDispatch
from speechnexus_gateway.application.submit_execution import SubmitExecutionService
from speechnexus_gateway.domain.asr_results import (
    AsrResultStore,
    PreparedAsrResult,
    StagedAsrResult,
)
from speechnexus_gateway.domain.observation import RecordExecutionObservation
from speechnexus_gateway.domain.submission import (
    ExecutionState,
    SubmissionIntent,
    SubmissionOutcomeUnknownError,
    SubmitRequest,
)
from speechnexus_gateway.infrastructure.file_execution_worker import FileExecutionWorker
from tests.fake.execution import InMemorySubmissionLedger, fake_ingress
from tests.fake.execution_command import original_request


class Results:
    def __init__(self) -> None:
        self.calls = 0

    async def stage(
        self, result: PreparedAsrResult, *, cancelled: Callable[[], bool]
    ) -> StagedAsrResult:
        assert not cancelled()
        self.calls += 1
        return StagedAsrResult(result.request_digest, result.reference("synthetic-private"), "v1")


class Pipeline:
    def __init__(self) -> None:
        self.calls = 0
        self.started = threading.Event()
        self.finish = threading.Event()
        self.finish.set()
        self.before_stage: Callable[[], None] = lambda: None

    def validate_request(self, request: SubmitRequest) -> SubmitRequest:
        return copy.deepcopy(request)

    async def stage_result(
        self,
        request: SubmitRequest,
        *,
        store: AsrResultStore,
        max_result_bytes: int,
        cancelled: Callable[[], bool],
    ) -> StagedAsrResult:
        self.calls += 1
        self.before_stage()
        self.started.set()
        while not self.finish.is_set():
            if cancelled():
                raise asyncio.CancelledError
            await asyncio.sleep(0.002)
        assert max_result_bytes == 1024
        return await store.stage(
            PreparedAsrResult(
                request.identity.workspace_id, request.request_digest, b'{"text":""}'
            ),
            cancelled=cancelled,
        )


@contextmanager
def configured(
    ledger: InMemorySubmissionLedger | None = None, pipeline: Pipeline | None = None
) -> Iterator[tuple[Any, ...]]:
    ledger = ledger or InMemorySubmissionLedger()
    pipeline = pipeline or Pipeline()
    ingress = fake_ingress(node_id="node-input", gateway_id="gateway-input", timeout_seconds=0.05)
    results = Results()
    worker = FileExecutionWorker(
        ingress=ingress,
        ledger=ledger,
        execution=pipeline,
        results=results,
        max_result_bytes=1024,
        max_command_bytes=16384,
        max_pending=2,
    )
    worker.open()
    submit = SubmitExecutionService(
        ingress=ingress,
        ledger=ledger,
        dispatch=OriginalCommandDispatch(commands=ledger, engine=worker, max_document_bytes=16384),
        observation=worker,
        committed_execution=worker,
    )
    abort = ExecutionAbortService(ingress=ingress, ledger=ledger, abort=worker)
    try:
        yield ledger, pipeline, results, worker, submit, abort, ingress
    finally:
        pipeline.finish.set()
        worker.close()


def _wait_for_thread(check: Callable[[], bool]) -> None:
    deadline = time.monotonic() + 3
    while not check():
        assert time.monotonic() < deadline, "worker did not produce the expected state"
        time.sleep(0.005)


async def wait_for(check: Callable[[], bool]) -> None:
    await asyncio.to_thread(_wait_for_thread, check)


def stored(ledger: InMemorySubmissionLedger, request: SubmitRequest) -> SubmissionIntent:
    value = ledger.get(request.identity.submit_intent_id)
    assert value is not None
    return value


async def test_only_new_committed_acceptance_runs_and_ingress_is_free() -> None:
    with configured() as (ledger, pipeline, results, worker, submit, _, ingress):
        request = original_request()

        def before_stage() -> None:
            current = stored(ledger, request)
            assert current is not None and current.execution_state is ExecutionState.EXECUTING
            # If the submit service retained its short send lock, this times out.
            with ingress.dispatch(request.identity):
                pass

        pipeline.before_stage = before_stage
        first = submit.submit(request)
        assert not first.idempotent_replay
        await wait_for(lambda: stored(ledger, request).staging_result is not None)
        for _ in range(3):
            assert submit.submit(request).idempotent_replay
        assert pipeline.calls == results.calls == 1
        current = stored(ledger, request)
        assert current.engine_request_id == first.intent.engine_request_id
        assert current.execution_state is ExecutionState.EXECUTING
        assert current.execution_state.holds_capacity and not ledger._evidence
        assert worker.running


class LostAcceptance(InMemorySubmissionLedger):
    def record_execution_observation(self, request: RecordExecutionObservation) -> SubmissionIntent:
        result = super().record_execution_observation(request)
        if request.staging_result is None:
            raise SubmissionOutcomeUnknownError("synthetic acceptance commit ACK lost")
        return result


async def test_acceptance_commit_ack_loss_never_starts_and_replay_never_redispatches() -> None:
    ledger = LostAcceptance()
    request = original_request()
    with configured(ledger) as (_, pipeline, results, worker, submit, _, _):
        with pytest.raises(SubmissionOutcomeUnknownError):
            submit.submit(request)
        assert submit.submit(request).idempotent_replay
        assert stored(ledger, request).execution_state is ExecutionState.EXECUTING
        assert pipeline.calls == results.calls == 0
        assert not worker._preparations
    # Startup recovers the original record as unknown, never as a runnable item.
    with configured(ledger) as (_, pipeline, results, _, submit, _, _):
        assert submit.submit(request).idempotent_replay
        current = stored(ledger, request)
        assert current.execution_state is ExecutionState.UNKNOWN_QUARANTINED
        assert current.execution_state.holds_capacity and pipeline.calls == results.calls == 0


class LostResult(InMemorySubmissionLedger):
    def __init__(self) -> None:
        super().__init__()
        self.result_writes = 0

    def record_execution_observation(self, request: RecordExecutionObservation) -> SubmissionIntent:
        result = super().record_execution_observation(request)
        if request.staging_result is not None:
            self.result_writes += 1
            raise SubmissionOutcomeUnknownError("synthetic result commit ACK lost")
        return result


async def test_result_commit_ack_loss_reads_original_without_repeating_inference_or_put() -> None:
    ledger = LostResult()
    request = original_request()
    with configured(ledger) as (_, pipeline, results, worker, submit, _, _):
        submit.submit(request)
        await wait_for(lambda: stored(ledger, request).staging_result is not None)
        await wait_for(lambda: not worker._preparations)
        saved = stored(ledger, request)
        assert ledger.result_writes == pipeline.calls == results.calls == 1
        assert saved.execution_state is ExecutionState.EXECUTING
    with configured(ledger) as (_, pipeline, results, _, submit, _, _):
        assert submit.submit(request).idempotent_replay
        restored = stored(ledger, request)
        assert restored.staging_result == saved.staging_result
        assert restored.engine_request_id == saved.engine_request_id
        assert restored.execution_state is ExecutionState.UNKNOWN_QUARANTINED
        assert pipeline.calls == results.calls == 0


async def test_cancel_stops_local_task_without_manufacturing_stop_or_release() -> None:
    pipeline = Pipeline()
    pipeline.finish.clear()
    with configured(pipeline=pipeline) as (ledger, _, results, worker, submit, abort, _):
        request = original_request()
        submit.submit(request)
        await wait_for(pipeline.started.is_set)
        leases_before = copy.deepcopy(ledger._lease_states)
        aborted = abort.abort(
            AbortExecutionRequest(
                request.identity.submit_intent_id,
                request.identity.operation_id,
                request.identity.attempt_id,
                request.identity.fencing_token,
                "client_cancelled",
                100,
            )
        )
        assert aborted is not None
        assert aborted.intent.execution_state is ExecutionState.UNKNOWN_QUARANTINED
        await wait_for(lambda: not worker._preparations)
        assert not ledger._evidence and ledger._lease_states == leases_before
        assert results.calls == 0 and pipeline.calls == 1
        assert submit.submit(request).idempotent_replay
        assert pipeline.calls == 1


class BlockedResult(InMemorySubmissionLedger):
    def __init__(self) -> None:
        super().__init__()
        self.entered = threading.Event()
        self.release = threading.Event()
        self.finished = threading.Event()

    def record_execution_observation(self, request: RecordExecutionObservation) -> SubmissionIntent:
        if request.staging_result is not None:
            self.entered.set()
            assert self.release.wait(3)
        result = super().record_execution_observation(request)
        if request.staging_result is not None:
            self.finished.set()
        return result


async def test_close_joins_database_registration_before_returning() -> None:
    ledger = BlockedResult()
    with configured(ledger) as (_, _, _, worker, submit, _, _):
        submit.submit(original_request())
        await wait_for(ledger.entered.is_set)
        closing = asyncio.create_task(asyncio.to_thread(worker.close))
        try:
            await asyncio.sleep(0.03)
            assert not closing.done() and not ledger.finished.is_set()
            ledger.release.set()
            await closing
            assert ledger.finished.is_set() and not worker.running
        finally:
            ledger.release.set()
            await closing


def test_query_or_manual_start_cannot_execute_a_record_without_its_preparation() -> None:
    request = original_request()
    ledger = InMemorySubmissionLedger()
    at = datetime.now(UTC)
    intent = SubmissionIntent(
        request.identity, at, at, request.request_digest, request.output_stream_id
    )
    ledger.add(intent, request_document=request.request_document)
    intent.mark_dispatched("synthetic-other-process", at=at)
    intent = ledger.record_execution_observation(
        RecordExecutionObservation.from_intent(intent, expected_version=1)
    )
    with configured(ledger) as (_, pipeline, results, worker, _, _, _):
        with pytest.raises(SubmissionOutcomeUnknownError):
            worker.start(intent)
        assert worker.observe(intent) is None
        assert pipeline.calls == results.calls == 0
