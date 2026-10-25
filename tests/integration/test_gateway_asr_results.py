"""Versioned MinIO roundtrip; codec is real, recognition/VAD remain synthetic."""

import asyncio
import json
import os
from dataclasses import replace
from typing import Any

import pytest
from minio import Minio
from tests.fake.execution_command import original_request
from tests.fake.vad import ScriptedVadModel
from tests.integration.test_gateway_frozen_inputs import object_store as object_store
from tests.integration.test_gateway_frozen_inputs import upload_command
from tests.unit.gateway.test_file_asr_command import CapturingRecognizer

from speechnexus_gateway.application.execution_inputs import FileExecutionInputs
from speechnexus_gateway.domain.asr_results import AsrResultError
from speechnexus_gateway.domain.execution_inputs import ExecutionInputLimits
from speechnexus_gateway.domain.media import AudioDecodeLimits
from speechnexus_gateway.domain.vad import VadPolicy
from speechnexus_gateway.infrastructure.asr_results_minio import MinioAsrResults
from speechnexus_gateway.infrastructure.execution_objects_minio import MinioExecutionInputs
from speechnexus_gateway.infrastructure.pyav_audio import PyAvAudioDecoder
from speechnexus_gateway.interfaces.file_asr_command import CommandFileAsr

pytestmark = pytest.mark.integration


def results(bucket: str) -> MinioAsrResults:
    return MinioAsrResults(
        endpoint=os.environ["SPEECHNEXUS_TEST_MINIO_ENDPOINT"],
        bucket=bucket,
        access_key=os.environ["SPEECHNEXUS_TEST_MINIO_ACCESS_KEY"],
        secret_key=os.environ["SPEECHNEXUS_TEST_MINIO_SECRET_KEY"],
        timeout_seconds=10,
    )


@pytest.mark.parametrize("speech", [True, False])
async def test_command_to_private_result_and_pinned_readback(
    object_store: tuple[MinioExecutionInputs, Minio, str], speech: bool
) -> None:
    inputs, admin, bucket = object_store
    command, _ = await asyncio.to_thread(upload_command, admin, bucket)
    command["parameters"]["max_segment_ms"] = 1000
    request = original_request(command)
    recognizer = CapturingRecognizer()
    consumer = CommandFileAsr(
        inputs=FileExecutionInputs(inputs),
        decoder=PyAvAudioDecoder(),
        vad_model=ScriptedVadModel(fallback=0.9 if speech else 0.0),
        recognizer=recognizer,
        vad_tuning=VadPolicy("policy-input", max_segment_ms=1000),
        max_completion_tokens=64,
        max_call_seconds=2,
        input_limits=ExecutionInputLimits(1000000, 1024, 10),
        decode_limits=AudioDecodeLimits(),
    )
    store = results(bucket)
    try:
        receipt = await consumer.stage_result(
            request, store=store, max_result_bytes=1024, cancelled=lambda: False
        )

        def read() -> bytes:
            with inputs.open_verified(
                receipt.reference,
                limits=ExecutionInputLimits(1024, 64, 10),
                cancelled=lambda: False,
            ) as verified:
                assert verified.storage_version == receipt.storage_version
                return verified.stream.read()

        assert json.loads(await asyncio.to_thread(read)) == {
            "text": "Synthetic.\nSynthetic." if speech else ""
        }
        assert len(recognizer.requests) == (2 if speech else 0)
        assert receipt.reference.object_key.startswith("private/execution-results/")
        assert receipt.request_digest == request.request_digest
    finally:
        store.close()


async def test_wrong_readback_digest_never_returns_a_receipt(
    object_store: tuple[MinioExecutionInputs, Minio, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from speechnexus_gateway.domain.asr_results import PreparedAsrResult

    _, _, bucket = object_store
    store = results(bucket)
    original = store._put

    async def corrupt(*args: Any, **kwargs: Any) -> str:
        # A successful versioned write of equal-sized wrong bytes still cannot
        # defeat the expected result digest during actual MinIO GET readback.
        wrong = replace(args[1], body=b"x" * len(args[1].body))
        return await original(args[0], wrong, args[2], args[3])

    monkeypatch.setattr(store, "_put", corrupt)
    result = PreparedAsrResult("ws-demo", "sha256:" + "a" * 64, b'{"text":"synthetic"}')
    try:
        with pytest.raises(AsrResultError, match="asr_result_readback_failed"):
            await store.stage(result, cancelled=lambda: False)
    finally:
        store.close()
