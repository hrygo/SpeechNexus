"""Listed models must satisfy the frozen contract and never overstate readiness."""

from __future__ import annotations

from typing import Any

import pytest
from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry

from speechnexus_backend.speech.application.model_catalog import (
    ReadinessFacts,
    model_document,
    model_list,
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


BINDING = Publication("binding", "tts-quality", 3, "binding-rev-3", 1_700_000_000_500_000)
DEFINITION: dict[str, Any] = {
    "binding_id": "tts-quality",
    "logical_model": "qwen3-tts-customvoice",
    "tasks": ["tts_file", "tts_realtime", "tts_file"],
    "release_tier": "production",
}


def test_model_document_satisfies_the_frozen_contract() -> None:
    _component("Model").validate(model_document(BINDING, DEFINITION, owned_by="speechnexus"))


def test_unmeasured_readiness_stays_unavailable() -> None:
    document_value = model_document(BINDING, DEFINITION, owned_by="speechnexus")

    assert document_value["nexus"]["readiness"] == {
        "configured": True,
        "engine_ready": False,
        "quality_qualified": False,
        "admission_state": "unavailable",
    }


def test_measured_readiness_is_reported_verbatim() -> None:
    facts = ReadinessFacts(engine_ready=True, quality_qualified=True, admission_state="queueable")
    document_value = model_document(BINDING, DEFINITION, owned_by="speechnexus", readiness=facts)

    assert document_value["nexus"]["readiness"]["engine_ready"] is True
    assert document_value["nexus"]["readiness"]["admission_state"] == "queueable"


def test_tasks_are_deduplicated_and_ordered() -> None:
    document_value = model_document(BINDING, DEFINITION, owned_by="speechnexus")

    assert document_value["nexus"]["tasks"] == ["tts_file", "tts_realtime"]


def test_created_is_seconds_from_the_publication_instant() -> None:
    document_value = model_document(BINDING, DEFINITION, owned_by="speechnexus")

    assert document_value["created"] == BINDING.published_at_us // 1_000_000


def test_binding_that_disagrees_with_its_publication_is_rejected() -> None:
    mismatched = {**DEFINITION, "binding_id": "other-binding"}
    with pytest.raises(ValueError):
        model_document(BINDING, mismatched, owned_by="speechnexus")


@pytest.mark.parametrize(
    "broken",
    [
        {**DEFINITION, "tasks": []},
        {**DEFINITION, "tasks": ["not_a_task"]},
        {**DEFINITION, "release_tier": "beta"},
        {**DEFINITION, "logical_model": ""},
    ],
)
def test_incomplete_binding_declarations_are_rejected(broken: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        model_document(BINDING, broken, owned_by="speechnexus")


def test_only_published_bindings_become_models() -> None:
    profile = Publication("profile", "profile-main", 1, "profile-rev-1", 1)
    with pytest.raises(ValueError):
        model_document(profile, DEFINITION, owned_by="speechnexus")


def test_unknown_admission_state_is_rejected() -> None:
    with pytest.raises(ValueError):
        ReadinessFacts(admission_state="probably_available")


def test_list_envelope_wraps_the_documents() -> None:
    first = model_document(BINDING, DEFINITION, owned_by="speechnexus")

    assert model_list([first]) == {"object": "list", "data": [first]}
