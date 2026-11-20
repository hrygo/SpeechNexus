"""One synthesis bound is judged at admission, on both entries (#1073).

The public contract declares one synthesis per request at up to
``MAX_SPEECH_TEXT_CHARS`` (4096). Two declarations narrow that for a concrete
request: the planner chunk budget and the selected binding's
``max_input_codepoints``. These cases pin the shared rule -- the tighter one wins
-- where each entry applies it: the synchronous plan (whose admission checks the
plan's planner budget) and the Job create gate, which previously never compared
text against either cap and so answered ``202`` for text dispatch would refuse.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

import pytest

from speechnexus_backend.speech.application.speech_synthesis import (
    SpeechUnavailableError,
    SpeechUnsupportedError,
)
from speechnexus_backend.speech.domain.speech_synthesis import (
    MAX_SPEECH_TEXT_CHARS,
    PLANNER_CHARS_MAX,
    PLANNER_CHARS_MIN,
    SpeechFormat,
    SpeechPlan,
    speech_text_bound,
)
from speechnexus_backend.speech.infrastructure.public_job_admission import (
    PublicJobAdmissionRejectedError,
    _effective_planner,
    binding_gate,
)
from speechnexus_backend.speech.infrastructure.tts_admission import PostgresTtsAdmission

TTS_BLOCK: dict[str, Any] = {
    "language": "en",
    "text_commit_mode": "explicit",
    "audio_delivery": "complete",
    "speed": 1.0,
    "instructions": None,
    "seed": None,
    "planner": {"max_chars": 400, "max_wait_ms": 1000},
}


def _definition(max_input_codepoints: int | None) -> dict[str, Any]:
    definition: dict[str, Any] = {"execution_defaults": {"tts": dict(TTS_BLOCK)}}
    if max_input_codepoints is not None:
        definition["parameter_domains"] = {"tts": {"max_input_codepoints": max_input_codepoints}}
    return definition


def _gate(
    text: str,
    *,
    max_input_codepoints: int | None,
    planner: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run the create path's request gate exactly as ``build_public_job_admission`` does."""

    intent: dict[str, Any] = {"overrides": {"tts": {"planner": planner}}} if planner else {}
    effective_planner = _effective_planner(TTS_BLOCK, intent)
    effective = {**TTS_BLOCK, "planner": effective_planner}
    binding_gate(
        stage="request",
        definition=_definition(max_input_codepoints),
        kind="tts",
        effective=effective,
        text=text,
    )
    return effective_planner


def test_the_tighter_declared_cap_decides() -> None:
    assert speech_text_bound(2000, 4096) == 2000
    assert speech_text_bound(4096, 2000) == 2000
    assert speech_text_bound(4096, 4096) == MAX_SPEECH_TEXT_CHARS


@pytest.mark.parametrize("chars", [401, 1000, 4096])
def test_text_inside_both_caps_passes_the_create_gate(chars: int) -> None:
    """401-4096 is admissible when the caller raises the budget and the model allows it."""

    planner = _gate(
        "啊" * chars, max_input_codepoints=MAX_SPEECH_TEXT_CHARS, planner={"max_chars": 4096}
    )
    assert planner["max_chars"] == 4096
    # The omitted wait keeps the preset: one override field cannot erase the other.
    assert planner["max_wait_ms"] == 1000


@pytest.mark.parametrize("chars", [401, 1000, 4096])
def test_the_same_text_is_refused_when_the_binding_ceiling_is_lower(chars: int) -> None:
    """The binding's own ceiling still refuses the text an execution would refuse."""

    with pytest.raises(PublicJobAdmissionRejectedError) as rejected:
        _gate("啊" * chars, max_input_codepoints=400, planner={"max_chars": 4096})

    assert rejected.value.code == "unsupported_parameter"


def test_the_planner_budget_alone_still_refuses_without_a_raised_override() -> None:
    """A 401-character Job with the preset budget is refused at create, not at dispatch."""

    with pytest.raises(PublicJobAdmissionRejectedError) as rejected:
        _gate("啊" * 401, max_input_codepoints=MAX_SPEECH_TEXT_CHARS)

    assert rejected.value.code == "unsupported_parameter"


def test_a_definition_without_a_ceiling_is_bounded_by_the_contract() -> None:
    """No declared model ceiling means ``MAX_SPEECH_TEXT_CHARS``, never no bound."""

    planner = _gate(
        "啊" * MAX_SPEECH_TEXT_CHARS, max_input_codepoints=None, planner={"max_chars": 4096}
    )
    assert planner["max_chars"] == MAX_SPEECH_TEXT_CHARS
    with pytest.raises(PublicJobAdmissionRejectedError):
        _gate("啊" * 401, max_input_codepoints=None)


@pytest.mark.parametrize("declared", [PLANNER_CHARS_MAX, 1_000_000])
def test_the_effective_planner_never_states_more_than_one_synthesis(declared: int) -> None:
    block = {**TTS_BLOCK, "planner": {"max_chars": declared, "max_wait_ms": 1000}}
    planner = _effective_planner(block, {})
    assert planner["max_chars"] == 4096


def test_a_planner_below_the_preset_minimum_is_a_binding_defect() -> None:
    block = {**TTS_BLOCK, "planner": {"max_chars": PLANNER_CHARS_MIN - 1, "max_wait_ms": 1000}}
    with pytest.raises(PublicJobAdmissionRejectedError) as rejected:
        _effective_planner(block, {})
    assert rejected.value.code == "job_binding_incomplete"


class _Originals:
    def read(self, identity: Any) -> None:
        return None


class _Facts:
    def read(self, plan: Any, recipe: Any) -> None:
        raise SpeechUnavailableError("synthetic facts sentinel")


class _Service:
    def admit(self, recipe: Any) -> None:
        raise AssertionError("no admission attempt was expected")


class _Recipe:
    model_id = "tts-quality"
    approved = SimpleNamespace(binding_revision_id="binding-rev-2")


def _synchronous_admission() -> PostgresTtsAdmission:
    return PostgresTtsAdmission(
        cast(Any, _Facts()),
        cast(Any, _Service()),
        cast(Any, _Originals()),
        cast(Any, _Recipe()),
        default_language="en",
    )


def _plan(text: str, planner_max_chars: int) -> SpeechPlan:
    return SpeechPlan(
        request_id="request-text-bound",
        model_id="tts-quality",
        workspace_id="workspace-default",
        binding_revision_id="binding-rev-2",
        workspace_policy_revision=1,
        text=text,
        voice_id="voice-vivian",
        voice_revision_id="voice-rev-4",
        voice_revision=4,
        response_format=SpeechFormat.PCM,
        sample_rate_hz=24000,
        channels=1,
        speed=1.0,
        planner_max_chars=planner_max_chars,
    )


def test_synchronous_admission_refuses_text_over_the_plan_budget() -> None:
    with pytest.raises(SpeechUnsupportedError) as rejected:
        _synchronous_admission().admit(_plan("啊" * 401, 400))

    assert rejected.value.param == "input"


@pytest.mark.parametrize("chars", [401, 4096])
def test_synchronous_admission_accepts_the_raised_budget(chars: int) -> None:
    """The plan the route builds carries ``min(planner, binding ceiling)``.

    Here the budget is already 4096, so the planner gate passes and admission
    proceeds to the next read (the sentinel) instead of answering 422.
    """

    with pytest.raises(SpeechUnavailableError):
        _synchronous_admission().admit(_plan("啊" * chars, 4096))
