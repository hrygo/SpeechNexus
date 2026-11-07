"""Bounded, structurally verified VoiceDesign responses without GPU models."""

import asyncio
import io
import json
import wave
from collections.abc import AsyncIterator, Callable
from typing import cast

import httpx
import pytest

from speechnexus_gateway.application.readiness import ReadinessService
from speechnexus_gateway.application.voice_design_readiness import VoiceDesignWarmupProbe
from speechnexus_gateway.domain.file_execution import EngineRequestRejectedError
from speechnexus_gateway.domain.voice_design import (
    VoiceDesignCallError,
    VoiceDesignExecutionTarget,
    VoiceDesignFailure,
    VoiceDesignPolicy,
    VoiceDesignRejectedError,
    VoiceDesignRequest,
)
from speechnexus_gateway.infrastructure.vllm_voice_design import (
    VllmVoiceDesignEndpoint,
    VllmVoiceDesignSynthesizer,
    voice_design_body,
)

TARGET = VoiceDesignExecutionTarget(
    "node-1", "gateway-1", "voice-creation", "instance-voice-design", 1, "binding-voice-design"
)
ENDPOINT = VllmVoiceDesignEndpoint(
    "127.0.0.1",
    8002,
    "synthetic-voice-design",
    "binding-voice-design",
    "a" * 64,
    execution_target=TARGET,
)
POLICY = VoiceDesignPolicy("policy-voice-design")
REQUEST = VoiceDesignRequest("intent-voice-design", "温和的女声", "试听文本。", "zh", 7)
PCM = b"\x01\x00" * 2400


def wav_bytes(pcm: bytes = PCM, *, rate: int = 24000) -> bytes:
    output = io.BytesIO()
    with wave.open(output, "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(rate)
        writer.writeframes(pcm)
    return output.getvalue()


class Chunks(httpx.AsyncByteStream):
    def __init__(self, data: bytes) -> None:
        self.data = data

    async def __aiter__(self) -> AsyncIterator[bytes]:
        yield self.data


class WaitingStream(httpx.AsyncByteStream):
    async def __aiter__(self) -> AsyncIterator[bytes]:
        yield b"RI"
        await asyncio.Event().wait()


def client_for(
    stream: httpx.AsyncByteStream | None,
    requests: list[httpx.Request],
    *,
    status: int = 200,
    content_type: str = "audio/wav",
    handler: object | None = None,
) -> VllmVoiceDesignSynthesizer:
    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if handler is not None:
            resolved = cast(Callable[[httpx.Request], httpx.Response], handler)
            return resolved(request)
        return httpx.Response(
            status,
            headers={"content-type": content_type},
            stream=stream or Chunks(wav_bytes()),
        )

    return VllmVoiceDesignSynthesizer(ENDPOINT, transport=httpx.MockTransport(handle))


def test_the_engine_body_carries_only_the_verified_fields() -> None:
    body = json.loads(voice_design_body(REQUEST, ENDPOINT))
    assert body == {
        "model": "synthetic-voice-design",
        "input": "试听文本。",
        "task_type": "VoiceDesign",
        "instructions": "温和的女声",
        "language": "Chinese",
        "response_format": "wav",
        "seed": 7,
    }
    assert "voice" not in body and "speed" not in body and "sample_rate" not in body


async def test_an_absent_seed_is_never_sent_on_the_wire() -> None:
    requests: list[httpx.Request] = []
    request = VoiceDesignRequest("intent-voice-design", "温和的女声", "试听文本。", "zh", None)

    await client_for(Chunks(wav_bytes()), requests).synthesize(
        request, POLICY, cancelled=lambda: False
    )

    sent = json.loads(requests[0].content)
    assert "seed" not in sent
    assert sent["language"] == "Chinese"


async def test_a_verified_wav_response_is_normalized_from_one_request() -> None:
    requests: list[httpx.Request] = []
    result = await client_for(Chunks(wav_bytes()), requests).synthesize(
        REQUEST, POLICY, cancelled=lambda: False
    )

    assert result.request_id == REQUEST.request_id
    assert result.engine_media_type == "audio/wav"
    assert result.pcm16 == PCM
    assert result.sample_rate_hz == 24000
    assert result.channels == 1
    assert result.duration_ms == 100
    assert result.policy_digest == POLICY.digest
    assert [request.url.path for request in requests] == ["/v1/audio/speech"]
    assert requests[0].headers["authorization"] == "Bearer " + "a" * 64


async def test_a_client_error_is_a_proven_rejection_without_a_retry() -> None:
    requests: list[httpx.Request] = []
    synthesizer = client_for(None, requests, status=422, content_type="application/json")

    with pytest.raises(VoiceDesignRejectedError) as raised:
        await synthesizer.synthesize(REQUEST, POLICY, cancelled=lambda: False)

    error = raised.value
    assert error.code is VoiceDesignFailure.ENGINE_REJECTED
    assert error.request_may_have_run is False
    assert isinstance(error, EngineRequestRejectedError)
    assert len(requests) == 1


@pytest.mark.parametrize("status", [408, 429])
async def test_a_transient_client_status_is_unknown_and_retryable(status: int) -> None:
    with pytest.raises(VoiceDesignCallError) as raised:
        await client_for(None, [], status=status).synthesize(
            REQUEST, POLICY, cancelled=lambda: False
        )

    assert raised.value.code is VoiceDesignFailure.TRANSPORT_UNKNOWN
    assert raised.value.request_may_have_run is True
    assert not isinstance(raised.value, EngineRequestRejectedError)


async def test_a_server_error_stays_unknown_and_is_not_a_rejection() -> None:
    with pytest.raises(VoiceDesignCallError) as raised:
        await client_for(None, [], status=500).synthesize(REQUEST, POLICY, cancelled=lambda: False)
    assert raised.value.code is VoiceDesignFailure.TRANSPORT_UNKNOWN
    assert raised.value.request_may_have_run is True
    assert not isinstance(raised.value, EngineRequestRejectedError)


async def test_a_timeout_is_a_deadline_failure_not_a_rejection() -> None:
    policy = VoiceDesignPolicy("policy-voice-design", timeout_seconds=0.05)
    with pytest.raises(VoiceDesignCallError) as raised:
        await client_for(WaitingStream(), []).synthesize(REQUEST, policy, cancelled=lambda: False)
    assert raised.value.code is VoiceDesignFailure.DEADLINE_EXCEEDED
    assert raised.value.request_may_have_run is True
    assert not isinstance(raised.value, EngineRequestRejectedError)


async def test_a_wrong_media_type_or_body_is_invalid() -> None:
    with pytest.raises(VoiceDesignCallError) as wrong_type:
        await client_for(None, [], content_type="application/json").synthesize(
            REQUEST, POLICY, cancelled=lambda: False
        )
    assert wrong_type.value.code is VoiceDesignFailure.RESPONSE_INVALID

    with pytest.raises(VoiceDesignCallError) as wrong_body:
        await client_for(Chunks(b"not a wave file at all........"), []).synthesize(
            REQUEST, POLICY, cancelled=lambda: False
        )
    assert wrong_body.value.code is VoiceDesignFailure.RESPONSE_INVALID


async def test_audio_beyond_the_policy_ceiling_is_refused() -> None:
    policy = VoiceDesignPolicy("policy-voice-design", max_audio_ms=1000, max_pcm_bytes=1024)
    with pytest.raises(VoiceDesignCallError) as raised:
        await client_for(Chunks(wav_bytes()), []).synthesize(
            REQUEST, policy, cancelled=lambda: False
        )
    assert raised.value.code is VoiceDesignFailure.OUTPUT_LIMIT_EXCEEDED


async def test_target_mismatch_is_refused_before_any_request() -> None:
    requests: list[httpx.Request] = []
    other = VoiceDesignExecutionTarget(
        "node-2", "gateway-1", "voice-creation", "instance-voice-design", 1, "binding-voice-design"
    )
    with pytest.raises(ValueError):
        client_for(Chunks(wav_bytes()), requests).validate_execution_target(other)
    assert requests == []


async def test_warmup_is_a_real_synthesis_and_never_a_health_probe() -> None:
    requests: list[httpx.Request] = []
    await client_for(Chunks(wav_bytes()), requests).warmup()

    assert len(requests) == 1
    assert requests[0].url.path == "/v1/audio/speech"
    body = json.loads(requests[0].content)
    assert body["task_type"] == "VoiceDesign"
    assert body["input"] and body["instructions"]
    assert all("/health" not in request.url.path for request in requests)
    assert all("/v1/omni/sleep" not in request.url.path for request in requests)


async def test_a_health_200_without_a_working_synthesis_is_not_ready() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/health":
            return httpx.Response(200, json={"status": "ok"})
        return httpx.Response(503, headers={"content-type": "application/json"})

    synthesizer = VllmVoiceDesignSynthesizer(ENDPOINT, transport=httpx.MockTransport(handler))
    report = await ReadinessService(
        [VoiceDesignWarmupProbe(synthesizer)], timeout_seconds=1
    ).inspect()

    assert report.ready is False
    assert [check.status.value for check in report.checks] == ["failed"]
    assert [request.url.path for request in requests] == ["/v1/audio/speech"]
