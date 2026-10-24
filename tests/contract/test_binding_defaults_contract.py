"""Sparse default declarations reuse frozen canonical domains, without inserting values."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urldefrag

import pytest
from speechnexus_contracts import binding_defaults
from speechnexus_contracts import binding_defaults_models as snapshot_models
from speechnexus_contracts import scenario_profiles_models as profile_models

ROOT = Path(__file__).resolve().parents[2]
CONTRACTS = ROOT / "contracts"
# Fully resolved field-domain fingerprints from integration base 08c77de. This
# guards the source refactor's complete legacy constraints/default annotations,
# rather than keeping a second handwritten schema as an alternative authority.
LEGACY_DOMAINS = {
    "AsrParameters": "899d8fa57e33ed31b7a3f56a5c83850aec8dffe98ef79eadbf9eed7b37031b81",
    "TtsParameters": "5c31b9b4a5a9d06aa2db951d4d60e5d693084d8cbc28107bc60b56529e11fd71",
    "OutputParameters": "24e6365c3bd93e7f8fcd557ffe364c0c8fc85de637493e7b0242d33eb2afe839",
    "ProfileParameters": "2239c2b480bb75b3f5ee920d10da6c9007d6f3487669a80c89944b14b3a3a73c",
}


def encoded(value: Any) -> str:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )


def resolved(node: Any, path: Path) -> Any:
    """Expand the acyclic selected JSON Schema fields; literal data stays literal."""
    if isinstance(node, list):
        return [resolved(item, path) for item in node]
    if not isinstance(node, dict):
        return node
    if "$ref" in node:
        file, fragment = urldefrag(node["$ref"])
        target = (path.parent / file).resolve() if file else path
        assert target.is_relative_to(CONTRACTS)
        value = json.loads(target.read_text())
        for token in fragment.removeprefix("/").split("/") if fragment else []:
            value = value[unquote(token).replace("~1", "/").replace("~0", "~")]
        fields = resolved(value, target)
        siblings = resolved({key: value for key, value in node.items() if key != "$ref"}, path)
        assert isinstance(fields, dict)
        for key in fields.keys() & siblings.keys():
            assert key in {"description", "title"} or encoded(fields[key]) == encoded(siblings[key])
        return {**fields, **siblings}
    return {
        key: value if key in {"default", "const", "enum", "examples"} else resolved(value, path)
        for key, value in node.items()
        if key not in {"$id", "$schema", "$defs"}
    }


@pytest.mark.parametrize("name", LEGACY_DOMAINS)
def test_complete_profile_domains_are_identical_to_the_frozen_legacy_source(name: str) -> None:
    path = CONTRACTS / "scenario-profile.schema.json"
    schema = json.loads(path.read_text())["$defs"][name]
    assert (
        hashlib.sha256(encoded(resolved(schema, path)).encode()).hexdigest() == LEGACY_DOMAINS[name]
    )


def test_static_requiredness_distinguishes_complete_profiles_from_sparse_defaults() -> None:
    definitions = json.loads((CONTRACTS / "scenario-profile.schema.json").read_text())["$defs"]
    assert profile_models.AsrParameters.__required_keys__ == frozenset(
        definitions["AsrParameters"]["required"]
    )
    assert profile_models.TtsParameters.__required_keys__ == frozenset(
        definitions["TtsParameters"]["required"]
    )
    assert snapshot_models.BindingDefaultParameters.__required_keys__ == frozenset()
    assert (
        snapshot_models.ExternalSharedProfileParameterValuesSchemaJsonAsrParameterFields.__required_keys__
        == frozenset()
    )
    assert (
        snapshot_models.ExternalSharedProfileParameterValuesSchemaJsonTtsParameterFields.__required_keys__
        == frozenset()
    )
    schema = json.loads((CONTRACTS / "binding-parameter-defaults.schema.json").read_text())
    assert snapshot_models.Root.__required_keys__ == frozenset(schema["required"])


@pytest.mark.parametrize(
    "parameters", [{}, {"asr": {"max_segment_ms": 15000}}, {"tts": {"instructions": None}}]
)
def test_snapshot_validation_preserves_omission_empty_and_null_without_defaults(
    parameters: Any,
) -> None:
    value = {
        "schema_version": "1.0.0",
        "snapshot_id": "defaults-synthetic",
        "binding_id": "binding-synthetic",
        "binding_revision": "9999999999999999999",
        "binding_revision_id": "binding-revision-synthetic",
        "parameters": parameters,
        "created_at": "2026-10-24T00:00:00Z",
    }
    before = encoded(value)
    assert binding_defaults.parse("BindingParameterDefaultsSnapshot", value) is value
    assert encoded(value) == before
    assert value["parameters"] == parameters
