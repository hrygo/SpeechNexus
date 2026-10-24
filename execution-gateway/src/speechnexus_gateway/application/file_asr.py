"""Sequential file recognition with VAD gating and source sample provenance."""

from __future__ import annotations

import hashlib
from collections.abc import AsyncGenerator, Callable
from dataclasses import dataclass, field

from speechnexus_gateway.application.file_asr_audio import FileAsrAudio
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
    ) -> None:
        if type(execution_id) is not str or not 1 <= len(execution_id) <= 128:
            raise ValueError("a fixed execution identity is required")
        self.summary: AudioDecodeSummary | None = None
        self._audio = audio
        self._recognizer = recognizer
        self._binding_revision = recognizer.binding_revision_id
        self._execution_id = execution_id
        self._policy = policy
        self._cancelled = cancelled
        self._has_dispatched = False
        self._iterator = self._recognize()

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
                    self._has_dispatched = True
                    recognized = await self._recognizer.transcribe(
                        AsrAudio(request_id, vad.pcm16), self._policy, cancelled=self._cancelled
                    )
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
