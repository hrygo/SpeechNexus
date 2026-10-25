"""Explicit declared defaults, immutable parent identity and no implicit engine values."""

import copy
import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from speechnexus_contracts import ContractValidationError

from speechnexus_backend.speech.application.binding_documents import check_command, decode_document
from speechnexus_backend.speech.domain.binding_defaults import BindingDefaultsSnapshot
from speechnexus_backend.speech.interfaces.binding_default_drafts import (
    defaults_to_wire,
    prepare_binding_defaults_draft,
)
from speechnexus_backend.speech.interfaces.binding_drafts import prepare_binding_draft
from tests.support.binding_examples import binding_request


def test_presence_empty_and_null_have_distinct_original_intents_without_insertion() -> None:
    original = binding_request()
    legacy = prepare_binding_draft(original, "same-original-key")
    omitted = prepare_binding_defaults_draft(original, "same-original-key")
    assert legacy == omitted and omitted.parameter_defaults is None
    explicit = prepare_binding_defaults_draft(
        {**original, "parameter_defaults": {}}, "same-original-key"
    )
    assert explicit.parameter_defaults == b"{}" and explicit.intent != omitted.intent
    assert explicit.definition == omitted.definition
    check_command(explicit)
    with pytest.raises(ContractValidationError):
        prepare_binding_defaults_draft({**original, "parameter_defaults": None})
    with pytest.raises(ContractValidationError):
        prepare_binding_draft({**original, "parameter_defaults": {}})


def test_full_intent_keeps_values_but_parent_definition_stays_legacy_shape() -> None:
    request = {**binding_request(), "parameter_defaults": {"asr": {"max_segment_ms": 15000}}}
    before = copy.deepcopy(request)
    command = prepare_binding_defaults_draft(request, "original-default-key")
    assert request == before
    assert decode_document(command.intent) == request
    assert decode_document(command.parameter_defaults or b"{}") == request["parameter_defaults"]
    assert "parameter_defaults" not in decode_document(command.definition)
    check_command(command)
    with pytest.raises(ValueError):
        check_command(replace(command, parameter_defaults=None))


@pytest.mark.parametrize(
    "defaults",
    [
        {"asr": {"language": "fr"}},
        {"asr": {"max_segment_ms": 30001}},
        {"asr": {"finalization": "streaming_finalize"}},
        {"asr": {"rollback_tokens": 1}},
        {"asr": {"preview_interval_ms": 5000, "max_segment_ms": 1000}},
        {"tts": {"language": "en"}},
    ],
)
def test_defaults_must_fit_declared_parent_domains(defaults: object) -> None:
    with pytest.raises(ContractValidationError):
        prepare_binding_defaults_draft({**binding_request(), "parameter_defaults": defaults})


def test_neutral_disabled_values_remain_explicit_and_no_annotations_are_inserted() -> None:
    value = {"asr": {"rollback_tokens": 0}}
    command = prepare_binding_defaults_draft({**binding_request(), "parameter_defaults": value})
    assert decode_document(command.parameter_defaults or b"{}") == value
    assert "auxiliary_deadline_ms" not in value["asr"]


def test_snapshot_projection_uses_only_fixed_facts_and_retains_explicit_empty() -> None:
    snapshot = BindingDefaultsSnapshot(
        "defaults-synthetic", "binding-synthetic", "1", "binding-revision-synthetic", b"{}", 0
    )
    wire = defaults_to_wire(snapshot)
    assert wire["parameters"] == {} and wire["created_at"] == "1970-01-01T00:00:00.000000Z"
    assert wire["binding_revision_id"] == snapshot.revision_id
    assert "publication" not in wire and "key_digest" not in wire


def tts_request() -> dict[str, Any]:
    examples = json.loads(
        (Path(__file__).parents[1] / "fixtures/contracts/consumer-profiles.json").read_text()
    )["examples"]
    parent = next(x["value"] for x in examples if x["id"] == "binding-revision-source-1")
    return {key: copy.deepcopy(parent[key]) for key in binding_request() if key in parent}


@pytest.mark.parametrize(
    "values",
    [
        {"language": "fr"},
        {"speed": 1.5},
        {"seed": 0},
        {"text_commit_mode": "none"},
        {"audio_delivery": "invalid"},
    ],
)
def test_tts_defaults_are_checked_against_the_same_parent(values: dict[str, Any]) -> None:
    with pytest.raises(ContractValidationError):
        prepare_binding_defaults_draft({**tts_request(), "parameter_defaults": {"tts": values}})


def test_tts_null_explicit_values_and_sparse_nested_plan_remain_unchanged() -> None:
    values = {"tts": {"speed": 1.0, "seed": None, "instructions": None}}
    command = prepare_binding_defaults_draft({**tts_request(), "parameter_defaults": values})
    assert decode_document(command.parameter_defaults or b"{}") == values
    request = tts_request()
    request["parameter_domains"]["tts"]["supports_instructions"] = False
    with pytest.raises(ContractValidationError):
        prepare_binding_defaults_draft(
            {**request, "parameter_defaults": {"tts": {"instructions": "x"}}}
        )
    request["parameter_domains"]["tts"]["max_input_codepoints"] = 100
    with pytest.raises(ContractValidationError):
        prepare_binding_defaults_draft(
            {
                **request,
                "parameter_defaults": {"tts": {"planner": {"max_chars": 101, "max_wait_ms": 100}}},
            }
        )
    with pytest.raises(ContractValidationError):
        prepare_binding_defaults_draft(
            {**tts_request(), "parameter_defaults": {"output": {"audio_format": "flac"}}}
        )
