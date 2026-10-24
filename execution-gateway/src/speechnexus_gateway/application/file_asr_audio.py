"""Prepare real file audio for ASR without owning admission, dispatch or publication."""

from __future__ import annotations

from collections.abc import Callable, Generator
from dataclasses import dataclass
from typing import BinaryIO

from speechnexus_gateway.application.vad import CpuVadSession
from speechnexus_gateway.domain.media import (
    AudioDecodeLimits,
    AudioDecoder,
    AudioDecodeSummary,
    AudioSourceFormat,
    MediaCancelledError,
    MediaError,
    SourceAudioClock,
)
from speechnexus_gateway.domain.vad import (
    EmptyVadBufferError,
    VadModel,
    VadPolicy,
    VadSegment,
)


@dataclass(frozen=True, slots=True)
class PreparedAsrSegment:
    vad: VadSegment
    source_clock: SourceAudioClock
    source_sample_span: tuple[int, int]


class FileAsrAudio:
    """Single-use stream. Summary is available only after successful EOF and final VAD."""

    def __init__(
        self,
        *,
        decoder: AudioDecoder,
        model: VadModel,
        source: BinaryIO,
        source_format: AudioSourceFormat,
        limits: AudioDecodeLimits,
        vad_policy: VadPolicy,
        cancelled: Callable[[], bool],
    ) -> None:
        self.summary: AudioDecodeSummary | None = None
        self.model_sha256 = model.model_sha256
        self._stream = decoder.open(
            source,
            source_format=source_format,
            limits=limits,
            output_sample_rate_hz=16000,
            cancelled=cancelled,
        )
        try:
            self._vad = CpuVadSession(model=model, policy=vad_policy)
        except Exception:
            self._stream.close()
            raise
        self._cancelled = cancelled
        self._iterator = self._prepare()
        self._closed = False

    def __iter__(self) -> FileAsrAudio:
        return self

    def __next__(self) -> PreparedAsrSegment:
        return next(self._iterator)

    def close(self) -> None:
        try:
            self._iterator.close()
        finally:
            self._close_resources()

    def _close_resources(self) -> None:
        if not self._closed:
            self._closed = True
            try:
                self._stream.close()
            finally:
                self._vad.close()

    def _check(self) -> None:
        if self._cancelled():
            raise MediaCancelledError("file audio preparation was cancelled")

    def _wrap(self, segment: VadSegment, *, final: bool = False) -> PreparedAsrSegment:
        self._check()
        clock = self._stream.clock
        if clock is None:
            raise MediaError("decoder did not provide the source sample clock")
        source_span = clock.coverage(
            segment.sample_start,
            segment.sample_end,
            rate_hz=16000,
            source_samples=self._stream.source_samples,
            final=final,
        )
        return PreparedAsrSegment(segment, clock, source_span)

    def _prepare(self) -> Generator[PreparedAsrSegment]:
        pending: VadSegment | None = None
        completed: AudioDecodeSummary | None = None
        try:
            self._check()
            for block in self._stream:
                self._check()
                if block.sample_rate_hz != 16000:
                    raise MediaError("file ASR requires the declared 16 kHz model clock")
                for segment in self._vad.push(block.pcm16, sample_start=block.sample_start):
                    if pending is not None:
                        yield self._wrap(pending)
                    pending = segment
            self._check()
            if self._stream.summary is None:
                raise MediaError("decoder did not complete the original audio")
            try:
                final = self._vad.commit()
            except EmptyVadBufferError:
                final = ()
            for segment in final:
                if pending is not None:
                    yield self._wrap(pending)
                pending = segment
            if pending is not None:
                yield self._wrap(pending, final=True)
            self._check()
            completed = self._stream.summary
        finally:
            self._close_resources()
        self.summary = completed
