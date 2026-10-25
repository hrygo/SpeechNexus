"""Single-use file synthesis: one frozen request, one engine call, one bounded delivery."""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Callable
from dataclasses import dataclass

from speechnexus_gateway.domain.execution_results import (
    MAX_AUDIO_RESULT_BYTES,
    ExecutionAudioStagingResult,
)
from speechnexus_gateway.domain.tts import (
    TtsAudio,
    TtsCallError,
    TtsDelivery,
    TtsDeliveryEncoder,
    TtsDeliveryError,
    TtsDeliveryFailure,
    TtsDeliveryFormat,
    TtsFailure,
    TtsPolicy,
    TtsRequest,
    TtsSynthesizer,
)
from speechnexus_gateway.domain.tts_results import (
    PreparedTtsResult,
    TtsResultStore,
    validate_tts_result_identity,
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
        if (
            type(max_payload_bytes) is not int
            or not 1024 <= max_payload_bytes <= MAX_AUDIO_RESULT_BYTES
        ):
            raise ValueError("invalid TTS delivery budget")
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
        if self._has_dispatched:
            raise ValueError("an original TTS synthesis cannot be dispatched again")
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
        if (
            delivery.format is not self._delivery_format
            or delivery.media_type != self._delivery_format.media_type
            or delivery.sample_rate_hz != audio.sample_rate_hz
            or delivery.channels != audio.channels
            or delivery.duration_ms != audio.duration_ms
            or delivery.source_pcm_digest != "sha256:" + hashlib.sha256(audio.pcm16).hexdigest()
            or len(delivery.payload) > self._max_payload_bytes
        ):
            raise TtsDeliveryError(TtsDeliveryFailure.ENCODE_FAILED)
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

    async def stage_result(
        self, *, workspace_id: str, request_digest: str, store: TtsResultStore
    ) -> ExecutionAudioStagingResult:
        """One synthesis and verified private PUT, never a terminal or meter write.

        The trusted caller must append the actual source interval through the
        original B port before registering this receipt; registration checks it.
        A lost object acknowledgment never authorizes calling this method again.
        """
        validate_tts_result_identity(workspace_id, request_digest)
        if self._policy.sample_rate_hz != 24000:
            raise ValueError("persistent file TTS requires the 24 kHz mono clock")
        result = await self.run()
        if (
            result.pcm_bytes % 2
            or result.sample_rate_hz != result.delivery.sample_rate_hz
            or result.channels != result.delivery.channels
            or result.duration_ms != result.delivery.duration_ms
        ):
            raise ValueError("TTS delivery contradicts the synthesized source clock")
        prepared = PreparedTtsResult.from_delivery(
            workspace_id=workspace_id,
            request_digest=request_digest,
            delivery=result.delivery,
            source_pcm_samples=result.pcm_bytes // 2,
        )
        self._check()
        return await store.stage(prepared, cancelled=self._cancelled)

    def _validated(self, audio: TtsAudio) -> TtsAudio:
        if (
            audio.request_id != self._request.request_id
            or audio.policy_digest != self._policy.digest
            or audio.sample_rate_hz != self._policy.sample_rate_hz
            or audio.channels != 1
            or len(audio.pcm16) > self._policy.max_pcm_bytes
            or audio.duration_ms > self._policy.max_audio_ms
        ):
            raise TtsCallError(TtsFailure.RESPONSE_INVALID, request_may_have_run=True)
        return audio
