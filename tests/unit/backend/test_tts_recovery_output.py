"""Recovered audio is delivered only to a new, private local file."""

from __future__ import annotations

import hashlib
import io
import wave
from pathlib import Path

import pytest

from speechnexus_backend import recover_tts
from speechnexus_backend.speech.domain.speech_synthesis import SpeechAudio, SpeechFormat


def test_private_wav_output_is_exact_and_cannot_be_replaced(tmp_path: Path) -> None:
    stream = io.BytesIO()
    with wave.open(stream, "wb") as wav:
        wav.setparams((1, 2, 24000, 0, "NONE", "not compressed"))
        wav.writeframes(b"\0\0" * 240)
    payload = stream.getvalue()
    audio = SpeechAudio(payload, SpeechFormat.WAV, 24000, 1, 10)
    target = tmp_path / "recovered.wav"
    result = recover_tts.write_private_audio(target, audio)
    assert target.read_bytes() == payload
    assert target.stat().st_mode & 0o777 == 0o600
    assert result == {
        "result": "complete",
        "bytes": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
        "format": "wav",
        "sample_rate_hz": 24000,
    }
    with pytest.raises(FileExistsError):
        recover_tts.write_private_audio(target, audio)
    assert target.read_bytes() == payload
    link = tmp_path / "link.wav"
    link.symlink_to(target)
    with pytest.raises(FileExistsError):
        recover_tts.write_private_audio(link, audio)


def test_failed_write_removes_only_its_own_incomplete_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    audio = SpeechAudio(b"\0\0" * 240, SpeechFormat.PCM, 24000, 1, 10)
    target = tmp_path / "partial.wav"

    def fail(fd: int) -> None:
        raise OSError("synthetic failure")

    monkeypatch.setattr("speechnexus_backend.recover_tts.os.fsync", fail)
    with pytest.raises(OSError):
        recover_tts.write_private_audio(target, audio)
    assert not target.exists()


def test_cli_writes_result_and_only_prints_safe_summary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import json
    import sys

    payload = b"\0\0" * 240
    audio = SpeechAudio(payload, SpeechFormat.PCM, 24000, 1, 10)
    output = tmp_path / "result.pcm"
    monkeypatch.setattr(recover_tts, "TtsRecoverySettings", lambda: object())
    monkeypatch.setattr(recover_tts, "recover", lambda *args, **kwargs: audio)
    monkeypatch.setattr(
        sys,
        "argv",
        ["recover_tts", "request-id", "--timeout-seconds", "2", "--output-file", str(output)],
    )
    recover_tts.main()
    summary = json.loads(capsys.readouterr().out)
    assert summary["bytes"] == len(payload)
    assert summary["sha256"] == hashlib.sha256(payload).hexdigest()
    assert summary["format"] == "pcm"
    assert output.read_bytes() == payload
