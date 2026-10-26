"""Private inspection command for the supervisor's fixed, read-only /input/media.

This reports native facts after exhausting the decoder. It neither publishes an
Asset nor interprets a client MIME/path as authority. The deployment must supply
the allowlisted format and any raw PCM metadata through its fixed command.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import time
import wave
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import BinaryIO

from speechnexus_media.decoder import PyAvAudioDecoder
from speechnexus_media.domain import (
    MEDIA_RECIPE_VERSION,
    AudioDecodeLimits,
    AudioSourceFormat,
    MediaCancelledError,
    MediaError,
    MediaLimitError,
)

_INPUT = Path("/input/media")


def _wav_source_facts(
    source: BinaryIO, encoded_bytes: int, *, check_budget: Callable[[], None]
) -> tuple[int, int, int]:
    check_budget()
    source.seek(0)
    header = source.read(12)
    if (
        len(header) != 12
        or header[:4] != b"RIFF"
        or header[8:] != b"WAVE"
        or int.from_bytes(header[4:8], "little") + 8 != encoded_bytes
    ):
        raise MediaError("incomplete or unsupported WAV container")
    offset = 12
    has_format = False
    data_bytes: int | None = None
    while offset < encoded_bytes:
        check_budget()
        source.seek(offset)
        chunk = source.read(8)
        if len(chunk) != 8:
            raise MediaError("incomplete WAV chunk header")
        size = int.from_bytes(chunk[4:], "little")
        next_offset = offset + 8 + size + (size % 2)
        if next_offset > encoded_bytes:
            raise MediaError("incomplete WAV chunk body or padding")
        if chunk[:4] == b"fmt ":
            if has_format or data_bytes is not None:
                raise MediaError("unsupported WAV format organization")
            has_format = True
        elif chunk[:4] == b"data":
            if not has_format or data_bytes is not None:
                raise MediaError("unsupported WAV data organization")
            data_bytes = size
        offset = next_offset
    if not has_format or data_bytes is None:
        raise MediaError("missing WAV format or audio data")
    check_budget()
    source.seek(0)
    try:
        with wave.open(source, "rb") as metadata:
            if data_bytes % (metadata.getnchannels() * metadata.getsampwidth()):
                raise MediaError("incomplete PCM WAV frame")
            return metadata.getframerate(), metadata.getnchannels(), metadata.getnframes()
    except wave.Error, EOFError:
        raise MediaError("unsupported PCM WAV metadata") from None


def inspect_audio(
    source: BinaryIO,
    *,
    source_format: AudioSourceFormat,
    limits: AudioDecodeLimits,
    cancelled: Callable[[], bool],
    monotonic: Callable[[], float] = time.monotonic,
) -> dict[str, str | int]:
    """Inspect an immutable seekable input; never retain or return decoded PCM.

    The outer process supervisor supplies hard resource/stop boundaries. Codec
    checks and this deadline are cooperative, not a physical termination proof.
    """
    # This initial inspection profile admits PCM WAV and explicit PCM only.
    # The shared B decoder keeps its broader formats unchanged, but codec EOF
    # alone cannot establish full-container integrity for public audio ready.
    if source_format.media_type not in ("audio/wav", "audio/pcm"):
        raise MediaError("audio format is not qualified for private inspection")
    deadline = monotonic() + limits.timeout_seconds

    def check_budget() -> None:
        if cancelled():
            raise MediaCancelledError("audio inspection was cancelled")
        if monotonic() >= deadline:
            raise MediaLimitError("audio inspection exceeded its deadline")

    source.seek(0)
    digest = hashlib.sha256()
    encoded_bytes = 0
    while True:
        check_budget()
        chunk = source.read(65536)
        if type(chunk) is not bytes:
            raise MediaError("invalid binary audio source")
        if not chunk:
            break
        encoded_bytes += len(chunk)
        if encoded_bytes > limits.max_encoded_bytes:
            raise MediaLimitError("encoded audio exceeded its byte limit")
        digest.update(chunk)
    expected = (
        _wav_source_facts(source, encoded_bytes, check_budget=check_budget)
        if source_format.media_type == "audio/wav"
        else None
    )
    remaining = deadline - monotonic()
    if remaining <= 0:
        raise MediaLimitError("audio inspection exceeded its deadline")
    stream = PyAvAudioDecoder(monotonic=monotonic).open(
        source,
        source_format=source_format,
        limits=replace(limits, timeout_seconds=min(remaining, limits.timeout_seconds)),
        cancelled=cancelled,
    )
    try:
        for _block in stream:
            pass
        summary = stream.summary
        if summary is None or summary.encoded_bytes != encoded_bytes:
            raise MediaError("inspection did not produce complete source facts")
        if expected is not None and expected != (
            summary.clock.sample_rate_hz,
            summary.clock.channels,
            summary.source_samples,
        ):
            raise MediaError("decoded samples do not cover the declared WAV source")
        return {
            "schema_version": 1,
            "status": "complete",
            "recipe_version": MEDIA_RECIPE_VERSION,
            "sha256": "sha256:" + digest.hexdigest(),
            "encoded_bytes": encoded_bytes,
            "media_type": source_format.media_type,
            "sample_rate_hz": summary.clock.sample_rate_hz,
            "channels": summary.clock.channels,
            "source_samples": summary.source_samples,
            "duration_ms": summary.duration_ms,
        }
    finally:
        stream.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Inspect the supervisor's private audio input")
    parser.add_argument(
        "--media-type",
        required=True,
        choices=("audio/wav", "audio/pcm"),
    )
    parser.add_argument("--sample-rate-hz", type=int)
    parser.add_argument("--channels", type=int)
    parser.add_argument("--max-encoded-bytes", type=int, required=True)
    parser.add_argument("--max-duration-ms", type=int, required=True)
    parser.add_argument("--timeout-seconds", type=float, required=True)
    args = parser.parse_args(argv)
    try:
        # Only this standalone worker controls these settings, before loading
        # NumPy/codec dependencies. The shared decoder never changes a service's
        # thread environment. The sandbox itself continues to deny new threads.
        os.environ["OPENBLAS_NUM_THREADS"] = "1"
        import av

        av.logging.set_level(None)
        source_format = AudioSourceFormat(args.media_type, args.sample_rate_hz, args.channels)
        limits = AudioDecodeLimits(
            max_encoded_bytes=args.max_encoded_bytes,
            max_duration_ms=args.max_duration_ms,
            timeout_seconds=args.timeout_seconds,
        )
        with _INPUT.open("rb") as source:
            report = inspect_audio(
                source, source_format=source_format, limits=limits, cancelled=lambda: False
            )
    except MediaError as error:
        # No vendor messages, private paths or input bytes enter the receipt.
        print(json.dumps({"schema_version": 1, "status": error.code}))
        return 1
    except Exception:
        print(json.dumps({"schema_version": 1, "status": "unavailable"}))
        return 2
    print(json.dumps(report, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
