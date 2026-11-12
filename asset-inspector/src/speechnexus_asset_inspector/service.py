"""Bounded inspection slots over one decoder child; no queue and no side effects."""

from __future__ import annotations

import hashlib
import io
import json
import tempfile
import wave
from collections.abc import AsyncIterable
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Literal

from speechnexus_asset_inspector.protocol import RECIPE_VERSION, SCHEMA_VERSION, WAV_MEDIA_TYPE
from speechnexus_asset_inspector.runner import InspectionRunner
from speechnexus_asset_inspector.settings import InspectionSettings

type InspectionStatus = Literal[
    "complete",
    "invalid_media",
    "media_limit_exceeded",
    "busy",
    "unavailable",
    "deadline_exceeded",
]

_SELF_CHECK_RATE_HZ = 16_000
_SELF_CHECK_SAMPLES = 80  # 5 ms
_SELF_CHECK_DURATION_MS = 5


class BodyProtocolError(Exception):
    """The received body did not match its declared Content-Length."""


@dataclass(frozen=True, slots=True)
class InspectionOutcome:
    status: InspectionStatus
    receipt: bytes = b""

    def __post_init__(self) -> None:
        if self.status != "complete" and self.receipt:
            raise ValueError("A failed inspection cannot carry a receipt")


def self_check_wav() -> bytes:
    """A deterministic 5 ms PCM WAV generated in-process, never a client input."""
    output = io.BytesIO()
    with wave.open(output, "wb") as encoded:
        encoded.setnchannels(1)
        encoded.setsampwidth(2)
        encoded.setframerate(_SELF_CHECK_RATE_HZ)
        encoded.writeframes(b"\x00\x00" * _SELF_CHECK_SAMPLES)
    return output.getvalue()


class InspectorService:
    """Hold at most ``concurrency`` decode slots and release each after child death."""

    def __init__(
        self,
        settings: InspectionSettings,
        *,
        runtime_root: Path,
        runner: InspectionRunner,
    ) -> None:
        root = runtime_root.resolve()
        if not root.is_dir() or root == Path("/"):
            raise ValueError("Inspection runtime root is not a private directory")
        self._settings = settings
        self._runtime_root = root
        self._runner = runner
        self._in_flight = 0
        self._ready = False

    @property
    def ready(self) -> bool:
        return self._ready

    @property
    def in_flight(self) -> int:
        return self._in_flight

    @property
    def settings(self) -> InspectionSettings:
        return self._settings

    def mark_ready(self) -> None:
        self._ready = True

    def _acquire(self) -> bool:
        # The event loop is single-threaded, so check and increment is atomic as
        # long as no await intervenes. There is deliberately no queue.
        if self._in_flight >= self._settings.concurrency:
            return False
        self._in_flight += 1
        return True

    def _release(self) -> None:
        self._in_flight -= 1

    async def inspect(
        self,
        stream: AsyncIterable[bytes],
        *,
        declared_bytes: int,
        deadline_ms: int,
        max_duration_ms: int,
    ) -> InspectionOutcome:
        # A starting service is temporarily at capacity: report the retryable
        # busy verdict, never a terminal unavailable or a real decode attempt.
        if not self._ready:
            await self._discard(stream, declared_bytes)
            return InspectionOutcome("busy")
        # Decide capacity before accepting the body: a slow upload must not
        # bypass the slot bound, and a full service rejects without queueing.
        if not self._acquire():
            await self._discard(stream, declared_bytes)
            return InspectionOutcome("busy")
        try:
            with tempfile.TemporaryFile(dir=self._runtime_root) as body:
                await self._receive(stream, body, declared_bytes)
                body.flush()
                body.seek(0)
                wall_seconds = max(0.001, min(deadline_ms / 1000, self._settings.max_wall_seconds))
                duration_ms = max(1, min(max_duration_ms, self._settings.max_duration_ms))
                outcome = await self._runner.run(
                    input_fd=body.fileno(),
                    max_encoded_bytes=self._settings.max_encoded_bytes,
                    max_duration_ms=duration_ms,
                    wall_seconds=wall_seconds,
                )
        finally:
            self._release()
        if outcome.status == "complete":
            if not 0 < len(outcome.output) <= self._settings.max_receipt_bytes:
                return InspectionOutcome("unavailable")
            return InspectionOutcome("complete", outcome.output)
        if outcome.status == "deadline":
            return InspectionOutcome("deadline_exceeded")
        if outcome.status in ("invalid_media", "media_limit_exceeded", "unavailable"):
            return InspectionOutcome(outcome.status)
        return InspectionOutcome("unavailable")

    async def _receive(
        self, stream: AsyncIterable[bytes], body: BinaryIO, declared_bytes: int
    ) -> None:
        received = 0
        async for chunk in stream:
            if type(chunk) is not bytes:
                raise BodyProtocolError
            received += len(chunk)
            if received > declared_bytes:
                raise BodyProtocolError
            body.write(chunk)
        if received != declared_bytes:
            raise BodyProtocolError

    async def _discard(self, stream: AsyncIterable[bytes], declared_bytes: int) -> None:
        received = 0
        async for chunk in stream:
            if type(chunk) is not bytes:
                raise BodyProtocolError
            received += len(chunk)
            if received > declared_bytes:
                raise BodyProtocolError
        if received != declared_bytes:
            raise BodyProtocolError

    async def self_check(self) -> bool:
        """Decode the built-in 5 ms WAV through the real child and verify the receipt."""
        content = self_check_wav()
        with tempfile.TemporaryFile(dir=self._runtime_root) as body:
            body.write(content)
            body.flush()
            body.seek(0)
            if not self._acquire():
                return False
            try:
                outcome = await self._runner.run(
                    input_fd=body.fileno(),
                    max_encoded_bytes=self._settings.max_encoded_bytes,
                    max_duration_ms=self._settings.max_duration_ms,
                    wall_seconds=min(self._settings.max_wall_seconds, 10.0),
                )
            finally:
                self._release()
        if outcome.status != "complete" or not outcome.output:
            return False
        try:
            receipt: object = json.loads(outcome.output)
        except UnicodeDecodeError, ValueError:
            return False
        return receipt == {
            "schema_version": SCHEMA_VERSION,
            "status": "complete",
            "recipe_version": RECIPE_VERSION,
            "sha256": "sha256:" + hashlib.sha256(content).hexdigest(),
            "encoded_bytes": len(content),
            "media_type": WAV_MEDIA_TYPE,
            "sample_rate_hz": _SELF_CHECK_RATE_HZ,
            "channels": 1,
            "source_samples": _SELF_CHECK_SAMPLES,
            "duration_ms": _SELF_CHECK_DURATION_MS,
        }
