"""Bounded vendor streaming, trace identity and ambiguous failures without GPU models."""

import asyncio
import json
from collections.abc import AsyncIterator
from dataclasses import replace
from typing import Any

import httpx
import pytest

from speechnexus_gateway.domain.asr import (
    AsrAudio,
    AsrCallError,
    AsrFailure,
    AsrPolicy,
    AsrSegment,
    AsrWord,
)
from speechnexus_gateway.infrastructure.vllm_asr import VllmAsrEndpoint, VllmAsrRecognizer

ENDPOINT = VllmAsrEndpoint("127.0.0.1", 8000, "synthetic-asr", "binding-1")
POLICY = AsrPolicy("config-1")
AUDIO = AsrAudio("request-1", b"\x01\x00" * 1600)


def event(choices: list[Any], **extra: Any) -> bytes:
    return (
        "data: "
        + json.dumps(
            {
                "id": "transcribe-request-1",
                "object": "transcription.chunk",
                "created": 1,
                "model": "synthetic-asr",
                "choices": choices,
                **extra,
            },
            ensure_ascii=False,
        )
        + "\n\n"
    ).encode()


def response_bytes(
    *,
    text: str = "合成 synthetic.",
    finish: str = "stop",
    tokens: int = 7,
    **extra: Any,
) -> bytes:
    return b"".join(
        [
            event([{"delta": {"content": text}}]),
            event(
                [{"delta": {"content": ""}, "finish_reason": finish, "stop_reason": None}],
                **extra,
            ),
            event(
                [],
                usage={
                    "prompt_tokens": 20,
                    "completion_tokens": tokens,
                    "total_tokens": 20 + tokens,
                },
            ),
            b"data: [DONE]\n\n",
        ]
    )


class Chunks(httpx.AsyncByteStream):
    def __init__(self, data: bytes, *, size: int = 1) -> None:
        self.data = data
        self.size = size
        self.closed = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for offset in range(0, len(self.data), self.size):
            yield self.data[offset : offset + self.size]

    async def aclose(self) -> None:
        self.closed = True


def client_for(
    stream: httpx.AsyncByteStream,
    requests: list[httpx.Request],
    *,
    status: int = 200,
    headers: dict[str, str] | None = None,
) -> VllmAsrRecognizer:
    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            status,
            headers={
                "content-type": "text/event-stream",
                "x-request-id": "request-1",
                **(headers or {}),
            },
            stream=stream,
        )

    return VllmAsrRecognizer(ENDPOINT, transport=httpx.MockTransport(handle))


@pytest.mark.parametrize("size", [1, 7, 4096])
async def test_real_httpx_stream_handles_fragmented_utf8_and_verifies_terminal_usage(
    size: int,
) -> None:
    requests: list[httpx.Request] = []
    stream = Chunks(response_bytes().replace(b"\n", b"\r\n"), size=size)
    result = await client_for(stream, requests).transcribe(AUDIO, POLICY, cancelled=lambda: False)
    assert result.text == "合成 synthetic." and result.completion_tokens == 7
    assert result.request_id == AUDIO.request_id
    assert result.engine_request_id == "transcribe-request-1"
    assert result.policy_digest == POLICY.digest and stream.closed
    assert "synthetic." not in repr(result)
    assert len(requests) == 1
    request = requests[0]
    assert request.url.path == "/v1/audio/transcriptions"
    assert request.headers["x-request-id"] == "request-1"
    assert request.headers["accept-encoding"] == "identity"
    body = request.content
    assert b'name="stream"\r\n\r\ntrue' in body
    assert b'name="stream_include_usage"\r\n\r\ntrue' in body
    assert b'name="max_completion_tokens"\r\n\r\n1024' in body
    assert b'name="language"' not in body
    assert b"RIFF" in body and b'filename="audio.wav"' in body


async def test_native_language_duration_and_timestamp_evidence_are_carried() -> None:
    stream = Chunks(
        response_bytes(
            language="en",
            duration=0.1,
            segments=[
                {
                    "id": 0,
                    "start": 0.0,
                    "end": 0.1,
                    "text": "synthetic",
                    "tokens": [1, 2],
                    "avg_logprob": -0.1,
                    "compression_ratio": 1.0,
                    "no_speech_prob": 0.01,
                    "seek": 0,
                    "temperature": 0.0,
                }
            ],
            words=[{"word": "synthetic", "start": 0.0, "end": 0.1}],
        )
    )
    result = await client_for(stream, []).transcribe(AUDIO, POLICY, cancelled=lambda: False)
    assert result.language == "en"
    assert result.duration_ms == 100
    assert result.segments == (
        AsrSegment(
            0,
            100,
            "synthetic",
            id=0,
            avg_logprob=-0.1,
            compression_ratio=1.0,
            no_speech_prob=0.01,
            seek=0,
            temperature=0.0,
            tokens=(1, 2),
        ),
    )
    assert result.words == (AsrWord("synthetic", 0, 100),)


async def test_native_engine_without_optional_evidence_leaves_them_unset() -> None:
    result = await client_for(Chunks(response_bytes()), []).transcribe(
        AUDIO, POLICY, cancelled=lambda: False
    )
    assert result.language is None
    assert result.duration_ms is None
    assert result.segments == () and result.words == ()


@pytest.mark.parametrize(
    "extra",
    [
        {
            "segments": [
                {"start": 0.08, "end": 0.09, "text": "b"},
                {"start": 0.01, "end": 0.02, "text": "a"},
            ]
        },
        {"segments": [{"start": 0.0, "end": 0.2, "text": "beyond"}]},
        {"segments": [{"start": 0.05, "end": 0.05, "text": "empty"}]},
        {"segments": [{"start": 0.0, "end": 0.1}]},
        {"segments": [{"start": 0.0, "end": float("nan"), "text": "nonfinite"}]},
        {
            "words": [
                {"word": "later", "start": 0.08, "end": 0.09},
                {"word": "earlier", "start": 0.0, "end": 0.01},
            ]
        },
        {"words": [{"word": "beyond", "start": 0.0, "end": 0.2}]},
        {"duration": 2.0},
        {"language": 7},
    ],
)
async def test_unusable_native_evidence_is_rejected(extra: dict[str, Any]) -> None:
    stream = Chunks(response_bytes(**extra))
    with pytest.raises(AsrCallError) as error:
        await client_for(stream, []).transcribe(AUDIO, POLICY, cancelled=lambda: False)
    assert error.value.code == AsrFailure.RESPONSE_INVALID
    assert error.value.request_may_have_run and stream.closed


@pytest.mark.parametrize(
    "change",
    [
        "status",
        "redirect",
        "content_type",
        "encoding",
        "header_id",
        "event_id",
        "model",
        "missing_finish",
        "missing_usage",
        "missing_done",
        "duplicate_usage",
        "after_finish",
        "invalid_utf8",
        "duplicate_json",
        "nonfinite_json",
        "invalid_tokens",
    ],
)
async def test_corrupt_or_unverifiable_responses_never_become_success(change: str) -> None:
    body = response_bytes()
    headers: dict[str, str] = {}
    status = 200
    if change == "status":
        status = 500
    elif change == "redirect":
        status = 307
        headers["location"] = "http://untrusted.invalid/private"
    elif change == "content_type":
        headers["content-type"] = "application/json"
    elif change == "encoding":
        headers["content-encoding"] = "br"
    elif change == "header_id":
        headers["x-request-id"] = "another-request"
    elif change == "event_id":
        body = body.replace(b"transcribe-request-1", b"transcribe-another")
    elif change == "model":
        body = body.replace(b"synthetic-asr", b"another-model")
    elif change == "missing_finish":
        body = event([{"delta": {"content": "synthetic"}}]) + b"data: [DONE]\n\n"
    elif change == "missing_usage":
        body = event([{"delta": {}, "finish_reason": "stop"}]) + b"data: [DONE]\n\n"
    elif change == "missing_done":
        body = body.removesuffix(b"data: [DONE]\n\n")
    elif change == "duplicate_usage":
        body = body.replace(
            b"data: [DONE]",
            event([], usage={"prompt_tokens": 20, "completion_tokens": 7, "total_tokens": 27})
            + b"data: [DONE]",
        )
    elif change == "after_finish":
        body = body.replace(
            b"data: [DONE]", event([{"delta": {"content": "synthetic"}}]) + b"data: [DONE]"
        )
    elif change == "invalid_utf8":
        body = b'data: {"bad": "\xff"}\n\n'
    elif change == "duplicate_json":
        body = body.replace(b'"created": 1', b'"created": 1, "created": 2')
    elif change == "nonfinite_json":
        body = body.replace(b'"created": 1', b'"created": NaN')
    else:
        body = body.replace(b'"completion_tokens": 7', b'"completion_tokens": true')
    requests: list[httpx.Request] = []
    stream = Chunks(body, size=4096)
    with pytest.raises(AsrCallError) as error:
        await client_for(stream, requests, status=status, headers=headers).transcribe(
            AUDIO, POLICY, cancelled=lambda: False
        )
    assert error.value.code == AsrFailure.RESPONSE_INVALID
    assert error.value.request_may_have_run and stream.closed and len(requests) == 1
    assert "untrusted" not in str(error.value) and "synthetic" not in str(error.value)


async def test_token_limit_terminal_is_truncation_and_never_published_as_complete_text() -> None:
    stream = Chunks(response_bytes(finish="length", tokens=1))
    with pytest.raises(AsrCallError) as error:
        await client_for(stream, []).transcribe(
            AUDIO, replace(POLICY, max_completion_tokens=1), cancelled=lambda: False
        )
    assert error.value.code == AsrFailure.OUTPUT_TRUNCATED
    assert error.value.completion_tokens == 1 and error.value.request_may_have_run


@pytest.mark.parametrize("kind", ["tokens", "line", "text", "events", "total"])
async def test_output_caps_stop_consumption_and_close_the_stream(kind: str) -> None:
    if kind == "tokens":
        body = response_bytes(tokens=1025)
    elif kind == "line":
        body = b"data: " + b"x" * 65537
    elif kind == "text":
        body = event([{"delta": {"content": "x" * 40000}}]) * 2
    elif kind == "events":
        body = event([{"delta": {"content": ""}}]) * 8193
    else:
        body = b":" + b"x" * 4094 + b"\n"
        body *= 257
    stream = Chunks(body, size=4096)
    with pytest.raises(AsrCallError) as error:
        await client_for(stream, []).transcribe(AUDIO, POLICY, cancelled=lambda: False)
    assert error.value.code == AsrFailure.OUTPUT_LIMIT_EXCEEDED and stream.closed


class WaitingStream(httpx.AsyncByteStream):
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.closed = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        self.started.set()
        yield b"data:"
        await asyncio.Event().wait()

    async def aclose(self) -> None:
        self.closed = True


async def test_cancel_before_send_makes_no_request() -> None:
    requests: list[httpx.Request] = []
    with pytest.raises(AsrCallError) as error:
        await client_for(Chunks(b""), requests).transcribe(AUDIO, POLICY, cancelled=lambda: True)
    assert error.value.code == AsrFailure.CANCELLED
    assert not error.value.request_may_have_run and not requests


async def test_inflight_cancel_closes_transport_but_preserves_possible_engine_execution() -> None:
    stream = WaitingStream()
    cancel = asyncio.Event()
    task = asyncio.create_task(
        client_for(stream, []).transcribe(AUDIO, POLICY, cancelled=cancel.is_set)
    )
    await asyncio.wait_for(stream.started.wait(), timeout=2)
    cancel.set()
    with pytest.raises(AsrCallError) as error:
        await asyncio.wait_for(task, timeout=2)
    assert error.value.code == AsrFailure.CANCELLED
    assert error.value.request_may_have_run and stream.closed


async def test_absolute_deadline_also_interrupts_a_partial_event_that_never_finishes() -> None:
    stream = WaitingStream()
    with pytest.raises(AsrCallError) as error:
        await asyncio.wait_for(
            client_for(stream, []).transcribe(
                AUDIO, replace(POLICY, timeout_seconds=0.05), cancelled=lambda: False
            ),
            timeout=2,
        )
    assert error.value.code == AsrFailure.DEADLINE_EXCEEDED
    assert error.value.request_may_have_run and stream.closed


async def test_native_task_cancellation_is_propagated_after_closing_the_response() -> None:
    stream = WaitingStream()
    task = asyncio.create_task(
        client_for(stream, []).transcribe(AUDIO, POLICY, cancelled=lambda: False)
    )
    await asyncio.wait_for(stream.started.wait(), timeout=2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert stream.closed


async def test_transport_failure_is_sanitized_and_not_retried() -> None:
    requests: list[httpx.Request] = []

    def fail(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        raise httpx.ReadError("synthetic private vendor details")

    client = VllmAsrRecognizer(ENDPOINT, transport=httpx.MockTransport(fail))
    with pytest.raises(AsrCallError) as error:
        await client.transcribe(AUDIO, POLICY, cancelled=lambda: False)
    assert error.value.code == AsrFailure.TRANSPORT_UNKNOWN
    assert error.value.request_may_have_run and len(requests) == 1
    assert "private" not in str(error.value)


@pytest.mark.parametrize(
    "changes",
    [
        {"address": "https://127.0.0.1"},
        {"address": "example.com"},
        {"address": "8.8.8.8"},
        {"address": "0.0.0.0"},
        {"address": "169.254.169.254"},
        {"port": True},
        {"served_model": "/synthetic/private/path"},
        {"bearer_token": "synthetic\nheader"},
    ],
)
def test_endpoint_is_private_pinned_and_never_accepts_a_client_url(changes: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        replace(ENDPOINT, **changes)


@pytest.mark.parametrize(
    "changes",
    [
        {"language": "auto\nheader"},
        {"language": True},
        {"prompt": "\ud800"},
        {"prompt": "x" * 10001},
        {"temperature": float("nan")},
        {"temperature": True},
        {"max_completion_tokens": True},
        {"max_completion_tokens": 0},
        {"timeout_seconds": float("inf")},
    ],
)
def test_frozen_policy_is_strict(changes: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        replace(POLICY, **changes)


@pytest.mark.parametrize(
    "changes",
    [
        {"request_id": "synthetic\nheader"},
        {"pcm16": b""},
        {"pcm16": b"x"},
        {"pcm16": bytearray(b"\x00\x00")},
        {"pcm16": b"\x00\x00" * (16000 * 30 + 1)},
    ],
)
def test_native_segment_requires_bounded_complete_private_pcm(changes: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        replace(AUDIO, **changes)


def test_frozen_policy_hash_changes_with_effective_parameters_and_hides_prompt() -> None:
    assert replace(POLICY, prompt="Synthetic private hint").digest != POLICY.digest
    assert replace(POLICY, language="zh").digest != POLICY.digest
    assert replace(POLICY, max_completion_tokens=512).digest != POLICY.digest
    assert "Synthetic private hint" not in repr(replace(POLICY, prompt="Synthetic private hint"))
    endpoint = replace(ENDPOINT, bearer_token="synthetic-secret")
    assert "synthetic-secret" not in repr(endpoint) and "127.0.0.1" not in repr(endpoint)
