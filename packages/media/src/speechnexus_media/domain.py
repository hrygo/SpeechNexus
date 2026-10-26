"""Native audio facts and exact sample clocks, independent of codecs and wire DTOs."""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass, field
from fractions import Fraction
from typing import BinaryIO, Protocol

MEDIA_RECIPE_VERSION = "pcm-decode-1"
_MEDIA_TYPES = frozenset({"audio/wav", "audio/flac", "audio/mpeg", "audio/ogg", "audio/pcm"})


class MediaError(ValueError):
    code = "invalid_media"


class MediaLimitError(MediaError):
    code = "media_limit_exceeded"


class MediaCancelledError(MediaError):
    code = "media_cancelled"


class MediaTimelineError(MediaError):
    code = "media_timeline_discontinuity"


@dataclass(frozen=True, slots=True)
class AudioSourceFormat:
    media_type: str
    sample_rate_hz: int | None = None
    channels: int | None = None

    def __post_init__(self) -> None:
        if type(self.media_type) is not str or self.media_type not in _MEDIA_TYPES:
            raise MediaError("unsupported audio media type")
        for value, minimum, maximum in (
            (self.sample_rate_hz, 8000, 192000),
            (self.channels, 1, 8),
        ):
            if value is not None and (type(value) is not int or not minimum <= value <= maximum):
                raise MediaError("unsupported source audio format")
        if self.media_type == "audio/pcm" and (
            self.sample_rate_hz is None or self.channels is None
        ):
            raise MediaError("raw PCM16LE requires a sample rate and channel count")


@dataclass(frozen=True, slots=True)
class AudioDecodeLimits:
    max_encoded_bytes: int = 256 * 1024 * 1024
    max_duration_ms: int = 3600000
    max_frame_samples: int = 65536
    timeout_seconds: float = 30.0

    def __post_init__(self) -> None:
        for value, minimum, maximum in (
            (self.max_encoded_bytes, 1, 1024 * 1024 * 1024),
            (self.max_duration_ms, 1, 86400000),
            (self.max_frame_samples, 1, 1048576),
        ):
            if type(value) is not int or not minimum <= value <= maximum:
                raise MediaError("invalid media resource limit")
        if (
            type(self.timeout_seconds) not in (int, float)
            or not math.isfinite(self.timeout_seconds)
            or not 0 < self.timeout_seconds <= 3600
        ):
            raise MediaError("invalid media deadline")


@dataclass(frozen=True, slots=True)
class SourceAudioClock:
    sample_rate_hz: int
    channels: int
    # None means the container did not provide an original timestamp.
    first_timestamp: Fraction | None

    def __post_init__(self) -> None:
        if (
            type(self.sample_rate_hz) is not int
            or not 8000 <= self.sample_rate_hz <= 192000
            or type(self.channels) is not int
            or not 1 <= self.channels <= 8
            or (self.first_timestamp is not None and not isinstance(self.first_timestamp, Fraction))
        ):
            raise MediaError("invalid source sample clock")

    def coverage(
        self, start: int, end: int, *, rate_hz: int, source_samples: int, final: bool = False
    ) -> tuple[int, int]:
        """Cover a target interval in decoded source samples, clipped at actual source EOF."""
        if (
            any(type(value) is not int for value in (start, end, rate_hz, source_samples))
            or not 0 <= start < end <= 2**53 - 1
            or rate_hz not in (16000, 24000)
            or not 0 < source_samples <= 2**53 - 1
        ):
            raise MediaError("invalid sample-clock interval")
        first = start * self.sample_rate_hz // rate_hz
        last = min(source_samples, (end * self.sample_rate_hz + rate_hz - 1) // rate_hz)
        if final:
            if abs(end * self.sample_rate_hz - source_samples * rate_hz) > self.sample_rate_hz:
                raise MediaTimelineError("final output exceeds the resampling rounding envelope")
            last = source_samples
        if first >= last:
            raise MediaTimelineError("output does not map to observed source samples")
        return first, last


@dataclass(frozen=True, slots=True)
class PcmBlock:
    sample_rate_hz: int
    sample_start: int
    pcm16: bytes = field(repr=False)

    def __post_init__(self) -> None:
        if (
            type(self.sample_rate_hz) is not int
            or self.sample_rate_hz not in (16000, 24000)
            or type(self.sample_start) is not int
            or self.sample_start < 0
            or type(self.pcm16) is not bytes
            or not 0 < len(self.pcm16) <= self.sample_rate_hz // 4 * 2
            or len(self.pcm16) % 2
            or self.sample_end > 2**53 - 1
        ):
            raise MediaError("invalid bounded mono PCM16LE block")

    @property
    def sample_end(self) -> int:
        return self.sample_start + len(self.pcm16) // 2


@dataclass(frozen=True, slots=True)
class AudioDecodeSummary:
    clock: SourceAudioClock
    source_samples: int
    output_sample_rate_hz: int
    output_samples: int
    encoded_bytes: int

    @property
    def duration_ms(self) -> int:
        return (
            self.source_samples * 1000 + self.clock.sample_rate_hz - 1
        ) // self.clock.sample_rate_hz


class AudioDecodeStream(Protocol):
    @property
    def clock(self) -> SourceAudioClock | None: ...

    @property
    def source_samples(self) -> int: ...

    @property
    def summary(self) -> AudioDecodeSummary | None: ...

    def __iter__(self) -> AudioDecodeStream: ...

    def __next__(self) -> PcmBlock: ...

    def close(self) -> None: ...


class AudioDecoder(Protocol):
    def open(
        self,
        source: BinaryIO,
        *,
        source_format: AudioSourceFormat,
        limits: AudioDecodeLimits,
        output_sample_rate_hz: int = 16000,
        cancelled: Callable[[], bool],
    ) -> AudioDecodeStream: ...
