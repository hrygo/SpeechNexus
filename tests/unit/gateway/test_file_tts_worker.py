"""The persisted file lane records complete TTS audio coverage before its receipt."""

import copy
import hashlib
import time
from collections.abc import Callable
from dataclasses import replace

from speechnexus_gateway.application.command_dispatch import OriginalCommandDispatch
from speechnexus_gateway.application.submit_execution import SubmitExecutionService
from speechnexus_gateway.domain.execution_inputs import ExecutionObjectReference
from speechnexus_gateway.domain.execution_results import (
    ExecutionAudioMetadata,
    ExecutionAudioStagingResult,
)
from speechnexus_gateway.domain.file_execution import FileExecutionLimits
from speechnexus_gateway.domain.submission import ExecutionState, SubmitRequest
from speechnexus_gateway.infrastructure.file_execution_worker import FileExecutionWorker
from tests.fake.execution import InMemorySubmissionLedger, fake_ingress
from tests.unit.gateway.test_file_tts_command import tts_request


def audio_receipt(
    request: SubmitRequest, *, source_samples: int = 2400
) -> ExecutionAudioStagingResult:
    pcm = b"\x01\x00" * source_samples
    digest = "sha256:" + hashlib.sha256(pcm).hexdigest()
    return ExecutionAudioStagingResult(
        request.request_digest,
        ExecutionObjectReference(
            "audio-result-synthetic",
            request.identity.workspace_id,
            "synthetic-private",
            "private/execution-results/synthetic/audio.pcm",
            digest,
            len(pcm),
        ),
        "v1",
        "audio/pcm",
        ExecutionAudioMetadata(24000, 1, source_samples, source_samples * 1000 // 24000, digest),
    )


class AudioPipeline:
    def __init__(self, *, source_samples: int = 2400) -> None:
        self.source_samples = source_samples
        self.calls = 0

    def validate_request(self, request: SubmitRequest) -> SubmitRequest:
        return copy.deepcopy(request)

    async def stage_result(
        self,
        request: SubmitRequest,
        *,
        store: object,
        cancelled: Callable[[], bool],
    ) -> ExecutionAudioStagingResult:
        assert not cancelled()
        self.calls += 1
        return audio_receipt(request, source_samples=self.source_samples)


def submit(
    pipeline: AudioPipeline,
) -> tuple[InMemorySubmissionLedger, FileExecutionWorker[object], SubmitExecutionService]:
    ledger = InMemorySubmissionLedger()
    ingress = fake_ingress(node_id="node-tts", gateway_id="gateway-tts", timeout_seconds=0.05)
    worker = FileExecutionWorker(
        ingress=ingress,
        ledger=ledger,
        execution=pipeline,
        results=object(),
        limits=FileExecutionLimits(1024 * 1024, 16384, 1, 10),
        engine_request_prefix="file-tts",
    )
    worker.open()
    service = SubmitExecutionService(
        ingress=ingress,
        ledger=ledger,
        dispatch=OriginalCommandDispatch(commands=ledger, engine=worker, max_document_bytes=16384),
        observation=worker,
        committed_execution=worker,
    )
    return ledger, worker, service


def wait_for(check: Callable[[], bool]) -> None:
    deadline = time.monotonic() + 3
    while not check():
        assert time.monotonic() < deadline, "file TTS worker did not reach the expected state"
        time.sleep(0.005)


async def test_complete_audio_is_metered_before_its_receipt_is_registered() -> None:
    pipeline = AudioPipeline()
    ledger, worker, service = submit(pipeline)
    request = tts_request()
    try:
        outcome = service.submit(request)
        assert outcome.intent.execution_state is ExecutionState.EXECUTING
        wait_for(lambda: (ledger.get("intent-tts") or outcome.intent).staging_result is not None)
        stored = ledger.get("intent-tts")
        assert stored is not None
        assert stored.staging_result is not None
        assert stored.staging_result.media_type == "audio/pcm"
        assert stored.engine_request_id is not None
        interval = ledger.query_output_interval("generated-" + stored.engine_request_id)
        assert interval is not None
        assert (
            interval.interval.sample_start,
            interval.interval.sample_end,
            interval.interval.sample_rate_hz,
        ) == (0, 2400, 24000)
        assert pipeline.calls == 1
    finally:
        worker.close()


async def test_output_ceiling_failure_quarantines_without_registering_a_receipt() -> None:
    pipeline = AudioPipeline(source_samples=4800)
    request = tts_request()
    request = replace(
        request,
        identity=replace(request.identity, max_output_audio_ms=100),
    )
    ledger, worker, service = submit(pipeline)
    try:
        outcome = service.submit(request)
        wait_for(
            lambda: (
                (ledger.get("intent-tts") or outcome.intent).execution_state
                is ExecutionState.UNKNOWN_QUARANTINED
            )
        )
        stored = ledger.get("intent-tts")
        assert stored is not None
        assert stored.staging_result is None
    finally:
        worker.close()
