"""Single-use file synthesis: one frozen request, one engine call, one bounded delivery."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass

from speechnexus_gateway.domain.tts import (
    TtsAudio,
    TtsCallError,
    TtsDelivery,
    TtsDeliveryEncoder,
    TtsDeliveryFormat,
    TtsFailure,
    TtsPolicy,
    TtsRequest,
    TtsSynthesizer,
)


@dataclass(frozen=True, slots=True)
class FileTtsResult:
    execution_id: str
    request_id: str
    policy_digest: str
    binding_revision_id: str
    engine_media_type: str
    sample_rate_hz: int
    channels: int
    pcm_bytes: int
    duration_ms: int
    delivery: TtsDelivery


class FileTtsSynthesis:
    """Single use. The caller must already hold an admitted execution."""

    def __init__(
        self,
        *,
        synthesizer: TtsSynthesizer,
        encoder: TtsDeliveryEncoder,
        request: TtsRequest,
        policy: TtsPolicy,
        delivery_format: TtsDeliveryFormat,
        max_payload_bytes: int,
        cancelled: Callable[[], bool],
        execution_id: str,
    ) -> None:
        if type(execution_id) is not str or not 1 <= len(execution_id) <= 128:
            raise ValueError("a fixed execution identity is required")
        if not isinstance(delivery_format, TtsDeliveryFormat):
            raise ValueError("unsupported TTS delivery format")
        self._synthesizer = synthesizer
        self._encoder = encoder
        self._request = request
        self._policy = policy
        self._delivery_format = delivery_format
        self._max_payload_bytes = max_payload_bytes
        self._cancelled = cancelled
        self._execution_id = execution_id
        self._binding_revision = synthesizer.binding_revision_id
        self._has_dispatched = False

    @property
    def has_dispatched(self) -> bool:
        return self._has_dispatched

    def _check(self) -> None:
        if self._cancelled():
            raise TtsCallError(TtsFailure.CANCELLED, request_may_have_run=self._has_dispatched)

    async def run(self) -> FileTtsResult:
        self._check()
        if self._synthesizer.binding_revision_id != self._binding_revision:
            raise ValueError("TTS binding cannot change within an execution")
        self._has_dispatched = True
        audio = self._validated(
            await self._synthesizer.synthesize(
                self._request, self._policy, cancelled=self._cancelled
            )
        )
        self._check()
        delivery = await asyncio.to_thread(
            self._encoder.encode,
            audio,
            target=self._delivery_format,
            max_payload_bytes=self._max_payload_bytes,
            cancelled=self._cancelled,
        )
        self._check()
        return FileTtsResult(
            self._execution_id,
            self._request.request_id,
            self._policy.digest,
            self._binding_revision,
            audio.engine_media_type,
            audio.sample_rate_hz,
            audio.channels,
            len(audio.pcm16),
            audio.duration_ms,
            delivery,
        )

    def _validated(self, audio: TtsAudio) -> TtsAudio:
        if (
            audio.request_id != self._request.request_id
            or audio.policy_digest != self._policy.digest
            or audio.sample_rate_hz != self._policy.sample_rate_hz
            or audio.channels != 1
            or audio.duration_ms > self._policy.max_audio_ms
        ):
            raise TtsCallError(TtsFailure.RESPONSE_INVALID, request_may_have_run=True)
        return audio
