"""Bounded file-like audio decode; no client paths, protocols or secondary resources."""

from __future__ import annotations

import importlib
import io
import time
from collections.abc import Callable, Generator
from fractions import Fraction
from typing import Any, BinaryIO

from speechnexus_gateway.domain.media import (
    AudioDecodeLimits,
    AudioDecodeSummary,
    AudioSourceFormat,
    MediaCancelledError,
    MediaError,
    MediaLimitError,
    MediaTimelineError,
    PcmBlock,
    SourceAudioClock,
)

_FORMATS = {
    "audio/wav": "wav",
    "audio/flac": "flac",
    "audio/mpeg": "mp3",
    "audio/ogg": "ogg",
    "audio/pcm": "s16le",
}
_READ_BYTES = 65536


class _BoundedSource:
    name = "input"

    def __init__(self, source: BinaryIO, *, size: int, check: Callable[[], None]) -> None:
        self._source = source
        self._check = check
        self._read_limit = size * 4 + _READ_BYTES * 4
        self._read_bytes = 0
        self._operations = 0

    def _guard(self) -> None:
        self._check()
        self._operations += 1
        if self._operations > 1000000:
            raise MediaLimitError("audio input exceeded the I/O operation limit")

    def read(self, size: int) -> bytes:
        self._guard()
        if size < 0:
            raise MediaLimitError("unbounded audio reads are forbidden")
        result = self._source.read(min(size, _READ_BYTES))
        if type(result) is not bytes:
            raise MediaError("invalid binary audio source")
        self._read_bytes += len(result)
        if self._read_bytes > self._read_limit:
            raise MediaLimitError("audio input exceeded the read amplification limit")
        return result

    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        self._guard()
        return self._source.seek(offset, whence)

    def tell(self) -> int:
        self._guard()
        return self._source.tell()

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def writable(self) -> bool:
        return False


class PyAvAudioDecoder:
    def __init__(self, *, monotonic: Callable[[], float] = time.monotonic) -> None:
        try:
            self._av: Any = importlib.import_module("av")
        except ImportError:
            raise MediaError("the isolated audio runtime is unavailable") from None
        self._monotonic = monotonic

    def open(
        self,
        source: BinaryIO,
        *,
        source_format: AudioSourceFormat,
        limits: AudioDecodeLimits,
        output_sample_rate_hz: int = 16000,
        cancelled: Callable[[], bool],
    ) -> _DecodeStream:
        if output_sample_rate_hz not in (16000, 24000):
            raise MediaError("unsupported output sample clock")
        return _DecodeStream(
            self._av,
            self._monotonic,
            source,
            source_format,
            limits,
            output_sample_rate_hz,
            cancelled,
        )


class _DecodeStream:
    def __init__(
        self,
        av: Any,
        monotonic: Callable[[], float],
        source: BinaryIO,
        source_format: AudioSourceFormat,
        limits: AudioDecodeLimits,
        output_rate: int,
        cancelled: Callable[[], bool],
    ) -> None:
        self.clock: SourceAudioClock | None = None
        self.source_samples = 0
        self.summary: AudioDecodeSummary | None = None
        self._av = av
        self._monotonic = monotonic
        self._source = source
        self._format = source_format
        self._limits = limits
        self._rate = output_rate
        self._cancelled = cancelled
        self._deadline = monotonic() + limits.timeout_seconds
        self._iterator = self._decode()

    def __iter__(self) -> _DecodeStream:
        return self

    def __next__(self) -> PcmBlock:
        return next(self._iterator)

    def close(self) -> None:
        self._iterator.close()

    def _check(self) -> None:
        if self._cancelled():
            raise MediaCancelledError("audio preparation was cancelled")
        if self._monotonic() >= self._deadline:
            raise MediaLimitError("audio preparation exceeded its deadline")
        if self._av.logging.get_level() is not None:
            raise MediaError("audio runtime vendor logging must remain disabled")
        # get_level() alone cannot detect restore_default_callback(), which
        # restores FFmpeg stderr output without changing PyAV's threshold.
        self._av.logging.set_level(None)

    def _deny_secondary(self, *args: Any, **kwargs: Any) -> Any:
        raise MediaError("secondary audio resources are forbidden")

    def _frame_clock(self, frame: Any) -> None:
        rate = int(frame.sample_rate)
        channels = int(frame.layout.nb_channels)
        if not 8000 <= rate <= 192000 or not 1 <= channels <= 8:
            raise MediaError("unsupported decoded source format")
        if not 0 < frame.samples <= self._limits.max_frame_samples:
            raise MediaLimitError("decoded audio exceeded the frame limit")
        timestamp = (
            Fraction(frame.pts) * frame.time_base
            if frame.pts is not None and frame.time_base is not None
            else None
        )
        if self.clock is None:
            if (
                self._format.sample_rate_hz is not None and self._format.sample_rate_hz != rate
            ) or (self._format.channels is not None and self._format.channels != channels):
                raise MediaError("declared and decoded source formats differ")
            self.clock = SourceAudioClock(rate, channels, timestamp)
        elif (rate, channels) != (self.clock.sample_rate_hz, self.clock.channels):
            raise MediaTimelineError("source audio format changes within a file")
        elif timestamp is not None and self.clock.first_timestamp is not None:
            expected = self.clock.first_timestamp + Fraction(self.source_samples, rate)
            if abs(timestamp - expected) > Fraction(1, rate):
                raise MediaTimelineError("source audio timestamps are discontinuous")
        elif (timestamp is None) != (self.clock.first_timestamp is None):
            raise MediaTimelineError("source audio timestamp availability changes")
        if (self.source_samples + frame.samples) * 1000 > self._limits.max_duration_ms * rate:
            raise MediaLimitError("decoded audio exceeded the duration limit")
        # Normalize the resampler clock while retaining the original origin above.
        frame.pts = self.source_samples
        frame.time_base = Fraction(1, rate)
        self.source_samples += int(frame.samples)

    def _converted(self, frame: Any, start: int) -> Generator[PcmBlock]:
        self._check()
        if (
            frame.sample_rate != self._rate
            or frame.layout.nb_channels != 1
            or not 0 < frame.samples <= self._limits.max_frame_samples * 3 + self._rate // 10
            or (start + frame.samples) * 1000 > self._limits.max_duration_ms * self._rate
        ):
            raise MediaLimitError("resampled audio exceeded its frame or duration limit")
        pcm = frame.to_ndarray().astype("<i2", copy=False).tobytes()
        if len(pcm) != frame.samples * 2:
            raise MediaError("unexpected resampled PCM representation")
        block_bytes = self._rate // 10 * 2
        for offset in range(0, len(pcm), block_bytes):
            self._check()
            yield PcmBlock(self._rate, start + offset // 2, pcm[offset : offset + block_bytes])

    def _decode(self) -> Generator[PcmBlock]:
        output_samples = 0
        try:
            self._check()
            if not self._source.seekable():
                raise MediaError("audio preparation requires a private seekable source")
            self._source.seek(0, io.SEEK_END)
            size = self._source.tell()
            self._source.seek(0)
            if not 0 < size <= self._limits.max_encoded_bytes:
                raise MediaLimitError("encoded audio exceeded its byte limit or is empty")
            options = {"protocol_whitelist": "pipe", "err_detect": "explode"}
            if self._format.media_type == "audio/pcm":
                assert self._format.channels is not None
                if size % (2 * self._format.channels):
                    raise MediaError("raw PCM does not contain complete interleaved samples")
                options.update(
                    {
                        "sample_rate": str(self._format.sample_rate_hz),
                        "ch_layout": f"{self._format.channels}c",
                    }
                )
            guarded = _BoundedSource(self._source, size=size, check=self._check)
            with self._av.open(
                guarded,
                mode="r",
                format=_FORMATS[self._format.media_type],
                options=options,
                io_open=self._deny_secondary,
            ) as container:
                if len(container.streams.audio) != 1 or len(container.streams) > 8:
                    raise MediaError("expected exactly one bounded audio stream")
                stream = container.streams.audio[0]
                stream.codec_context.thread_count = 1
                resampler = self._av.AudioResampler(
                    format="s16",
                    layout="mono",
                    rate=self._rate,
                    frame_size=self._rate // 10,
                    threads=1,
                )
                for packet in container.demux(stream):
                    self._check()
                    if packet.size > 8 * 1024 * 1024:
                        raise MediaLimitError("encoded audio exceeded the packet limit")
                    for frame in packet.decode():
                        self._check()
                        self._frame_clock(frame)
                        for converted in resampler.resample(frame):
                            for block in self._converted(converted, output_samples):
                                yield block
                                output_samples = block.sample_end
                for converted in resampler.resample(None):
                    for block in self._converted(converted, output_samples):
                        yield block
                        output_samples = block.sample_end
            self._check()
            if self.clock is None or not output_samples:
                raise MediaError("audio contains no decoded samples")
            self.summary = AudioDecodeSummary(
                self.clock, self.source_samples, self._rate, output_samples, size
            )
        except MediaError:
            raise
        except Exception:
            raise MediaError("cannot decode the declared audio format") from None
