"""Synthetic probe observations cannot substitute for owner-issued evidence."""

import wave
from dataclasses import replace
from io import BytesIO
from typing import Any

import pytest

from speechnexus_backend.speech.domain.qualification_probe_decision import (
    ProbeEvidenceError,
    ProbeIdentity,
    ProbeTerminalEvidence,
    verify_probe_audio_and_terminal,
)


def test_complete_audio_requires_exact_generation_terminal_and_release() -> None:
    buffer = BytesIO()
    with wave.open(buffer, "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(24000)
        stream.writeframes(b"\x00\x00" * 24)
    wav = buffer.getvalue()
    identity = ProbeIdentity(
        "submit-1",
        "binding-1",
        "policy-1",
        "host-1",
        "gateway-1",
        "instance-1",
        1,
        "voice-rev-1",
        "Vivian",
    )
    terminal = ProbeTerminalEvidence(
        "submit-1", "engine_terminal_response", "sha256:" + "a" * 64, True, True, 1
    )
    args: dict[str, Any] = {
        "expected": identity,
        "observed": identity,
        "wav": wav,
        "terminal": terminal,
        "generated_samples": 24,
        "output_ceiling_samples": 24,
        "maximum_wav_bytes": 1024,
    }
    assert verify_probe_audio_and_terminal(**args).samples == 24
    bad = (
        {"observed": replace(identity, instance_generation=2)},
        {"terminal": replace(terminal, submit_intent_id="submit-2")},
        {"terminal": replace(terminal, resources_released=False)},
        {"terminal": replace(terminal, evidence_kind="abort_requested")},
        {"generated_samples": 25},
        {"output_ceiling_samples": 23},
        {"wav": wav[:-1]},
    )
    for override in bad:
        with pytest.raises(ProbeEvidenceError):
            verify_probe_audio_and_terminal(**(args | override))
