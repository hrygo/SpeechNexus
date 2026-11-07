"""Frozen Base clone facts: reference acceptance, pairing and explicit modes."""

import io
import wave

import pytest

from speechnexus_gateway.domain.voice_clone import (
    MAX_REFERENCE_AUDIO_BYTES,
    ReferenceAudio,
    ReferenceTextMode,
    VoiceClonePolicy,
    VoiceCloneRequest,
    reference_text_fits_duration,
)
from tests.fake.file_voice_clone_configuration import reference_wav

REFERENCE_TEXT = "Synthetic warmup reference."


def wav_bytes(pcm: bytes, *, rate: int = 16000, channels: int = 1, width: int = 2) -> bytes:
    output = io.BytesIO()
    with wave.open(output, "wb") as writer:
        writer.setnchannels(channels)
        writer.setsampwidth(width)
        writer.setframerate(rate)
        writer.writeframes(pcm)
    return output.getvalue()


def test_mono_16_bit_reference_audio_at_an_approved_rate_is_accepted() -> None:
    for rate in (16000, 24000):
        audio = ReferenceAudio.from_wav(reference_wav(rate=rate))
        assert audio.sample_rate_hz == rate
        assert audio.duration_ms == 1500
        assert audio.wav()[:4] == b"RIFF"
        assert audio.wav()[8:12] == b"WAVE"


@pytest.mark.parametrize("rate", [8000, 44100, 48000])
def test_reference_audio_at_an_unverified_rate_is_refused(rate: int) -> None:
    with pytest.raises(ValueError):
        ReferenceAudio.from_wav(reference_wav(rate=rate))


def test_stereo_and_eight_bit_reference_audio_are_refused() -> None:
    with pytest.raises(ValueError):
        ReferenceAudio.from_wav(wav_bytes(b"\x00\x00" * 32000, channels=2))
    with pytest.raises(ValueError):
        ReferenceAudio.from_wav(wav_bytes(b"\x00" * 32000, width=1))


def test_non_wav_trailing_bytes_and_oversize_reference_are_refused() -> None:
    with pytest.raises(ValueError):
        ReferenceAudio.from_wav(b"not a wave file" * 8)
    valid = reference_wav()
    with pytest.raises(ValueError):
        ReferenceAudio.from_wav(valid + b"\x00")
    with pytest.raises(ValueError):
        ReferenceAudio.from_wav(valid, max_bytes=1024)
    big = reference_wav(duration_ms=61000)
    with pytest.raises(ValueError):
        ReferenceAudio.from_wav(big, max_bytes=MAX_REFERENCE_AUDIO_BYTES)
    with pytest.raises(ValueError):
        ReferenceAudio.from_wav(reference_wav(duration_ms=500), min_duration_ms=1000)


def test_a_transcript_no_speaker_could_utter_in_that_time_is_refused() -> None:
    audio = ReferenceAudio.from_wav(reference_wav())
    assert reference_text_fits_duration("x" * 30, audio)
    assert not reference_text_fits_duration("x" * 31, audio)
    with pytest.raises(ValueError):
        VoiceCloneRequest("intent-voice-clone", "Ready.", "en", audio, "x" * 31)


def test_reference_text_presence_selects_the_mode() -> None:
    audio = ReferenceAudio.from_wav(reference_wav())
    icl = VoiceCloneRequest("intent-voice-clone", "Ready.", "en", audio, REFERENCE_TEXT)
    x_vector = VoiceCloneRequest("intent-voice-clone", "Ready.", "en", audio, None)

    assert icl.mode is ReferenceTextMode.ICL
    assert x_vector.mode is ReferenceTextMode.X_VECTOR_ONLY


def test_request_validation_refuses_empty_text_and_invalid_documents() -> None:
    audio = ReferenceAudio.from_wav(reference_wav())
    with pytest.raises(ValueError):
        VoiceCloneRequest("intent-voice-clone", "Ready.", "en", audio, "")
    with pytest.raises(ValueError):
        VoiceCloneRequest("intent-voice-clone", "", "en", audio, REFERENCE_TEXT)
    with pytest.raises(ValueError):
        VoiceCloneRequest("intent-voice-clone", "Ready.", "en", audio, "\x00")


def test_repr_never_carries_audio_content_or_the_transcript() -> None:
    audio = ReferenceAudio.from_wav(reference_wav())
    request = VoiceCloneRequest("intent-voice-clone", "Ready.", "en", audio, REFERENCE_TEXT)

    for text in (repr(audio), repr(request)):
        assert "Synthetic warmup reference" not in text
        assert "RIFF" not in text
        assert "AAAA" not in text


def test_x_vector_only_is_an_explicit_policy_decision() -> None:
    assert VoiceClonePolicy("policy-voice-clone").allow_x_vector_only is False
    opted_in = VoiceClonePolicy("policy-voice-clone", allow_x_vector_only=True)
    assert opted_in.allow_x_vector_only is True
    assert opted_in.digest != VoiceClonePolicy("policy-voice-clone").digest


@pytest.mark.parametrize(
    "changes",
    [
        {"sample_rate_hz": 16000},
        {"reference_sample_rates": (8000,)},
        {"reference_sample_rates": (24000, 16000)},
        {"min_reference_audio_ms": 0},
        {"max_reference_audio_ms": 120000},
        {"max_reference_audio_bytes": 1},
        {"task": "VoiceDesign"},
    ],
)
def test_an_invalid_policy_is_refused(changes: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        VoiceClonePolicy("policy-voice-clone", **changes)  # type: ignore[arg-type]
