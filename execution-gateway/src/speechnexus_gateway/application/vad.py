"""One ordered, bounded CPU VAD session; no network, database, or ASR side effects."""

from __future__ import annotations

from contextlib import suppress

from speechnexus_gateway.domain.vad import (
    VAD_FRAME_BYTES,
    VadError,
    VadInferenceError,
    VadModel,
    VadPolicy,
    VadSegment,
    VadSegmenter,
)


class CpuVadSession:
    """The caller owns admission/epoch serialization and must resample to 16 kHz first.

    Chunk retries/old epochs must be rejected or deduplicated before this boundary.
    ASR may only consume returned segments whose ``has_speech`` is true.
    """

    def __init__(self, *, model: VadModel, policy: VadPolicy) -> None:
        self.model_sha256 = model.model_sha256
        self.policy = policy
        self._detector = model.new_detector()
        self._segments = VadSegmenter(policy)
        self._pending = bytearray()
        self._accepted_samples = 0
        self._closed = False

    @property
    def accepted_samples(self) -> int:
        return self._accepted_samples

    @property
    def buffered_bytes(self) -> int:
        return self._segments.buffered_bytes + len(self._pending)

    @property
    def buffer_limit_bytes(self) -> int:
        return (self.policy.max_samples + self.policy.prefix_samples) * 2 + VAD_FRAME_BYTES * 2

    def push(self, pcm16: bytes, *, sample_start: int) -> tuple[VadSegment, ...]:
        self._require_open()
        if (
            type(sample_start) is not int
            or sample_start != self._accepted_samples
            or type(pcm16) is not bytes
            or not 0 < len(pcm16) <= 16000 * 2 // 4
            or len(pcm16) % 2
            or self._accepted_samples + len(pcm16) // 2 > (2**53 - 1) * 2 // 3
        ):
            raise VadError("expected ordered PCM16LE mono, at most 250 ms at 16 kHz")
        self._accepted_samples += len(pcm16) // 2
        emitted: list[VadSegment] = []
        offset = 0
        while offset < len(pcm16):
            take = min(VAD_FRAME_BYTES - len(self._pending), len(pcm16) - offset)
            self._pending.extend(pcm16[offset : offset + take])
            offset += take
            if len(self._pending) == VAD_FRAME_BYTES:
                frame = bytes(self._pending)
                self._pending.clear()
                emitted.extend(self._score(frame, actual=frame))
        return tuple(emitted)

    def commit(self) -> tuple[VadSegment, ...]:
        self._require_open()
        emitted: list[VadSegment] = []
        if self._pending:
            actual = bytes(self._pending)
            frame = actual + b"\x00" * (VAD_FRAME_BYTES - len(actual))
            self._pending.clear()
            emitted.extend(self._score(frame, actual=actual))
        # A tail can itself close a VAD/max segment; no duplicate empty result follows.
        if self._segments.has_uncommitted_audio or not emitted:
            emitted.extend(self._segments.commit())
        self._reset_detector()
        return tuple(emitted)

    def clear(self) -> None:
        self._require_open()
        self._pending.clear()
        self._segments.clear(position=self._accepted_samples)
        self._reset_detector()

    def close(self) -> None:
        """Discard pending audio. Drain callers must commit before closing."""
        self._closed = True
        self._pending.clear()
        self._segments.clear(position=self._accepted_samples)
        self._reset_detector()

    def _score(self, frame: bytes, *, actual: bytes) -> tuple[VadSegment, ...]:
        try:
            score = self._detector.probability(frame)
            return self._segments.push(actual, score)
        except Exception:
            self._closed = True
            self._pending.clear()
            self._segments.clear(position=self._accepted_samples)
            with suppress(Exception):
                self._detector.reset()
            raise VadInferenceError("CPU VAD failed; session state cannot be continued") from None

    def _reset_detector(self) -> None:
        try:
            self._detector.reset()
        except Exception:
            self._closed = True
            raise VadInferenceError(
                "CPU VAD reset failed; session state cannot be continued"
            ) from None

    def _require_open(self) -> None:
        if self._closed:
            raise VadError("VAD session is closed; use a new stream epoch")
