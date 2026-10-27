"""Projections must satisfy the frozen public contract, not a local copy of it."""

from __future__ import annotations

from typing import Any

import pytest
from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry

from speechnexus_backend.speech.application.effective_configuration import (
    effective_configuration,
)
from speechnexus_backend.speech.domain.publications import Publication
from tests.support.contracts import ROOT, document, retrieve

_OPENAPI = document(ROOT / "speech-openapi.yaml")


def _component(name: str) -> Draft202012Validator:
    schema = {"$ref": "#/components/schemas/" + name, **_OPENAPI}
    return Draft202012Validator(
        schema,
        registry=Registry(retrieve=retrieve),  # type: ignore[call-arg]
        format_checker=FormatChecker(),
    )


TTS_BINDING = Publication("binding", "binding-tts", 2, "binding-rev-2", 1_000)
MAIN_PROFILE = Publication("profile", "profile-main", 1, "profile-rev-1", 1_000)
VIVIAN = Publication("voice", "voice-vivian", 4, "voice-rev-4", 1_000)
PRONUNCIATION = Publication("pronunciation", "pron-main", 2, "pron-rev-2", 1_000)


def test_projection_satisfies_the_frozen_contract() -> None:
    document_value = effective_configuration(
        models={"tts": TTS_BINDING},
        policy_revision=7,
        catalog_revision=31,
        profile=MAIN_PROFILE,
        voice=VIVIAN,
        pronunciation=PRONUNCIATION,
    )
    _component("EffectiveConfiguration").validate(document_value)


def test_published_reference_satisfies_the_frozen_contract() -> None:
    _component("ResourceReference").validate(TTS_BINDING.reference)
    _component("ResourceReference").validate(MAIN_PROFILE.reference)


def test_revisions_are_decimal_text_in_the_projection() -> None:
    document_value = effective_configuration(
        models={"tts": TTS_BINDING}, policy_revision=7, catalog_revision=31
    )
    assert document_value["models"]["tts"]["revision"] == "2"
    assert document_value["policy_revision"] == "7"
    assert document_value["catalog_revision"] == "31"


def test_empty_model_set_is_rejected() -> None:
    with pytest.raises(ValueError):
        effective_configuration(models={}, policy_revision=1, catalog_revision=1)


def test_unknown_model_slot_is_rejected() -> None:
    with pytest.raises(ValueError):
        effective_configuration(
            models={"diarization": TTS_BINDING}, policy_revision=1, catalog_revision=1
        )


def test_model_slot_must_reference_a_binding() -> None:
    with pytest.raises(ValueError):
        effective_configuration(models={"tts": VIVIAN}, policy_revision=1, catalog_revision=1)


def test_voice_and_profile_slots_keep_their_publication_kind() -> None:
    with pytest.raises(ValueError):
        effective_configuration(
            models={"tts": TTS_BINDING},
            policy_revision=1,
            catalog_revision=1,
            voice=TTS_BINDING,
        )
    with pytest.raises(ValueError):
        effective_configuration(
            models={"tts": TTS_BINDING},
            policy_revision=1,
            catalog_revision=1,
            profile=VIVIAN,
        )


def test_pronunciation_slot_must_reference_a_pronunciation_publication() -> None:
    with pytest.raises(ValueError):
        effective_configuration(
            models={"tts": TTS_BINDING},
            policy_revision=1,
            catalog_revision=1,
            pronunciation=MAIN_PROFILE,
        )


def test_unknown_degradation_is_rejected() -> None:
    with pytest.raises(ValueError):
        effective_configuration(
            models={"tts": TTS_BINDING},
            policy_revision=1,
            catalog_revision=1,
            degradation="silently_worse",
        )


def test_parameters_are_copied_not_aliased() -> None:
    source: dict[str, Any] = {"tts": {"speed": 1.0}}
    document_value = effective_configuration(
        models={"tts": TTS_BINDING},
        policy_revision=1,
        catalog_revision=1,
        parameters=source,
    )
    source["tts"]["speed"] = 2.0
    assert document_value["parameters"] == {"tts": {"speed": 1.0}}
