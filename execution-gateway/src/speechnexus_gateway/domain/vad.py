"""Versioned, bounded acoustic segmentation in the model's 16 kHz sample clock."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol

from speechnexus_gateway.domain.canonical import command_digest

VAD_SAMPLE_RATE_HZ = 16000
VAD_FRAME_SAMPLES = 512
VAD_FRAME_BYTES = VAD_FRAME_SAMPLES * 2
VAD_RECIPE_VERSION = "cpu-vad-1"


class VadError(ValueError):
    """An invalid local VAD operation; messages never include audio or model paths."""


class EmptyVadBufferError(VadError):
    pass


class VadInferenceError(RuntimeError):
    pass


class VadDetector(Protocol):
    """One mutable recurrent state per session; input is exactly one PCM16LE frame."""

    def probability(self, pcm16: bytes) -> float: ...

    def reset(self) -> None: ...


class VadModel(Protocol):
    """The immutable model may be shared, but every call creates isolated state."""

    @property
    def model_sha256(self) -> str: ...

    def new_detector(self) -> VadDetector: ...


class VadCloseReason(StrEnum):
    VAD = "vad"
    CLIENT_COMMIT = "client_commit"
    BUDGET_ROLLOVER = "budget_rollover"


@dataclass(frozen=True, slots=True)
class VadPolicy:
    """Resolved policy, frozen for a session; this is not a public/vendor DTO."""

    config_revision: str
    threshold: float = 0.5
    end_threshold: float = 0.35
    min_speech_ms: int = 96
    prefix_padding_ms: int = 300
    silence_duration_ms: int = 500
    max_segment_ms: int = 20000

    def __post_init__(self) -> None:
        if not isinstance(self.config_revision, str) or not self.config_revision:
            raise VadError("a frozen configuration revision is required")
        for value in (self.threshold, self.end_threshold):
            if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 1:
                raise VadError("invalid VAD threshold")
        if self.end_threshold > self.threshold:
            raise VadError("the end threshold exceeds the speech threshold")
        for value, minimum, maximum in (
            (self.min_speech_ms, 32, 500),
            (self.prefix_padding_ms, 0, 3000),
            (self.silence_duration_ms, 200, 3000),
            (self.max_segment_ms, 1000, 30000),
        ):
            if type(value) is not int or not minimum <= value <= maximum:
                raise VadError("invalid VAD duration")

    @property
    def max_samples(self) -> int:
        return self.max_segment_ms * 16

    @property
    def min_speech_samples(self) -> int:
        return math.ceil(self.min_speech_ms * 16 / VAD_FRAME_SAMPLES) * VAD_FRAME_SAMPLES

    @property
    def prefix_samples(self) -> int:
        # Prefix is a maximum look-back, bounded by the segment's hard ceiling.
        return min(self.prefix_padding_ms * 16, self.max_samples - self.min_speech_samples)

    @property
    def digest(self) -> str:
        return command_digest(
            {
                "recipe": VAD_RECIPE_VERSION,
                "config_revision": self.config_revision,
                "threshold": self.threshold,
                "end_threshold": self.end_threshold,
                "min_speech_ms": self.min_speech_ms,
                "prefix_padding_ms": self.prefix_padding_ms,
                "silence_duration_ms": self.silence_duration_ms,
                "max_segment_ms": self.max_segment_ms,
            }
        )


@dataclass(frozen=True, slots=True)
class VadSegment:
    sequence: int
    sample_start: int
    sample_end: int
    reason: VadCloseReason
    config_revision: str
    policy_digest: str
    pcm16: bytes = field(repr=False)

    @property
    def has_speech(self) -> bool:
        return bool(self.pcm16)

    @property
    def wire_sample_span(self) -> tuple[int, int]:
        """24 kHz coverage: floor(start * 3/2), ceil(end * 3/2), never accumulated."""
        return self.sample_start * 3 // 2, (self.sample_end * 3 + 1) // 2


class VadSegmenter:
    """Consumes ordered scored frames. It neither runs a model nor dispatches ASR."""

    def __init__(self, policy: VadPolicy) -> None:
        self.policy = policy
        self.position = 0
        self._uncommitted_start = 0
        self._sequence = 0
        self._prefix = bytearray()
        self._candidate = bytearray()
        self._active = bytearray()
        self._active_has_speech = False
        self._active_start = 0
        self._silence_samples = 0
        self._continuing = False

    @property
    def buffered_bytes(self) -> int:
        return len(self._prefix) + len(self._candidate) + len(self._active)

    @property
    def has_uncommitted_audio(self) -> bool:
        return self.position > self._uncommitted_start

    def push(self, pcm16: bytes, probability: float) -> tuple[VadSegment, ...]:
        if (
            type(pcm16) is not bytes
            or not 0 < len(pcm16) <= VAD_FRAME_BYTES
            or len(pcm16) % 2
            or type(probability) not in (int, float)
            or not math.isfinite(probability)
            or not 0 <= probability <= 1
        ):
            raise VadError("invalid scored PCM frame")
        emitted: list[VadSegment] = []
        if self._active:
            self._append_active(pcm16, probability, emitted)
        elif self._continuing:
            self._active_start = self.position
            self._append_active(pcm16, probability, emitted)
        else:
            self._continuing = False
            self.position += len(pcm16) // 2
            if probability >= self.policy.threshold:
                self._candidate.extend(pcm16)
                if len(self._candidate) // 2 >= self.policy.min_speech_samples:
                    prefix = bytes(self._prefix)
                    audio = prefix + bytes(self._candidate)
                    self._active_start = self.position - len(audio) // 2
                    self._prefix.clear()
                    self._candidate.clear()
                    # Reuse one bounded append path even at an unusual prefix/max combination.
                    self.position = self._active_start
                    self._append_active(audio, probability, emitted)
            else:
                self._prefix.extend(self._candidate)
                self._prefix.extend(pcm16)
                self._candidate.clear()
                self._trim_prefix()
            if (
                not self._active
                and self.position - self._uncommitted_start >= self.policy.max_samples
            ):
                # A pending onset crosses the idle window intact. Do not label its frames
                # as silence or discard its debounce history.
                end = (
                    self.position - len(self._candidate) // 2
                    if self._candidate
                    else self._uncommitted_start + self.policy.max_samples
                )
                self._emit_silence_through(end, VadCloseReason.BUDGET_ROLLOVER, emitted)
                keep = max(0, self.position - end - len(self._candidate) // 2) * 2
                if len(self._prefix) > keep:
                    del self._prefix[: len(self._prefix) - keep]
        return tuple(emitted)

    def commit(self) -> tuple[VadSegment, ...]:
        emitted: list[VadSegment] = []
        if self._active:
            self._close_active(VadCloseReason.CLIENT_COMMIT, emitted)
        elif self.position > self._uncommitted_start:
            self._emit_silence_through(self.position, VadCloseReason.CLIENT_COMMIT, emitted)
        else:
            raise EmptyVadBufferError("no uncommitted audio")
        self._drop_buffers()
        return tuple(emitted)

    def clear(self, *, position: int) -> None:
        """Discard private uncommitted audio without rewinding the input clock or sequence."""
        self.position = position
        self._uncommitted_start = position
        self._drop_buffers()

    def _trim_prefix(self) -> None:
        keep = self.policy.prefix_samples * 2
        if len(self._prefix) > keep:
            del self._prefix[: len(self._prefix) - keep]

    def _append_active(self, pcm16: bytes, probability: float, emitted: list[VadSegment]) -> None:
        remaining = pcm16
        while remaining:
            capacity = self.policy.max_samples * 2 - len(self._active)
            piece, remaining = remaining[:capacity], remaining[capacity:]
            self._active.extend(piece)
            self._active_has_speech |= probability >= self.policy.end_threshold
            samples = len(piece) // 2
            self.position += samples
            if probability < self.policy.end_threshold:
                self._silence_samples += samples
            else:
                self._silence_samples = 0
            if len(self._active) // 2 == self.policy.max_samples:
                self._close_active(VadCloseReason.BUDGET_ROLLOVER, emitted, preserve_silence=True)
                self._continuing = self._silence_samples < self.policy.silence_duration_ms * 16
                self._active_start = self.position
                if remaining and not self._continuing:
                    self._prefix.extend(remaining)
                    self.position += len(remaining) // 2
                    self._trim_prefix()
                    break
            elif self._silence_samples >= self.policy.silence_duration_ms * 16:
                self._close_active(VadCloseReason.VAD, emitted)
                # Endpoint silence is already inside the closed segment, not next prefix.
                self._drop_buffers()

    def _close_active(
        self, reason: VadCloseReason, emitted: list[VadSegment], *, preserve_silence: bool = False
    ) -> None:
        pcm16 = bytes(self._active) if self._active_has_speech else b""
        self._emit(self._active_start, self.position, reason, pcm16, emitted)
        self._active.clear()
        self._active_has_speech = False
        if not preserve_silence:
            self._silence_samples = 0

    def _emit_silence_through(
        self, end: int, reason: VadCloseReason, emitted: list[VadSegment]
    ) -> None:
        while self._uncommitted_start < end:
            next_end = min(end, self._uncommitted_start + self.policy.max_samples)
            self._emit(self._uncommitted_start, next_end, reason, b"", emitted)

    def _emit(
        self,
        start: int,
        end: int,
        reason: VadCloseReason,
        pcm16: bytes,
        emitted: list[VadSegment],
    ) -> None:
        self._sequence += 1
        emitted.append(
            VadSegment(
                self._sequence,
                start,
                end,
                reason,
                self.policy.config_revision,
                self.policy.digest,
                pcm16,
            )
        )
        self._uncommitted_start = end

    def _drop_buffers(self) -> None:
        self._prefix.clear()
        self._candidate.clear()
        self._active.clear()
        self._active_has_speech = False
        self._silence_samples = 0
        self._continuing = False
