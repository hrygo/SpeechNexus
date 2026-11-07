"""Fixed private Base voice-clone synthesis over the vLLM-Omni speech endpoint.

The verified vendor call is ``POST /v1/audio/speech`` with ``task_type`` set to
``Base``, the text to speak in ``input``, the reference clip as a base64 data URL
in ``ref_audio`` and its transcript in ``ref_text`` (or
``x_vector_only_mode: true`` when no transcript is approved). The adapter issues
exactly one request per synthesis, never follows a redirect and never calls the
engine sleep endpoint: only a stopped container or terminated process releases
memory.

Reference bytes are private content: this module never logs a request body, a
data URL or a transcript, and its exceptions carry only a failure code.
"""

from __future__ import annotations

import asyncio
import base64
import importlib
import json
import re
from collections.abc import Callable
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any

from speechnexus_gateway.domain.languages import engine_language
from speechnexus_gateway.domain.tts import TtsAudio
from speechnexus_gateway.domain.voice_clone import (
    VOICE_CLONE_SAMPLE_RATE_HZ,
    ReferenceTextMode,
    VoiceCloneCallError,
    VoiceCloneExecutionTarget,
    VoiceCloneFailure,
    VoiceClonePolicy,
    VoiceCloneRejectedError,
    VoiceCloneRequest,
)
from speechnexus_gateway.infrastructure.private_engine_endpoint import (
    PrivateEngineEndpoint,
    load_controlled_endpoint_descriptor,
)
from speechnexus_gateway.infrastructure.vllm_audio import wav_pcm

_MEDIA_TYPE = "audio/wav"
_MAX_ENVELOPE_BYTES = 65536
_MAX_RESPONSE_BYTES = VOICE_CLONE_SAMPLE_RATE_HZ * 60 * 2 + _MAX_ENVELOPE_BYTES


@dataclass(frozen=True, slots=True)
class VllmVoiceCloneEndpoint(PrivateEngineEndpoint):
    """A configured private IP literal for the fixed vLLM-Omni Base endpoint."""

    execution_target: VoiceCloneExecutionTarget | None = None

    def __post_init__(self) -> None:
        PrivateEngineEndpoint.__post_init__(self)
        if self.execution_target is not None and (
            self.execution_target.binding_revision_id != self.binding_revision_id
            or self.bearer_token is None
            or re.fullmatch(r"[0-9a-f]{64}", self.bearer_token) is None
        ):
            raise ValueError(
                "a controlled voice clone target requires its native generation credential"
            )


def load_controlled_voice_clone_endpoint(path: Path) -> VllmVoiceCloneEndpoint:
    """Read one private, bounded deployment descriptor; caller input cannot select it."""
    try:
        descriptor = load_controlled_endpoint_descriptor(path)
        target = descriptor.target
        if set(target) != {item.name for item in fields(VoiceCloneExecutionTarget)}:
            raise ValueError
        return VllmVoiceCloneEndpoint(
            address=descriptor.address,
            port=descriptor.port,
            served_model=descriptor.served_model,
            binding_revision_id=target["binding_revision_id"],
            bearer_token=descriptor.bearer_token,
            execution_target=VoiceCloneExecutionTarget(**target),
        )
    except OSError, ValueError, TypeError, KeyError, UnicodeError, RecursionError:
        raise ValueError("invalid private controlled voice clone deployment descriptor") from None


def voice_clone_body(request: VoiceCloneRequest, endpoint: VllmVoiceCloneEndpoint) -> bytes:
    """The exact vendor request body; no voice, speed or sample-clock overrides."""
    document: dict[str, Any] = {
        "model": endpoint.served_model,
        "input": request.preview_text,
        "task_type": "Base",
        "language": engine_language(request.language),
        "ref_audio": "data:audio/wav;base64,"
        + base64.b64encode(request.reference_audio.wav()).decode("ascii"),
        "response_format": "wav",
    }
    if request.mode is ReferenceTextMode.ICL:
        document["ref_text"] = request.reference_text
    else:
        # Omission is not a mode in the vendor API; the flag is explicit.
        document["x_vector_only_mode"] = True
    return json.dumps(document, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode(
        "utf-8"
    )


def _invalid() -> VoiceCloneCallError:
    return VoiceCloneCallError(VoiceCloneFailure.RESPONSE_INVALID, request_may_have_run=True)


def _limit() -> VoiceCloneCallError:
    return VoiceCloneCallError(VoiceCloneFailure.OUTPUT_LIMIT_EXCEEDED, request_may_have_run=True)


class VllmVoiceCloneSynthesizer:
    """One call per admitted clone request, with bounded structural verification.

    A transport close never attests engine stop. A client-error answer is the only
    failure that positively reports that this request did not run; a timeout, a
    transport failure or a server error stays unknown and keeps capacity held.
    """

    def __init__(
        self,
        endpoint: VllmVoiceCloneEndpoint,
        *,
        transport: Any = None,
        warmup_request: VoiceCloneRequest | None = None,
        warmup_policy: VoiceClonePolicy | None = None,
    ) -> None:
        try:
            self._httpx: Any = importlib.import_module("httpx")
        except ImportError:
            raise ValueError("the isolated voice clone HTTP runtime is unavailable") from None
        if (warmup_request is None) != (warmup_policy is None):
            raise ValueError("a warmup requires both its frozen request and policy")
        self._endpoint = endpoint
        self._transport = transport
        self._warmup_request = warmup_request
        self._warmup_policy = warmup_policy

    @property
    def binding_revision_id(self) -> str:
        return self._endpoint.binding_revision_id

    def validate_execution_target(self, target: VoiceCloneExecutionTarget) -> None:
        if self._endpoint.execution_target is None or self._endpoint.execution_target != target:
            raise ValueError("configured voice clone generation does not match the admitted target")

    def _headers(self, request_id: str) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "Accept": _MEDIA_TYPE,
            "Accept-Encoding": "identity",
            "X-Request-ID": request_id,
        }
        if self._endpoint.bearer_token is not None:
            headers["Authorization"] = "Bearer " + self._endpoint.bearer_token
        return headers

    async def _post(self, body: bytes, *, request_id: str, timeout_seconds: float) -> bytes:
        async with (
            self._httpx.AsyncClient(
                base_url=self._endpoint.base_url,
                timeout=self._httpx.Timeout(timeout_seconds, connect=5),
                trust_env=False,
                follow_redirects=False,
                transport=self._transport,
            ) as client,
            client.stream(
                "POST", "/v1/audio/speech", headers=self._headers(request_id), content=body
            ) as response,
        ):
            # A rate limit or an upload timeout is a transient answer: the engine
            # may read the request again, so neither proves the request died.
            if response.status_code in {408, 429}:
                raise VoiceCloneCallError(
                    VoiceCloneFailure.TRANSPORT_UNKNOWN, request_may_have_run=True
                )
            if response.is_client_error:
                raise VoiceCloneRejectedError()
            if response.status_code != 200:
                raise VoiceCloneCallError(
                    VoiceCloneFailure.TRANSPORT_UNKNOWN, request_may_have_run=True
                )
            if (
                response.headers.get("content-type", "").split(";")[0] != _MEDIA_TYPE
                or response.headers.get("content-encoding", "identity") != "identity"
            ):
                raise _invalid()
            payload = bytearray()
            async for chunk in response.aiter_bytes(chunk_size=65536):
                # One minute of 24 kHz mono PCM16 plus a bounded RIFF envelope.
                if len(payload) + len(chunk) > _MAX_RESPONSE_BYTES:
                    raise _limit()
                payload.extend(chunk)
            return bytes(payload)

    def _pcm(self, payload: bytes, policy: VoiceClonePolicy) -> bytes:
        if len(payload) > policy.max_pcm_bytes + _MAX_ENVELOPE_BYTES:
            raise _limit()
        return wav_pcm(
            payload,
            sample_rate_hz=policy.sample_rate_hz,
            max_pcm_bytes=policy.max_pcm_bytes,
            on_invalid=lambda _: _invalid(),
            on_limit=lambda _: _limit(),
        )

    async def synthesize(
        self,
        request: VoiceCloneRequest,
        policy: VoiceClonePolicy,
        *,
        cancelled: Callable[[], bool],
    ) -> TtsAudio:
        if cancelled():
            raise VoiceCloneCallError(VoiceCloneFailure.CANCELLED, request_may_have_run=False)
        body = voice_clone_body(request, self._endpoint)
        started = False

        async def call() -> TtsAudio:
            nonlocal started
            started = True
            payload = await self._post(
                body, request_id=request.request_id, timeout_seconds=policy.timeout_seconds
            )
            pcm = self._pcm(payload, policy)
            duration_ms = len(pcm) // 2 * 1000 // policy.sample_rate_hz
            if duration_ms < 1 or duration_ms > policy.max_audio_ms:
                raise _limit()
            return TtsAudio(
                request.request_id,
                _MEDIA_TYPE,
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
                    raise VoiceCloneCallError(
                        VoiceCloneFailure.CANCELLED, request_may_have_run=started
                    )
                return await work
        except TimeoutError:
            raise VoiceCloneCallError(
                VoiceCloneFailure.DEADLINE_EXCEEDED, request_may_have_run=started
            ) from None
        except VoiceCloneCallError:
            raise
        except self._httpx.TimeoutException:
            raise VoiceCloneCallError(
                VoiceCloneFailure.DEADLINE_EXCEEDED, request_may_have_run=started
            ) from None
        except self._httpx.HTTPError:
            raise VoiceCloneCallError(
                VoiceCloneFailure.TRANSPORT_UNKNOWN, request_may_have_run=started
            ) from None
        finally:
            work.cancel()
            cancel.cancel()
            await asyncio.gather(work, cancel, return_exceptions=True)

    async def warmup(self) -> None:
        """One real clone synthesis with the approved reference; a health answer is not enough."""
        if self._warmup_request is None or self._warmup_policy is None:
            raise ValueError("no approved voice clone warmup reference is configured")
        audio = await self.synthesize(
            self._warmup_request, self._warmup_policy, cancelled=lambda: False
        )
        if audio.duration_ms < 1:
            raise VoiceCloneCallError(VoiceCloneFailure.RESPONSE_INVALID, request_may_have_run=True)
