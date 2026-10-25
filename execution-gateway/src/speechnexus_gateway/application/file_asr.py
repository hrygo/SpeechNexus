"""Sequential file recognition with VAD gating and source sample provenance."""

from __future__ import annotations

import asyncio
import hashlib
import math
import time
from collections.abc import AsyncGenerator, Callable
from dataclasses import dataclass, field

from speechnexus_gateway.application.file_asr_audio import FileAsrAudio, PreparedAsrSegment
from speechnexus_gateway.domain.asr import (
    AsrAudio,
    AsrCallError,
    AsrFailure,
    AsrPolicy,
    AsrRecognizer,
)
from speechnexus_gateway.domain.canonical import command_digest
from speechnexus_gateway.domain.media import (
    AudioDecodeSummary,
    MediaCancelledError,
    SourceAudioClock,
)


@dataclass(frozen=True, slots=True)
class FileAsrSegment:
    sequence: int
    source_clock: SourceAudioClock
    source_sample_span: tuple[int, int]
    model_sample_span: tuple[int, int]
    text: str = field(repr=False)
    no_speech: bool
    request_id: str | None
    engine_request_id: str | None
    completion_tokens: int
    config_revision: str
    policy_digest: str
    vad_policy_digest: str
    binding_revision_id: str


class FileAsrTranscription:
    """Single use. The caller must hold a persisted/admitted execution before iterating."""

    def __init__(
        self,
        *,
        audio: FileAsrAudio,
        recognizer: AsrRecognizer,
        execution_id: str,
        policy: AsrPolicy,
        cancelled: Callable[[], bool],
        final_deadline_seconds: float | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if type(execution_id) is not str or not 1 <= len(execution_id) <= 128:
            raise ValueError("a fixed execution identity is required")
        if final_deadline_seconds is not None and (
            type(final_deadline_seconds) not in (int, float)
            or not math.isfinite(final_deadline_seconds)
            or not 0 < final_deadline_seconds <= 10
        ):
            raise ValueError("invalid file ASR final deadline")
        self.summary: AudioDecodeSummary | None = None
        self._audio = audio
        self._recognizer = recognizer
        self._binding_revision = recognizer.binding_revision_id
        self._execution_id = execution_id
        self._policy = policy
        self._cancelled = cancelled
        self._final_deadline = final_deadline_seconds
        self._monotonic = monotonic
        self._has_dispatched = False
        self._iterator = self._recognize()

    @property
    def has_dispatched(self) -> bool:
        """Conservative execution fact; never a stop or resource-release proof."""
        return self._has_dispatched

    def __aiter__(self) -> FileAsrTranscription:
        return self

    async def __anext__(self) -> FileAsrSegment:
        return await self._iterator.__anext__()

    async def aclose(self) -> None:
        try:
            await self._iterator.aclose()
        finally:
            self._audio.close()

    def _check(self) -> None:
        if self._cancelled():
            raise AsrCallError(AsrFailure.CANCELLED, request_may_have_run=self._has_dispatched)

    def _remaining_final(self, prepared: PreparedAsrSegment) -> float | None:
        if self._final_deadline is None:
            return None
        closed_at = prepared.closed_at_monotonic
        now = self._monotonic()
        if (
            closed_at is None
            or type(closed_at) not in (int, float)
            or not math.isfinite(closed_at)
            or not math.isfinite(now)
            or closed_at > now
        ):
            raise ValueError("file ASR requires a valid segment closing clock")
        remaining = closed_at + self._final_deadline - now
        if remaining <= 0:
            raise AsrCallError(
                AsrFailure.DEADLINE_EXCEEDED, request_may_have_run=self._has_dispatched
            )
        return remaining

    async def _recognize(self) -> AsyncGenerator[FileAsrSegment]:
        completed: AudioDecodeSummary | None = None
        try:
            self._check()
            for prepared in self._audio:
                self._check()
                vad = prepared.vad
                if vad.config_revision != self._policy.config_revision:
                    raise ValueError("VAD and ASR must consume the same frozen config revision")
                if self._recognizer.binding_revision_id != self._binding_revision:
                    raise ValueError("ASR binding cannot change within an execution")
                self._remaining_final(prepared)
                request_id = None
                engine_request_id = None
                text = ""
                completion_tokens = 0
                if vad.has_speech:
                    request_id = "nexus-" + command_digest(
                        {
                            "execution_id": self._execution_id,
                            "sequence": vad.sequence,
                            "sample_start": vad.sample_start,
                            "sample_end": vad.sample_end,
                            "audio_sha256": hashlib.sha256(vad.pcm16).hexdigest(),
                            "policy_digest": self._policy.digest,
                            "binding_revision_id": self._binding_revision,
                        }
                    ).removeprefix("sha256:")
                    remaining = self._remaining_final(prepared)
                    self._has_dispatched = True
                    try:
                        async with asyncio.timeout(remaining):
                            recognized = await self._recognizer.transcribe(
                                AsrAudio(request_id, vad.pcm16),
                                self._policy,
                                cancelled=self._cancelled,
                            )
                    except TimeoutError:
                        raise AsrCallError(
                            AsrFailure.DEADLINE_EXCEEDED, request_may_have_run=True
                        ) from None
                    if (
                        recognized.request_id != request_id
                        or recognized.policy_digest != self._policy.digest
                        or type(recognized.completion_tokens) is not int
                        or not 0
                        <= recognized.completion_tokens
                        <= self._policy.max_completion_tokens
                        or type(recognized.text) is not str
                    ):
                        raise AsrCallError(AsrFailure.RESPONSE_INVALID, request_may_have_run=True)
                    self._check()
                    text = recognized.text
                    engine_request_id = recognized.engine_request_id
                    completion_tokens = recognized.completion_tokens
                self._remaining_final(prepared)
                yield FileAsrSegment(
                    vad.sequence,
                    prepared.source_clock,
                    prepared.source_sample_span,
                    (vad.sample_start, vad.sample_end),
                    text,
                    not vad.has_speech,
                    request_id,
                    engine_request_id,
                    completion_tokens,
                    self._policy.config_revision,
                    self._policy.digest,
                    vad.policy_digest,
                    self._binding_revision,
                )
            self._check()
            completed = self._audio.summary
            if completed is None:
                raise ValueError("file audio did not complete")
        except MediaCancelledError:
            raise AsrCallError(
                AsrFailure.CANCELLED, request_may_have_run=self._has_dispatched
            ) from None
        finally:
            self._audio.close()
        self.summary = completed
