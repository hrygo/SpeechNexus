"""Bounded, structurally verified TTS responses without GPU models."""

import asyncio
import io
import json
import wave
from collections.abc import AsyncIterator
from dataclasses import replace

import httpx
import pytest

from speechnexus_gateway.domain.tts import (
    TtsCallError,
    TtsEngineFormat,
    TtsFailure,
    TtsPolicy,
    TtsRequest,
)
from speechnexus_gateway.infrastructure.vllm_tts import (
    VllmTtsEndpoint,
    VllmTtsSynthesizer,
)

ENDPOINT = VllmTtsEndpoint("127.0.0.1", 8000, "synthetic-tts", "binding-1")
POLICY = TtsPolicy("config-1", "Vivian")
REQUEST = TtsRequest("request-1", "合成 synthetic.")
PCM = b"\x01\x00" * 2400
MEDIA = {TtsEngineFormat.WAV: "audio/wav", TtsEngineFormat.PCM: "audio/pcm"}


def wav_bytes(
    pcm: bytes,
    *,
    rate: int = 24000,
    channels: int = 1,
    width: int = 2,
    audio_format: int = 1,
) -> bytes:
    output = io.BytesIO()
    with wave.open(output, "wb") as writer:
        writer.setnchannels(channels)
        writer.setsampwidth(width)
        writer.setframerate(rate)
        writer.writeframes(pcm)
    payload = bytearray(output.getvalue())
    if audio_format != 1:
        payload[20:22] = audio_format.to_bytes(2, "little")
    return bytes(payload)


def riff(*chunks: tuple[bytes, bytes]) -> bytes:
    body = b"".join(
        chunk_id + len(data).to_bytes(4, "little") + data + (b"\x00" if len(data) % 2 else b"")
        for chunk_id, data in chunks
    )
    return b"RIFF" + (len(body) + 4).to_bytes(4, "little") + b"WAVE" + body


def fmt_chunk(
    *, rate: int = 24000, channels: int = 1, width: int = 2, audio_format: int = 1
) -> tuple[bytes, bytes]:
    return (
        b"fmt ",
        (
            audio_format.to_bytes(2, "little")
            + channels.to_bytes(2, "little")
            + rate.to_bytes(4, "little")
            + (rate * channels * width).to_bytes(4, "little")
            + (channels * width).to_bytes(2, "little")
            + (width * 8).to_bytes(2, "little")
        ),
    )


class Chunks(httpx.AsyncByteStream):
    def __init__(self, data: bytes, *, size: int = 4096) -> None:
        self.data = data
        self.size = size
        self.closed = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for offset in range(0, len(self.data), self.size):
            yield self.data[offset : offset + self.size]

    async def aclose(self) -> None:
        self.closed = True


class WaitingStream(httpx.AsyncByteStream):
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.closed = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        self.started.set()
        yield b"RI"
        await asyncio.Event().wait()

    async def aclose(self) -> None:
        self.closed = True


def client_for(
    stream: httpx.AsyncByteStream,
    requests: list[httpx.Request],
    *,
    status: int = 200,
    headers: dict[str, str] | None = None,
    media_type: str = "audio/wav",
) -> VllmTtsSynthesizer:
    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            status,
            headers={
                "content-type": media_type,
                "x-audio-sample-rate": "24000",
                "x-audio-channels": "1",
                **(headers or {}),
            },
            stream=stream,
        )

    return VllmTtsSynthesizer(ENDPOINT, transport=httpx.MockTransport(handle))


async def test_wav_response_is_normalized_and_request_is_frozen() -> None:
    requests: list[httpx.Request] = []
    stream = Chunks(wav_bytes(PCM), size=7)
    result = await client_for(stream, requests).synthesize(REQUEST, POLICY, cancelled=lambda: False)
    assert result.request_id == "request-1"
    assert result.engine_media_type == "audio/wav"
    assert result.pcm16 == PCM and result.sample_rate_hz == 24000
    assert result.channels == 1 and result.duration_ms == 100
    assert result.policy_digest == POLICY.digest and stream.closed
    assert "synthetic" not in repr(result)
    assert len(requests) == 1
    request = requests[0]
    assert request.url.path == "/v1/audio/speech"
    assert request.headers["accept"] == "audio/wav"
    assert request.headers["accept-encoding"] == "identity"
    assert request.headers["x-request-id"] == "request-1"
    document = json.loads(request.content)
    assert document == {
        "model": "synthetic-tts",
        "input": "合成 synthetic.",
        "voice": "Vivian",
        "task_type": "CustomVoice",
        "response_format": "wav",
        "sample_rate": 24000,
        "speed": 1.0,
    }


async def test_pcm_response_with_instructions_matches_the_declared_shape() -> None:
    requests: list[httpx.Request] = []
    policy = replace(POLICY, engine_format=TtsEngineFormat.PCM, instructions="慢一点")
    result = await client_for(
        Chunks(b"\x02\x00" * 800), requests, media_type="audio/pcm"
    ).synthesize(REQUEST, policy, cancelled=lambda: False)
    assert result.engine_media_type == "audio/pcm"
    assert result.pcm16 == b"\x02\x00" * 800 and result.duration_ms == 33
    document = json.loads(requests[0].content)
    assert document["instructions"] == "慢一点"
    assert document["response_format"] == "pcm"
    without_instructions = json.loads(
        await _body_for(replace(POLICY, engine_format=TtsEngineFormat.PCM))
    )
    assert "instructions" not in without_instructions


async def _body_for(policy: TtsPolicy) -> bytes:
    requests: list[httpx.Request] = []
    await client_for(Chunks(b"\x00\x00" * 2400), requests, media_type="audio/pcm").synthesize(
        REQUEST, policy, cancelled=lambda: False
    )
    return requests[0].content


async def test_language_and_seed_are_forwarded_only_when_declared() -> None:
    declared = json.loads(
        await _body_for(replace(POLICY, engine_format=TtsEngineFormat.PCM, language="en", seed=7))
    )
    assert declared["language"] == "en"
    assert declared["seed"] == 7
    absent = json.loads(await _body_for(replace(POLICY, engine_format=TtsEngineFormat.PCM)))
    assert "language" not in absent
    assert "seed" not in absent


@pytest.mark.parametrize(
    "change",
    [
        "status",
        "redirect",
        "content_type",
        "encoding",
        "sample_rate_header",
        "channels_header",
        "missing_rate_header",
        "missing_channels_header",
        "truncated",
        "wrong_rate",
        "stereo",
        "width8",
        "non_pcm_format",
        "missing_data",
        "odd_data",
        "declared_size",
        "chunk_overrun",
    ],
)
async def test_corrupt_or_unverifiable_wav_responses_never_become_success(change: str) -> None:
    status = 200
    headers: dict[str, str] = {}
    payload = wav_bytes(PCM)
    media_type = "audio/wav"
    if change == "status":
        status = 500
    elif change == "redirect":
        status = 307
        headers["location"] = "http://untrusted.invalid/private"
    elif change == "content_type":
        media_type = "application/json"
    elif change == "encoding":
        headers["content-encoding"] = "br"
    elif change == "sample_rate_header":
        headers["x-audio-sample-rate"] = "8000"
    elif change == "channels_header":
        headers["x-audio-channels"] = "2"
    elif change == "missing_rate_header":
        headers["x-audio-sample-rate"] = ""
    elif change == "missing_channels_header":
        headers["x-audio-channels"] = ""
    elif change == "truncated":
        headers["x-audio-truncated"] = "true"
    elif change == "wrong_rate":
        payload = wav_bytes(PCM, rate=8000)
    elif change == "stereo":
        payload = wav_bytes(PCM, channels=2)
    elif change == "width8":
        payload = wav_bytes(PCM, width=1)
    elif change == "non_pcm_format":
        payload = wav_bytes(PCM, audio_format=3)
    elif change == "missing_data":
        payload = riff(fmt_chunk())
    elif change == "odd_data":
        payload = riff(fmt_chunk(), (b"data", b"\x00\x00\x00"))
    elif change == "declared_size":
        patched = bytearray(payload)
        patched[4:8] = (len(payload) + 4096).to_bytes(4, "little")
        payload = bytes(patched)
    else:
        patched = bytearray(payload)
        patched[40:44] = (len(payload) * 4).to_bytes(4, "little")
        payload = bytes(patched)
    requests: list[httpx.Request] = []
    stream = Chunks(payload)
    with pytest.raises(TtsCallError) as error:
        await client_for(
            stream, requests, status=status, headers=headers, media_type=media_type
        ).synthesize(REQUEST, POLICY, cancelled=lambda: False)
    expected = TtsFailure.OUTPUT_TRUNCATED if change == "truncated" else TtsFailure.RESPONSE_INVALID
    assert error.value.code == expected
    assert error.value.request_may_have_run and len(requests) == 1
    assert "untrusted" not in str(error.value) and "synthetic" not in str(error.value)


@pytest.mark.parametrize("change", ["odd_pcm", "empty_pcm", "over_budget", "too_long"])
async def test_pcm_shape_and_budget_violations_are_rejected(change: str) -> None:
    policy = replace(POLICY, engine_format=TtsEngineFormat.PCM)
    payload = b"\x00\x00" * 100
    if change == "odd_pcm":
        payload = b"\x00\x00\x00"
        expected = TtsFailure.RESPONSE_INVALID
    elif change == "empty_pcm":
        payload = b""
        expected = TtsFailure.RESPONSE_INVALID
    elif change == "over_budget":
        policy = replace(policy, max_pcm_bytes=1024)
        payload = b"\x00\x00" * 1024
        expected = TtsFailure.OUTPUT_LIMIT_EXCEEDED
    else:
        policy = replace(policy, max_audio_ms=100)
        payload = b"\x00\x00" * 24000
        expected = TtsFailure.OUTPUT_LIMIT_EXCEEDED
    with pytest.raises(TtsCallError) as error:
        await client_for(Chunks(payload), [], media_type="audio/pcm").synthesize(
            REQUEST, policy, cancelled=lambda: False
        )
    assert error.value.code == expected and error.value.request_may_have_run


async def test_total_response_bytes_are_capped_before_structural_parsing() -> None:
    policy = replace(POLICY, max_pcm_bytes=1024)
    payload = riff(fmt_chunk(), (b"data", b"\x00\x00" * 60000))
    with pytest.raises(TtsCallError) as error:
        await client_for(Chunks(payload), []).synthesize(REQUEST, policy, cancelled=lambda: False)
    assert error.value.code == TtsFailure.OUTPUT_LIMIT_EXCEEDED
    assert error.value.request_may_have_run and error.value.audio_bytes is not None


async def test_cancel_before_send_makes_no_request() -> None:
    requests: list[httpx.Request] = []
    with pytest.raises(TtsCallError) as error:
        await client_for(Chunks(b""), requests).synthesize(REQUEST, POLICY, cancelled=lambda: True)
    assert error.value.code == TtsFailure.CANCELLED
    assert not error.value.request_may_have_run and not requests


async def test_inflight_cancel_closes_transport_but_preserves_possible_engine_execution() -> None:
    stream = WaitingStream()
    cancel = asyncio.Event()
    task = asyncio.create_task(
        client_for(stream, []).synthesize(REQUEST, POLICY, cancelled=cancel.is_set)
    )
    await asyncio.wait_for(stream.started.wait(), timeout=2)
    cancel.set()
    with pytest.raises(TtsCallError) as error:
        await asyncio.wait_for(task, timeout=2)
    assert error.value.code == TtsFailure.CANCELLED
    assert error.value.request_may_have_run and stream.closed


async def test_absolute_deadline_interrupts_a_body_that_never_finishes() -> None:
    stream = WaitingStream()
    with pytest.raises(TtsCallError) as error:
        await asyncio.wait_for(
            client_for(stream, []).synthesize(
                REQUEST, replace(POLICY, timeout_seconds=0.05), cancelled=lambda: False
            ),
            timeout=2,
        )
    assert error.value.code == TtsFailure.DEADLINE_EXCEEDED
    assert error.value.request_may_have_run and stream.closed


async def test_transport_failure_after_send_is_unknown_not_success_or_stop() -> None:
    def handle(_: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("synthetic transport failure")

    with pytest.raises(TtsCallError) as error:
        await VllmTtsSynthesizer(ENDPOINT, transport=httpx.MockTransport(handle)).synthesize(
            REQUEST, POLICY, cancelled=lambda: False
        )
    assert error.value.code == TtsFailure.TRANSPORT_UNKNOWN
    assert error.value.request_may_have_run


@pytest.mark.parametrize(
    "address,port,model,binding",
    [
        ("8.8.8.8", 8000, "synthetic-tts", "binding-1"),
        ("127.0.0.1", 0, "synthetic-tts", "binding-1"),
        ("127.0.0.1", 8000, "bad model", "binding-1"),
        ("127.0.0.1", 8000, "synthetic-tts", ""),
    ],
)
def test_endpoint_identity_cannot_be_caller_supplied(
    address: str, port: int, model: str, binding: str
) -> None:
    with pytest.raises(ValueError):
        VllmTtsEndpoint(address, port, model, binding)


def test_bearer_token_is_accepted_but_never_exposed() -> None:
    endpoint = VllmTtsEndpoint("10.1.2.3", 8443, "synthetic-tts", "binding-1", "secret-token")
    assert endpoint.base_url == "http://10.1.2.3:8443"
    assert "secret-token" not in repr(endpoint)
    with pytest.raises(ValueError):
        VllmTtsEndpoint("10.1.2.3", 8443, "synthetic-tts", "binding-1", "bad token")
