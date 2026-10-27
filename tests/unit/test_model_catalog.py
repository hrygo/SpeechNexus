"""Listed models must satisfy the frozen contract and never overstate readiness."""

from __future__ import annotations

from typing import Any

import pytest
from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry

from speechnexus_backend.speech.application.capability_snapshot import (
    CapabilityPolicy,
    capability_snapshot,
    matches_if_none_match,
    snapshot_etag,
)
from speechnexus_backend.speech.application.model_catalog import (
    ReadinessFacts,
    capability_model,
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

FULL_DEFINITION: dict[str, Any] = {
    "schema_version": "1.0.0",
    "binding_id": "tts-quality",
    "revision": "3",
    "revision_id": "binding-rev-3",
    "logical_model": "qwen3-tts-customvoice",
    "tasks": ["tts_file", "tts_realtime"],
    "engine": {
        "kind": "vllm_omni",
        "image_digest": "sha256:" + "1" * 64,
        "runtime_version": "vllm-omni-2026.10",
    },
    "artifact_set": [
        {
            "role": "weights",
            "artifact_id": "model-qwen3-tts",
            "digest": "sha256:" + "2" * 64,
            "media_type": "application/x-safetensors",
            "size_bytes": 1,
        }
    ],
    "execution_recipe": {
        "recipe_revision": "1",
        "capacity_manifest_id": "cap-tts-quality-3",
        "stage_roles": ["tts_talker", "tts_code2wav"],
        "requires_complete_reservation": True,
    },
    "parameter_domains": {
        "tts": {
            "languages": ["zh", "en"],
            "audio_formats": ["mp3", "wav", "pcm"],
            "sample_rates_hz": [24000],
            "text_commit_modes": ["explicit", "sentence"],
            "audio_delivery_modes": ["complete", "chunked"],
            "supports_instructions": True,
            "supports_speed": True,
            "speed_range": {"minimum": 0.75, "maximum": 1.25},
            "max_input_codepoints": 2000,
            "supports_seed": False,
        }
    },
    "release_tier": "production",
    "publication": {
        "status": "draft",
        "published_at": None,
        "retired_at": None,
        "capacity_manifest_id": None,
        "qualification_report_id": None,
        "disabled_reason": None,
    },
    "created_at": "2026-10-27T08:00:00Z",
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


def test_capability_model_carries_only_declared_domains_and_readiness() -> None:
    value = capability_model(BINDING, FULL_DEFINITION, owned_by="speechnexus")

    _component("CapabilityModel").validate(value)
    assert value["parameter_domains"] == FULL_DEFINITION["parameter_domains"]
    assert value["readiness"] == {
        "configured": True,
        "engine_ready": False,
        "quality_qualified": False,
        "admission_state": "unavailable",
    }


def test_capability_projection_rejects_a_definition_without_contract_domains() -> None:
    with pytest.raises(ValueError):
        capability_model(BINDING, DEFINITION, owned_by="speechnexus")


def _policy() -> CapabilityPolicy:
    return CapabilityPolicy(
        deployment_id="test:speechnexus-api:speech",
        workspace_id="workspace-default",
        policy_revision=7,
        max_audio_bytes=4 * 1024 * 1024,
        max_text_codepoints=1_000_000,
        max_segment_ms=30_000,
        max_realtime_session_ms=600_000,
        max_queue_wait_ms=600_000,
    )


def test_capability_snapshot_satisfies_the_frozen_contract() -> None:
    model = capability_model(BINDING, FULL_DEFINITION, owned_by="speechnexus")
    snapshot = capability_snapshot(
        models=[model],
        profiles=[],
        voices=[],
        catalog_revision=12,
        generated_at_us=BINDING.published_at_us,
        policy=_policy(),
    )

    _component("CapabilitySnapshot").validate(snapshot)
    assert snapshot["models"][0]["parameter_domains"]["tts"]["audio_formats"] == [
        "mp3",
        "wav",
        "pcm",
    ]


def test_snapshot_id_and_etag_are_stable_for_equal_facts() -> None:
    model = capability_model(BINDING, FULL_DEFINITION, owned_by="speechnexus")
    first = capability_snapshot(
        models=[model],
        profiles=[],
        voices=[],
        catalog_revision=12,
        generated_at_us=BINDING.published_at_us,
        policy=_policy(),
    )
    second = capability_snapshot(
        models=[model],
        profiles=[],
        voices=[],
        catalog_revision=12,
        generated_at_us=BINDING.published_at_us,
        policy=_policy(),
    )
    etag = snapshot_etag(
        policy=_policy(),
        workspace_id="workspace-default",
        catalog_revision=12,
        snapshot=first,
    )

    assert first == second
    assert matches_if_none_match(etag, etag)
    assert matches_if_none_match("W/" + etag, etag)
    assert matches_if_none_match('"other", ' + etag, etag)
    assert not matches_if_none_match('"other"', etag)
