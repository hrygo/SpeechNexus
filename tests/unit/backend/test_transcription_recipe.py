"""The private ASR recipe is bounded, owner-only and internally consistent."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
import rfc8785

from speechnexus_backend.speech.infrastructure.transcription_recipe import (
    AsrParameters,
    load_transcription_recipe,
    private_file,
)

_DIGEST = "sha256:" + "a" * 64
_TEMPERATURE = 0.0


def _document() -> dict[str, Any]:
    physical = {
        "unified_physical_memory_bytes": 0,
        "device_memory_bytes": 8_000_000_000,
        "host_memory_bytes": 2_000_000_000,
        "cpu_decode_millicores": 1_000,
        "host_pcm_buffer_bytes": 0,
        "scratch_disk_bytes": 0,
    }
    per_slot = {
        "unified_physical_memory_bytes": 0,
        "device_memory_bytes": 1_000_000_000,
        "host_memory_bytes": 500_000_000,
        "cpu_decode_millicores": 100,
        "host_pcm_buffer_bytes": 0,
        "scratch_disk_bytes": 0,
    }
    document: dict[str, Any] = {
        "protocol": "transcription_admission_v1",
        "model_id": "asr-quality",
        "binding_publication_id": "binding-rev-3",
        "binding_revision_id": "binding-rev-3",
        "manifest_id": "manifest-1",
        "policy_revision_id": "policy-rev-1",
        "service_class": "batch",
        "admission_expires_at_us": 4_000_000_000_000_000,
        "quota_dimensions": ["concurrent_attempts"],
        "stage": {
            "stage_id": "stage-asr-1",
            "instance_id": "instance-1",
            "minimum_slots": 1,
            "reserved_slots": 1,
        },
        "runtime_plan": {
            "binding": {
                "target_node_id": "node-1",
                "pool_id": "asr-batch",
                "gateway_id": "gateway-1",
                "approved_template_id": "template-1",
                "binding_revision_id": "binding-rev-3",
                "manifest_id": "manifest-1",
                "config_digest": _DIGEST,
            },
            "topology_digest": _DIGEST,
            "qualification_digest": _DIGEST,
            "memory_architecture": "discrete_device_memory",
            "service_class": "batch",
            "loading_policy": "resident",
            "max_slots": 1,
            "realtime_reserved_slots": 0,
            "peak": physical,
            "steady": physical,
            "per_slot": per_slot,
        },
        "asr": {
            "silence_duration_ms": 400,
            "preview_interval_ms": 1_000,
            "temperature": _TEMPERATURE,
            "final_deadline_ms": 1_000,
            "auxiliary_deadline_ms": 5_000,
        },
    }
    body = rfc8785.dumps(document)
    document["recipe_digest"] = "sha256:" + hashlib.sha256(body).hexdigest()
    return document


def _write(tmp_path: Path, document: dict[str, Any], *, mode: int = 0o600) -> Path:
    path = tmp_path / "recipe.json"
    path.write_text(json.dumps(document, separators=(",", ":")))
    path.chmod(mode)
    return path


def test_valid_private_recipe_loads_frozen_facts(tmp_path: Path) -> None:
    recipe = load_transcription_recipe(_write(tmp_path, _document()))

    assert recipe.model_id == "asr-quality"
    assert recipe.binding_publication_id == "binding-rev-3"
    assert recipe.stage_id == "stage-asr-1"
    assert recipe.instance_id == "instance-1"
    assert recipe.reserved_slots == 1
    assert recipe.asr.preview_interval_ms == 1_000
    assert recipe.asr.final_deadline_ms == 1_000
    assert recipe.approved.binding_revision_id == "binding-rev-3"
    assert recipe.approved.quota_dimensions == frozenset({"concurrent_attempts"})


def test_relative_recipe_path_is_rejected() -> None:
    with pytest.raises(ValueError):
        load_transcription_recipe(Path("recipe.json"))


def test_group_readable_recipe_file_is_rejected(tmp_path: Path) -> None:
    path = _write(tmp_path, _document(), mode=0o640)
    with pytest.raises(ValueError):
        load_transcription_recipe(path)


def test_duplicate_json_keys_are_rejected(tmp_path: Path) -> None:
    path = tmp_path / "recipe.json"
    path.write_text('{"protocol":"transcription_admission_v1","protocol":"other"}')
    path.chmod(0o600)
    with pytest.raises(ValueError):
        load_transcription_recipe(path)


@pytest.mark.parametrize("constant", ["NaN", "Infinity", "-Infinity"])
def test_non_finite_recipe_values_are_rejected(tmp_path: Path, constant: str) -> None:
    path = tmp_path / "recipe.json"
    path.write_text('{"asr":{"temperature":' + constant + "}}")
    path.chmod(0o600)
    with pytest.raises(ValueError):
        load_transcription_recipe(path)


def test_declared_digest_must_match_the_document(tmp_path: Path) -> None:
    document = _document()
    document["recipe_digest"] = _DIGEST
    with pytest.raises(ValueError):
        load_transcription_recipe(_write(tmp_path, document))


def test_asr_parameters_must_fit_the_frozen_command_ranges() -> None:
    for values in (
        (400, 0, 0.0, 5_000, 1_000),
        (400, 1_000, 0.0, 5_000, 99),
        (400, 6_000, 0.0, 5_000, 1_000),
        (400, 1_000, 1.5, 5_000, 1_000),
    ):
        with pytest.raises(ValueError):
            AsrParameters(*values)


def test_private_file_refuses_oversized_content(tmp_path: Path) -> None:
    path = tmp_path / "recipe.json"
    path.write_bytes(b"x" * 64)
    path.chmod(0o600)
    assert private_file(path, 64) == b"x" * 64
    with pytest.raises(ValueError):
        private_file(path, 63)
