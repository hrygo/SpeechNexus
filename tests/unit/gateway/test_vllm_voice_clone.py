"""Bounded, structurally verified Base clone requests and responses without GPU models."""

import asyncio
import base64
import io
import json
import logging
import wave
from collections.abc import AsyncIterator, Callable
from dataclasses import replace
from typing import cast

import httpx
import pytest

from speechnexus_gateway.domain.file_execution import EngineRequestRejectedError
from speechnexus_gateway.domain.voice_clone import (
    ReferenceAudio,
    VoiceCloneCallError,
    VoiceCloneExecutionTarget,
    VoiceCloneFailure,
    VoiceClonePolicy,
    VoiceCloneRejectedError,
    VoiceCloneRequest,
)
from speechnexus_gateway.infrastructure.vllm_voice_clone import (
    VllmVoiceCloneEndpoint,
    VllmVoiceCloneSynthesizer,
    voice_clone_body,
)
from tests.fake.file_voice_clone_configuration import reference_wav

TARGET = VoiceCloneExecutionTarget(
    "node-1", "gateway-1", "voice-creation", "instance-voice-clone", 1, "binding-voice-clone"
)
ENDPOINT = VllmVoiceCloneEndpoint(
    "127.0.0.1",
    8004,
    "synthetic-voice-clone",
    "binding-voice-clone",
    "a" * 64,
    execution_target=TARGET,
)
POLICY = VoiceClonePolicy("policy-voice-clone")
AUDIO = ReferenceAudio.from_wav(reference_wav())
REFERENCE_TEXT = "Synthetic warmup reference."
REQUEST = VoiceCloneRequest("intent-voice-clone", "试听文本。", "zh", AUDIO, REFERENCE_TEXT)
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
    warmup: bool = False,
) -> VllmVoiceCloneSynthesizer:
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

    return VllmVoiceCloneSynthesizer(
        ENDPOINT,
        transport=httpx.MockTransport(handle),
        warmup_request=REQUEST if warmup else None,
        warmup_policy=POLICY if warmup else None,
    )


def test_the_engine_body_carries_the_verified_base_shape() -> None:
    body = json.loads(voice_clone_body(REQUEST, ENDPOINT))

    assert body == {
        "model": "synthetic-voice-clone",
        "input": "试听文本。",
        "task_type": "Base",
        "language": "Chinese",
        "ref_audio": "data:audio/wav;base64,"
        + base64.b64encode(REQUEST.reference_audio.wav()).decode("ascii"),
        "ref_text": REFERENCE_TEXT,
        "response_format": "wav",
    }
    assert "voice" not in body and "speed" not in body and "sample_rate" not in body
    assert "x_vector_only_mode" not in body
    assert body["ref_audio"].startswith("data:audio/wav;base64,")


def test_the_auto_language_is_forwarded_verbatim() -> None:
    body = json.loads(voice_clone_body(replace(REQUEST, language="auto"), ENDPOINT))

    assert body["language"] == "auto"


def test_an_unpaired_reference_is_sent_as_an_explicit_x_vector_request() -> None:
    unpaired = VoiceCloneRequest("intent-voice-clone", "Ready.", "en", AUDIO, None)
    body = json.loads(voice_clone_body(unpaired, ENDPOINT))

    assert body["task_type"] == "Base"
    assert body["x_vector_only_mode"] is True
    assert "ref_text" not in body
    assert body["ref_audio"].startswith("data:audio/wav;base64,")


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
    sent = json.loads(requests[0].content)
    assert sent["task_type"] == "Base"


@pytest.mark.parametrize("status", [400, 404, 422])
async def test_a_client_error_is_a_proven_rejection_without_a_retry(status: int) -> None:
    requests: list[httpx.Request] = []
    synthesizer = client_for(None, requests, status=status, content_type="application/json")

    with pytest.raises(VoiceCloneRejectedError) as raised:
        await synthesizer.synthesize(REQUEST, POLICY, cancelled=lambda: False)

    error = raised.value
    assert error.code is VoiceCloneFailure.ENGINE_REJECTED
    assert error.request_may_have_run is False
    assert isinstance(error, EngineRequestRejectedError)
    assert len(requests) == 1


@pytest.mark.parametrize("status", [408, 429])
async def test_a_transient_client_status_is_unknown_and_retryable(status: int) -> None:
    with pytest.raises(VoiceCloneCallError) as raised:
        await client_for(None, [], status=status).synthesize(
            REQUEST, POLICY, cancelled=lambda: False
        )

    assert raised.value.code is VoiceCloneFailure.TRANSPORT_UNKNOWN
    assert raised.value.request_may_have_run is True
    assert not isinstance(raised.value, EngineRequestRejectedError)


async def test_a_server_error_stays_unknown_and_is_not_a_rejection() -> None:
    with pytest.raises(VoiceCloneCallError) as raised:
        await client_for(None, [], status=500).synthesize(REQUEST, POLICY, cancelled=lambda: False)
    assert raised.value.code is VoiceCloneFailure.TRANSPORT_UNKNOWN
    assert raised.value.request_may_have_run is True
    assert not isinstance(raised.value, EngineRequestRejectedError)


async def test_a_timeout_is_a_deadline_failure_not_a_rejection() -> None:
    policy = VoiceClonePolicy("policy-voice-clone", timeout_seconds=0.05)
    with pytest.raises(VoiceCloneCallError) as raised:
        await client_for(WaitingStream(), []).synthesize(REQUEST, policy, cancelled=lambda: False)
    assert raised.value.code is VoiceCloneFailure.DEADLINE_EXCEEDED
    assert raised.value.request_may_have_run is True
    assert not isinstance(raised.value, EngineRequestRejectedError)


async def test_a_wrong_media_type_or_body_is_invalid() -> None:
    with pytest.raises(VoiceCloneCallError) as wrong_type:
        await client_for(None, [], content_type="application/json").synthesize(
            REQUEST, POLICY, cancelled=lambda: False
        )
    assert wrong_type.value.code is VoiceCloneFailure.RESPONSE_INVALID

    with pytest.raises(VoiceCloneCallError) as wrong_body:
        await client_for(Chunks(b"not a wave file at all........"), []).synthesize(
            REQUEST, POLICY, cancelled=lambda: False
        )
    assert wrong_body.value.code is VoiceCloneFailure.RESPONSE_INVALID


async def test_audio_beyond_the_policy_ceiling_is_refused() -> None:
    policy = VoiceClonePolicy("policy-voice-clone", max_audio_ms=1000, max_pcm_bytes=1024)
    with pytest.raises(VoiceCloneCallError) as raised:
        await client_for(Chunks(wav_bytes()), []).synthesize(
            REQUEST, policy, cancelled=lambda: False
        )
    assert raised.value.code is VoiceCloneFailure.OUTPUT_LIMIT_EXCEEDED


async def test_target_mismatch_is_refused_before_any_request() -> None:
    requests: list[httpx.Request] = []
    other = VoiceCloneExecutionTarget(
        "node-2", "gateway-1", "voice-creation", "instance-voice-clone", 1, "binding-voice-clone"
    )
    with pytest.raises(ValueError):
        client_for(Chunks(wav_bytes()), requests).validate_execution_target(other)
    assert requests == []


async def test_warmup_is_a_real_clone_synthesis_and_never_a_health_probe() -> None:
    requests: list[httpx.Request] = []
    await client_for(Chunks(wav_bytes()), requests, warmup=True).warmup()

    assert len(requests) == 1
    assert requests[0].url.path == "/v1/audio/speech"
    body = json.loads(requests[0].content)
    assert body["task_type"] == "Base"
    assert body["input"] and body["ref_text"]
    assert body["ref_audio"].startswith("data:audio/wav;base64,")
    assert all("/health" not in request.url.path for request in requests)
    assert all("/v1/omni/sleep" not in request.url.path for request in requests)


async def test_warmup_without_an_approved_reference_fails_closed() -> None:
    with pytest.raises(ValueError):
        await client_for(Chunks(wav_bytes()), []).warmup()


async def test_no_log_line_contains_the_reference_payload_or_transcript(
    caplog: pytest.LogCaptureFixture,
) -> None:
    encoded = base64.b64encode(AUDIO.wav()).decode("ascii")
    with caplog.at_level(logging.DEBUG):
        await client_for(Chunks(wav_bytes()), []).synthesize(
            REQUEST, POLICY, cancelled=lambda: False
        )
        with pytest.raises(VoiceCloneRejectedError):
            await client_for(None, [], status=422, content_type="application/json").synthesize(
                REQUEST, POLICY, cancelled=lambda: False
            )

    assert encoded not in caplog.text
    assert "data:audio/wav;base64" not in caplog.text
    assert REFERENCE_TEXT not in caplog.text
