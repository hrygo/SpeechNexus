"""Real-file controls distinguish an OAS document from schema annotation data."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft7Validator, Draft202012Validator
from openapi_spec_validator import OpenAPIV31SpecValidator

from tests.contract.test_http_contract_changes import document, kernel
from tests.contract.test_tagged_request_compatibility import expanded, tagged

SUPPORTED = "https://json-schema.org/draft/2020-12/schema"
LEGACY = "http://json-schema.org/draft-07/schema#"


def resource(schema: dict[str, Any], kind: str) -> tuple[dict[str, Any], str]:
    if kind.startswith("oas"):
        value: dict[str, Any] = {
            "openapi": "3.1.0",
            "info": {"title": "Synthetic reference", "version": "1"},
            "components": {"schemas": {"Command": schema}},
        }
        if kind == "oas-no-info":
            value.pop("info")
        elif kind == "oas-incomplete-info":
            value["info"].pop("version")
        elif kind == "oas-invalid-paths":
            value["paths"] = []
        elif kind == "oas-extra-root":
            value["type"] = "object"
        elif kind == "oas-legacy":
            value["jsonSchemaDialect"] = LEGACY
        return value, "/components/schemas/Command"
    value = {"$defs": {"Command": schema}}
    if kind != "bare":
        value["openapi"] = "3.1.0"
    if kind == "annotated-lookalike":
        # Even the required OAS fields do not certify this $defs schema resource.
        value["info"] = {"title": "Ordinary schema annotation", "version": "1"}
        value["paths"] = {}
    if kind.startswith("explicit"):
        value["$schema"] = SUPPORTED
        # This is an annotation in a bare schema, not an OAS dialect default.
        value["jsonSchemaDialect"] = LEGACY
        if kind == "explicit-legacy-ancestor":
            value["$defs"]["Command"] = {"$schema": LEGACY, **schema}
    elif kind == "supported-child-only":
        schema["$schema"] = SUPPORTED
    return value, "/$defs/Command"


@pytest.mark.parametrize("suffix", [".json", ".openapi.yaml"])
@pytest.mark.parametrize(
    "kind,allowed",
    [
        ("bare", False),
        ("annotated", False),
        ("annotated-lookalike", False),
        ("supported-child-only", False),
        ("explicit", True),
        ("explicit-legacy-ancestor", False),
        ("oas", True),
        ("oas-no-info", False),
        ("oas-incomplete-info", False),
        ("oas-invalid-paths", False),
        ("oas-extra-root", False),
        ("oas-legacy", False),
    ],
)
def test_real_resource_identity_controls_default_dialect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, suffix: str, kind: str, allowed: bool
) -> None:
    module = kernel()
    monkeypatch.setattr(module, "_ROOT", tmp_path)
    entries = []
    for name, schema in (("old", tagged()), ("new", expanded(tagged()))):
        value, fragment = resource(schema, kind)
        path = Path("contracts") / name / ("resource" + suffix)
        target = tmp_path / path
        target.parent.mkdir(parents=True)
        target.write_text(json.dumps(value), encoding="utf-8")
        # The existing loader reads actual bytes regardless of the filename.
        assert module._load_current(path.as_posix()) == value
        entries.append(
            document("/synthetic/actions", request={"$ref": f"{name}/{target.name}#{fragment}"})
        )
        if kind in ("bare", "annotated", "annotated-lookalike"):
            Draft7Validator.check_schema(value)
            witness = {"mode": "write", "payload": "synthetic", "selection": "legacy"}
            assert Draft7Validator(schema).is_valid(witness) == (name == "old")
        if allowed:
            assert Draft202012Validator(schema).is_valid({"mode": "write", "payload": "synthetic"})
        assert OpenAPIV31SpecValidator.schema_validator.is_valid(value) == (
            kind in ("oas", "oas-legacy")
        )
    issues = module._compare_openapi(*entries, "contracts/entry.yaml", standard_only=False)
    assert bool(issues) is not allowed


@pytest.mark.parametrize("affected", ["old", "new", "both"])
@pytest.mark.parametrize("annotation", [False, True])
@pytest.mark.parametrize("ref_sibling", [False, True])
def test_one_unknown_real_schema_resource_disables_proof_on_either_side(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    affected: str,
    annotation: bool,
    ref_sibling: bool,
) -> None:
    module = kernel()
    monkeypatch.setattr(module, "_ROOT", tmp_path)
    entries = []
    for name, schema in (("old", tagged()), ("new", expanded(tagged()))):
        kind = ("annotated" if annotation else "bare") if affected in (name, "both") else "explicit"
        value, fragment = resource(schema, kind)
        target = tmp_path / "contracts" / name / "target.json"
        target.parent.mkdir(parents=True)
        target.write_text(json.dumps(value), encoding="utf-8")
        # A supported bridge and supported ref sibling must not hide the target.
        bridge = target.with_name("bridge.json")
        reference = {"$ref": f"target.json#{fragment}"}
        if ref_sibling:
            reference["$schema"] = SUPPORTED
        bridge.write_text(
            json.dumps(
                {
                    "$schema": SUPPORTED,
                    "$defs": {"Command": reference},
                }
            ),
            encoding="utf-8",
        )
        entries.append(
            document("/synthetic/actions", request={"$ref": f"{name}/bridge.json#/$defs/Command"})
        )
    assert module._compare_openapi(*entries, "contracts/entry.yaml", standard_only=False)


@pytest.mark.parametrize("direction", ["request", "response", "openai"])
def test_valid_external_oas_retains_direction_and_openai_policy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, direction: str
) -> None:
    module = kernel()
    monkeypatch.setattr(module, "_ROOT", tmp_path)
    entries = []
    for name, schema in (("old", tagged()), ("new", expanded(tagged()))):
        value, fragment = resource(schema, "oas")
        target = tmp_path / "contracts" / name / "ordinary.json"
        target.parent.mkdir(parents=True)
        target.write_text(json.dumps(value), encoding="utf-8")
        entries.append(
            document(
                "/v1/audio/speech",
                **{
                    ("response" if direction == "response" else "request"): {
                        "$ref": f"{name}/ordinary.json#{fragment}"
                    }
                },
            )
        )
    issues = module._compare_openapi(
        *entries, "contracts/entry.yaml", standard_only=direction == "openai"
    )
    assert bool(issues) == (direction != "request")
