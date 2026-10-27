"""Bounded local media probe for the synchronous transcription route.

The probe decodes container metadata, never network or filesystem paths, and
stops as soon as the effective duration ceiling is crossed. It is the only place
that names accepted containers and codecs; unknown bytes are an explicit
unsupported-format failure instead of an engine call.
"""

from __future__ import annotations

import io
from typing import Any

import av

from speechnexus_backend.speech.application.transcription import (
    TranscriptionAudioTooLongError,
    TranscriptionAudioUnsupportedError,
    TranscriptionMediaTooLargeError,
)
from speechnexus_backend.speech.domain.transcription import AudioFacts

# Declared input containers. PyAV reports comma-separated aliases; every alias
# below is accepted because the same demuxer serves it.
ACCEPTED_CONTAINERS = frozenset(
    {
        "wav",
        "mp3",
        "flac",
        "ogg",
        "opus",
        "mp4",
        "m4a",
        "mov",
        "webm",
        "matroska",
        "aac",
        "aiff",
        "aif",
    }
)
ACCEPTED_CODECS = frozenset(
    {
        "pcm_s16le",
        "pcm_s24le",
        "pcm_s32le",
        "pcm_f32le",
        "pcm_u8",
        "pcm_alaw",
        "pcm_mulaw",
        "mp3",
        "mp3float",
        "aac",
        "flac",
        "opus",
        "vorbis",
        "alac",
    }
)
_MEDIA_TYPES = {
    "wav": "audio/wav",
    "mp3": "audio/mpeg",
    "flac": "audio/flac",
    "ogg": "audio/ogg",
    "opus": "audio/ogg",
    "mp4": "audio/mp4",
    "m4a": "audio/mp4",
    "mov": "audio/mp4",
    "webm": "audio/webm",
    "matroska": "audio/webm",
    "aac": "audio/aac",
    "aiff": "audio/aiff",
    "aif": "audio/aiff",
}


class PyAvAudioProbe:
    """One bounded inspect of uploaded bytes; no engine call and no stored fact."""

    def inspect(self, audio: bytes, *, max_audio_ms: int, max_upload_bytes: int) -> AudioFacts:
        if type(audio) is not bytes or not audio:
            raise TranscriptionAudioUnsupportedError
        if len(audio) > max_upload_bytes:
            raise TranscriptionMediaTooLargeError
        try:
            with av.open(io.BytesIO(audio), mode="r") as container:
                container_name = _accepted_container(container)
                stream = _audio_stream(container)
                media_type = _MEDIA_TYPES[container_name]
                codec = stream.codec_context.name
                if codec not in ACCEPTED_CODECS:
                    raise TranscriptionAudioUnsupportedError
                rate = int(stream.codec_context.sample_rate or 0)
                channels = int(stream.codec_context.channels or 0)
                duration_ms = _duration_ms(container, stream, max_audio_ms)
        except TranscriptionAudioTooLongError, TranscriptionAudioUnsupportedError:
            raise
        except av.error.FFmpegError, ValueError, OSError, IndexError, OverflowError:
            # A vendor message, path or demuxer detail never reaches the client.
            raise TranscriptionAudioUnsupportedError from None
        if not rate or channels <= 0:
            raise TranscriptionAudioUnsupportedError
        return AudioFacts(
            media_type=media_type,
            codec=codec,
            duration_ms=duration_ms,
            sample_rate_hz=rate,
            channels=channels,
        )


def _accepted_container(container: Any) -> str:
    names = {
        str(name).strip().lower()
        for name in str(getattr(container.format, "name", "")).split(",")
        if str(name).strip()
    }
    for name in names:
        if name in ACCEPTED_CONTAINERS:
            return name
    raise TranscriptionAudioUnsupportedError


def _audio_stream(container: Any) -> Any:
    streams = [stream for stream in container.streams if stream.type == "audio"]
    if len(streams) != 1:
        # Ambiguous multi-track uploads are not silently mixed or picked.
        raise TranscriptionAudioUnsupportedError
    return streams[0]


def _duration_ms(container: Any, stream: Any, max_audio_ms: int) -> int:
    declared = _declared_duration_ms(container, stream)
    if declared is not None:
        if declared <= 0 or declared > max_audio_ms:
            raise TranscriptionAudioTooLongError
        return declared
    return _decoded_duration_ms(container, stream, max_audio_ms)


def _declared_duration_ms(container: Any, stream: Any) -> int | None:
    if stream.duration is not None and stream.time_base is not None:
        seconds = float(stream.duration * stream.time_base)
        return _seconds_to_ms(seconds)
    if container.duration is not None:
        return _seconds_to_ms(float(container.duration) / float(av.time_base))
    return None


def _decoded_duration_ms(container: Any, stream: Any, max_audio_ms: int) -> int:
    rate = int(stream.codec_context.sample_rate or 0)
    if rate <= 0:
        raise TranscriptionAudioUnsupportedError
    samples = 0
    for frame in container.decode(stream):
        samples += int(frame.samples)
        if samples * 1000 > max_audio_ms * rate:
            raise TranscriptionAudioTooLongError
    if samples <= 0:
        raise TranscriptionAudioUnsupportedError
    return max(1, -((-samples * 1000) // rate))


def _seconds_to_ms(seconds: float) -> int:
    if seconds != seconds or seconds in (float("inf"), float("-inf")):
        raise TranscriptionAudioUnsupportedError
    return max(0, round(seconds * 1000))


__all__ = ["ACCEPTED_CODECS", "ACCEPTED_CONTAINERS", "PyAvAudioProbe"]
