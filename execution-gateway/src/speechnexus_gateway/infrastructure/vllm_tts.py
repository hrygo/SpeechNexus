"""Fixed private speech synthesis with bounded, structurally verified audio responses."""

from __future__ import annotations

import asyncio
import importlib
import json
import struct
from collections.abc import Callable
from typing import Any

from speechnexus_gateway.domain.tts import (
    TtsAudio,
    TtsCallError,
    TtsEngineFormat,
    TtsFailure,
    TtsPolicy,
    TtsRequest,
)
from speechnexus_gateway.infrastructure.private_engine_endpoint import (
    PrivateEngineEndpoint,
)

_MEDIA_TYPES = {TtsEngineFormat.WAV: "audio/wav", TtsEngineFormat.PCM: "audio/pcm"}
_MAX_ENVELOPE_BYTES = 65536
_MAX_WAV_CHUNKS = 32


class VllmTtsEndpoint(PrivateEngineEndpoint):
    """A configured private IP literal for the fixed vLLM-Omni speech endpoint."""


def _invalid(*, audio_bytes: int | None = None) -> TtsCallError:
    return TtsCallError(
        TtsFailure.RESPONSE_INVALID, request_may_have_run=True, audio_bytes=audio_bytes
    )


def _limit(*, audio_bytes: int | None = None) -> TtsCallError:
    return TtsCallError(
        TtsFailure.OUTPUT_LIMIT_EXCEEDED, request_may_have_run=True, audio_bytes=audio_bytes
    )


def _wav_pcm(payload: bytes, *, sample_rate_hz: int, max_pcm_bytes: int) -> bytes:
    """Extract mono PCM16LE from a plain RIFF/WAVE body, rejecting anything else."""

    if len(payload) < 44 or payload[:4] != b"RIFF" or payload[8:12] != b"WAVE":
        raise _invalid(audio_bytes=len(payload))
    declared = struct.unpack_from("<I", payload, 4)[0]
    end_limit = declared + 8
    if end_limit < 44 or end_limit > len(payload):
        raise _invalid(audio_bytes=len(payload))
    offset = 12
    chunks = 0
    format_seen = False
    data: bytes | None = None
    while offset < end_limit:
        if offset + 8 > end_limit:
            raise _invalid(audio_bytes=len(payload))
        chunk_id = payload[offset : offset + 4]
        size = struct.unpack_from("<I", payload, offset + 4)[0]
        body = offset + 8
        end = body + size
        if end > end_limit:
            raise _invalid(audio_bytes=len(payload))
        chunks += 1
        if chunks > _MAX_WAV_CHUNKS:
            raise _invalid(audio_bytes=len(payload))
        if chunk_id == b"fmt ":
            if format_seen or size < 16:
                raise _invalid(audio_bytes=len(payload))
            format_seen = True
            fields = struct.unpack_from("<HHIIHH", payload, body)
            if fields != (1, 1, sample_rate_hz, sample_rate_hz * 2, 2, 16):
                raise _invalid(audio_bytes=len(payload))
        elif chunk_id == b"data":
            if data is not None or size == 0 or size % 2:
                raise _invalid(audio_bytes=len(payload))
            data = payload[body:end]
        offset = end + (size & 1)
    if not format_seen or data is None or offset != end_limit:
        raise _invalid(audio_bytes=len(payload))
    if len(data) > max_pcm_bytes:
        raise _limit(audio_bytes=len(payload))
    return data


def _request_body(request: TtsRequest, policy: TtsPolicy, endpoint: VllmTtsEndpoint) -> bytes:
    document: dict[str, Any] = {
        "model": endpoint.served_model,
        "input": request.text,
        "voice": policy.voice,
        "task_type": policy.task.value,
        "response_format": policy.engine_format.value,
        "sample_rate": policy.sample_rate_hz,
        "speed": policy.speed,
    }
    if policy.instructions:
        document["instructions"] = policy.instructions
    if policy.language is not None:
        document["language"] = policy.language
    if policy.seed is not None:
        document["seed"] = policy.seed
    return json.dumps(document, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode(
        "utf-8"
    )


class VllmTtsSynthesizer:
    """One call per admitted text input, with no retry or redirect.

    A transport close never attests engine stop, and the engine may internally
    retry its own generation before answering; this adapter only guarantees that
    the gateway issues one HTTP request.
    """

    def __init__(self, endpoint: VllmTtsEndpoint, *, transport: Any = None) -> None:
        try:
            self._httpx: Any = importlib.import_module("httpx")
        except ImportError:
            raise ValueError("the isolated TTS HTTP runtime is unavailable") from None
        self._endpoint = endpoint
        self._transport = transport

    @property
    def binding_revision_id(self) -> str:
        return self._endpoint.binding_revision_id

    async def synthesize(
        self,
        request: TtsRequest,
        policy: TtsPolicy,
        *,
        cancelled: Callable[[], bool],
    ) -> TtsAudio:
        if cancelled():
            raise TtsCallError(TtsFailure.CANCELLED, request_may_have_run=False)
        media_type = _MEDIA_TYPES[policy.engine_format]
        headers = {
            "Content-Type": "application/json",
            "Accept": media_type,
            "Accept-Encoding": "identity",
            "X-Request-ID": request.request_id,
        }
        if self._endpoint.bearer_token is not None:
            headers["Authorization"] = "Bearer " + self._endpoint.bearer_token
        body = _request_body(request, policy, self._endpoint)
        max_response_bytes = policy.max_pcm_bytes + _MAX_ENVELOPE_BYTES
        started = False

        async def call() -> TtsAudio:
            nonlocal started
            async with self._httpx.AsyncClient(
                base_url=self._endpoint.base_url,
                timeout=self._httpx.Timeout(policy.timeout_seconds, connect=5),
                trust_env=False,
                follow_redirects=False,
                transport=self._transport,
            ) as client:
                started = True
                async with client.stream(
                    "POST", "/v1/audio/speech", headers=headers, content=body
                ) as response:
                    if (
                        response.status_code != 200
                        or response.headers.get("content-type", "").split(";")[0] != media_type
                        or response.headers.get("content-encoding", "identity") != "identity"
                        or response.headers.get("x-audio-sample-rate") != str(policy.sample_rate_hz)
                        or response.headers.get("x-audio-channels") != "1"
                    ):
                        raise _invalid()
                    if response.headers.get("x-audio-truncated") == "true":
                        raise TtsCallError(TtsFailure.OUTPUT_TRUNCATED, request_may_have_run=True)
                    payload = bytearray()
                    async for chunk in response.aiter_bytes(chunk_size=65536):
                        if len(payload) + len(chunk) > max_response_bytes:
                            raise _limit(audio_bytes=len(payload) + len(chunk))
                        payload.extend(chunk)
                    if policy.engine_format is TtsEngineFormat.WAV:
                        pcm = _wav_pcm(
                            bytes(payload),
                            sample_rate_hz=policy.sample_rate_hz,
                            max_pcm_bytes=policy.max_pcm_bytes,
                        )
                    else:
                        if not payload or len(payload) % 2:
                            raise _invalid(audio_bytes=len(payload))
                        if len(payload) > policy.max_pcm_bytes:
                            raise _limit(audio_bytes=len(payload))
                        pcm = bytes(payload)
                    duration_ms = len(pcm) // 2 * 1000 // policy.sample_rate_hz
                    if duration_ms < 1 or duration_ms > policy.max_audio_ms:
                        raise _limit(audio_bytes=len(pcm))
                    return TtsAudio(
                        request.request_id,
                        media_type,
                        pcm,
                        policy.sample_rate_hz,
                        1,
                        policy.digest,
                    )

        async def cancellation() -> None:
            while not cancelled():  # noqa: ASYNC110
                await asyncio.sleep(0.025)

        work = asyncio.create_task(call())
        cancel = asyncio.create_task(cancellation())
        try:
            async with asyncio.timeout(policy.timeout_seconds):
                done, _ = await asyncio.wait((work, cancel), return_when=asyncio.FIRST_COMPLETED)
                if cancel in done:
                    await cancel
                    raise TtsCallError(TtsFailure.CANCELLED, request_may_have_run=started)
                return await work
        except TimeoutError:
            raise TtsCallError(TtsFailure.DEADLINE_EXCEEDED, request_may_have_run=started) from None
        except self._httpx.HTTPError:
            raise TtsCallError(TtsFailure.TRANSPORT_UNKNOWN, request_may_have_run=started) from None
        finally:
            work.cancel()
            cancel.cancel()
            await asyncio.gather(work, cancel, return_exceptions=True)
