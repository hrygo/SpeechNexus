"""The local media probe bounds bytes and decoded duration before any engine call."""

from __future__ import annotations

import io
import wave

import pytest

from speechnexus_backend.speech.application.transcription import (
    TranscriptionAudioTooLongError,
    TranscriptionAudioUnsupportedError,
    TranscriptionMediaTooLargeError,
)
from speechnexus_backend.speech.infrastructure.audio_duration_probe import PyAvAudioProbe


def wav(duration_ms: int = 1_500, rate: int = 16_000) -> bytes:
    frames = rate * duration_ms // 1000
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(rate)
        writer.writeframes(b"\x00\x00" * frames)
    return buffer.getvalue()


def test_real_wav_is_measured_from_the_container() -> None:
    facts = PyAvAudioProbe().inspect(wav(), max_audio_ms=5_000, max_upload_bytes=1_000_000)

    assert facts.media_type == "audio/wav"
    assert facts.codec == "pcm_s16le"
    assert facts.sample_rate_hz == 16_000
    assert facts.channels == 1
    assert 1_400 <= facts.duration_ms <= 1_600


def test_duration_over_the_ceiling_is_refused() -> None:
    with pytest.raises(TranscriptionAudioTooLongError):
        PyAvAudioProbe().inspect(wav(3_000), max_audio_ms=2_000, max_upload_bytes=1_000_000)


def test_bytes_over_the_ceiling_are_refused_before_decoding() -> None:
    with pytest.raises(TranscriptionMediaTooLargeError):
        PyAvAudioProbe().inspect(wav(), max_audio_ms=5_000, max_upload_bytes=1_024)


@pytest.mark.parametrize("payload", [b"not audio at all" * 100, b"RIFF\x00\x00\x00\x00WAVE"])
def test_unknown_bytes_are_an_unsupported_format(payload: bytes) -> None:
    with pytest.raises(TranscriptionAudioUnsupportedError):
        PyAvAudioProbe().inspect(payload, max_audio_ms=5_000, max_upload_bytes=1_000_000)
