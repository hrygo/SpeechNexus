"""The resident TTS variant rule is spelled once, in the domain.

`deploy/job-startup-gate.py` and `scripts/dev-env/check-async-job-prereqs.py` both
call this function, so the cases matter to both: the ordering of the finding, the
de-duplication of `kinds`, and the fact that one variant (or none) is not a
finding. A second spelling of the rule in either consumer is how "the preflight
passes while the gate refuses" happens (#1075).
"""

from __future__ import annotations

import pytest

from speechnexus_backend.speech.domain.lane_selection import (
    DEFAULT_KINDS_ENV,
    RESIDENT_TTS_VARIANT_KINDS,
    resident_tts_variant_findings,
)


def test_the_three_resident_variants_are_the_kinds_the_deployment_selects() -> None:
    assert RESIDENT_TTS_VARIANT_KINDS == ("tts", "voice_design", "voice_clone")


@pytest.mark.parametrize(
    "kinds",
    [
        (),
        ("asr",),
        ("asr", "tts"),
        ("asr", "voice_design"),
        ("asr", "voice_clone"),
        ("asr", "tts", "voice_validation"),
        # One variant named twice is still one variant.
        ("tts", "tts"),
    ],
)
def test_a_selection_without_two_variants_is_accepted(kinds: tuple[str, ...]) -> None:
    assert resident_tts_variant_findings(kinds) == []


@pytest.mark.parametrize(
    ("kinds", "selected"),
    [
        (("tts", "voice_design"), "tts, voice_design"),
        # Listed in the order the selection names them, so the finding reads
        # back what the operator wrote.
        (("voice_clone", "asr", "tts"), "voice_clone, tts"),
        (("tts", "voice_design", "voice_clone"), "tts, voice_design, voice_clone"),
    ],
)
def test_two_variants_are_named_in_one_finding(kinds: tuple[str, ...], selected: str) -> None:
    findings = resident_tts_variant_findings(kinds)
    assert len(findings) == 1
    assert f"{DEFAULT_KINDS_ENV} names {selected}:" in findings[0]
    assert "one resident TTS variant at a time" in findings[0]
    assert "Select the one variant this deployment runs" in findings[0]


def test_the_caller_names_its_own_environment_variable() -> None:
    # The gate and the self-check pass their own constant, so neither has to keep
    # a copy of the message with a different variable name baked in.
    (finding,) = resident_tts_variant_findings(["tts", "voice_design"], kinds_env="OTHER_ENV")
    assert finding.startswith("OTHER_ENV names tts, voice_design:")
