"""Explicit nonstandard HTTP selection must not inherit the OpenAI Nexus exclusion."""

from __future__ import annotations

import copy
import importlib.util
import json
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
SOURCES = (
    ("contracts/admin-openapi.yaml", "/v1/nexus/admin/profiles"),
    ("contracts/workbench-openapi.yaml", "/v1/workbench/projects"),
    ("contracts/internal/runtime-control.openapi.yaml", "/v1/internal/runtime/intents"),
    ("contracts/internal/execution-control.openapi.yaml", "/v1/internal/executions"),
)


def kernel() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "http_change_kernel", ROOT / "scripts/dev-env/check-openai-contract-compat.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def document(path: str, request: Any = None, response: Any = None) -> dict[str, Any]:
    return {
        "openapi": "3.1.0",
        "info": {"title": "Synthetic", "version": "0.1.0.dev0"},
        "paths": {
            path: {
                "post": {
                    "operationId": "syntheticCreate",
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": request if request is not None else {"type": "object"}
                            }
                        },
                    },
                    "responses": {
                        "201": {
                            "description": "Synthetic",
                            "content": {
                                "application/json": {
                                    "schema": response
                                    if response is not None
                                    else {"type": "object"}
                                }
                            },
                        }
                    },
                }
            }
        },
    }


@pytest.mark.parametrize("source,path", SOURCES)
def test_full_http_mode_rejects_removed_nonstandard_path(source: str, path: str) -> None:
    base = document(path)
    current: dict[str, Any] = {"paths": {}}
    module = kernel()
    assert module._compare_openapi(base, current, source) == [], (
        "Original standard scope stays narrow"
    )
    issues = module._compare_openapi(base, current, source, standard_only=False)
    assert any("path removed" in issue and path in issue for issue in issues)


@pytest.mark.parametrize("composed", [False, True])
def test_full_http_mode_compares_nexus_inside_plain_and_exclusive_request(composed: bool) -> None:
    schema: dict[str, Any] = {
        "type": "object",
        "properties": {"nexus": {"type": "string", "enum": ["a", "b"]}},
    }
    changed = copy.deepcopy(schema)
    changed["properties"]["nexus"]["enum"] = ["a"]
    if composed:
        schema, changed = ({"oneOf": [value, {"type": "null"}]} for value in (schema, changed))
    base, current = document("/v1/audio/speech", schema), document("/v1/audio/speech", changed)
    module = kernel()
    assert module._compare_openapi(base, current, "synthetic.openapi.yaml") == []
    findings = module._compare_openapi(base, current, "synthetic.openapi.yaml", standard_only=False)
    assert findings and any(("oneOf" if composed else "nexus") in issue for issue in findings)


def test_original_standard_mode_retains_nexus_exclusion() -> None:
    path = "/v1/audio/speech"
    base = document(path, {"type": "object", "properties": {"nexus": {"type": "string"}}})
    current = document(path, {"type": "object"})
    assert kernel()._compare_openapi(base, current, "speech-openapi.yaml") == []


@pytest.mark.parametrize(
    "change",
    [
        "request-bound",
        "request-enum",
        "parameter",
        "operation",
        "status",
        "response-required",
        "response-enum",
        "default",
        "header",
        "boolean",
    ],
)
def test_selected_http_structure_breaks_are_rejected(change: str) -> None:
    path = "/v1/workbench/projects"
    base = document(
        path,
        {
            "type": "object",
            "properties": {
                "input": {"type": "string", "minLength": 1, "enum": ["a", "b"], "default": "a"}
            },
        },
        {
            "type": "object",
            "required": ["state"],
            "properties": {"state": {"type": "string", "enum": ["ready"]}},
        },
    )
    base["paths"][path]["post"]["responses"]["201"]["headers"] = {
        "X-Result": {"required": True, "schema": {"type": "string"}}
    }
    current = copy.deepcopy(base)
    operation = current["paths"][path]["post"]
    request = operation["requestBody"]["content"]["application/json"]["schema"]
    response = operation["responses"]["201"]["content"]["application/json"]["schema"]
    if change == "request-bound":
        request["properties"]["input"]["minLength"] = 2
    elif change == "request-enum":
        request["properties"]["input"]["enum"] = ["a"]
    elif change == "parameter":
        operation["parameters"] = [
            {"name": "revision", "in": "query", "required": True, "schema": {"type": "string"}}
        ]
    elif change == "operation":
        current["paths"][path] = {}
    elif change == "status":
        operation["responses"] = {}
    elif change == "response-required":
        response["required"] = []
    elif change == "response-enum":
        response["properties"]["state"]["enum"] = ["ready", "unknown"]
    elif change == "default":
        request["properties"]["input"]["default"] = "b"
    elif change == "header":
        operation["responses"]["201"]["headers"]["X-Result"]["required"] = False
    elif change == "boolean":
        operation["requestBody"]["content"]["application/json"]["schema"] = False
    assert kernel()._compare_openapi(base, current, "synthetic.openapi.yaml", standard_only=False)


def test_known_safe_changes_and_annotations_do_not_fail_the_check() -> None:
    path = "/v1/workbench/projects"
    base = document(path, {"type": "string", "enum": ["a"]}, {"type": "string", "enum": ["a", "b"]})
    current = document(
        path, {"type": "string", "enum": ["a", "b"]}, {"type": "string", "enum": ["a"]}
    )
    current["description"] = "Updated explanation"
    current["paths"]["/v1/workbench/new"] = copy.deepcopy(current["paths"][path])
    assert (
        kernel()._compare_openapi(base, current, "synthetic.openapi.yaml", standard_only=False)
        == []
    )


def _git(root: Path, *args: str) -> str:
    return subprocess.check_output(
        [
            "git",
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "commit.gpgSign=false",
            "-C",
            str(root),
            *args,
        ],
        text=True,
        stderr=subprocess.PIPE,
    ).strip()


def _write(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value) if path.suffix == ".json" else yaml.safe_dump(value), encoding="utf-8"
    )


@pytest.fixture
def repository(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[ModuleType, Path, str]:
    spec = importlib.util.spec_from_file_location(
        "http_change_cli", ROOT / "scripts/dev-env/check-http-contract-changes.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "_ROOT", tmp_path)
    for source, path in SOURCES:
        _write(tmp_path / source, document(path))
    shared = {
        "$defs": {
            "Input": {
                "type": "object",
                "properties": {"nexus": {"type": "string", "enum": ["a", "b"]}},
            }
        }
    }
    _write(tmp_path / "contracts/shared/synthetic.schema.json", shared)
    _write(
        tmp_path / SOURCES[0][0],
        document(SOURCES[0][1], {"$ref": "shared/synthetic.schema.json#/$defs/Input"}),
    )
    _git(tmp_path, "init")
    _git(tmp_path, "config", "user.name", "Synthetic contract test")
    _git(tmp_path, "config", "user.email", "synthetic@example.invalid")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-m", "Synthetic frozen baseline")
    return module, tmp_path, _git(tmp_path, "rev-parse", "HEAD")


def test_cli_reports_versions_and_supported_scope_without_claiming_complete_compatibility(
    repository: tuple[ModuleType, Path, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    module, _, base = repository
    monkeypatch.setattr(sys, "argv", ["check", "--base", base, "--format", "json"])
    assert module.main() == 0
    report = json.loads(capsys.readouterr().out)
    assert report["baseline"] == base and report["findings"] == []
    assert {row["path"] for row in report["sources"]} == {source for source, _ in SOURCES}
    assert all(
        row["base_version"] == row["current_version"] == "0.1.0.dev0" for row in report["sources"]
    )
    assert any("authorization" in item for item in report["unverified"])
    assert any("standalone JSON" in item for item in report["unverified"])


def test_external_nexus_changes_use_the_frozen_baseline_even_with_a_version_bump(
    repository: tuple[ModuleType, Path, str],
) -> None:
    module, root, base = repository
    source = root / "contracts/shared/synthetic.schema.json"
    value = yaml.safe_load(source.read_text())
    value["$defs"]["Input"]["properties"]["nexus"]["enum"] = ["a"]
    _write(source, value)
    admin = yaml.safe_load((root / SOURCES[0][0]).read_text())
    admin["info"]["version"] = "0.2.0.dev0"
    _write(root / SOURCES[0][0], admin)
    report = module.inspect_sources(base)
    assert any("nexus" in item and "enum" in item for item in report["findings"])
    assert report["sources"][0]["current_version"] == "0.2.0.dev0"


@pytest.mark.parametrize(
    "fault", ["missing-current", "invalid-yaml", "invalid-root", "missing-baseline"]
)
def test_incomplete_or_invalid_frozen_sources_cannot_pass(
    repository: tuple[ModuleType, Path, str],
    fault: str,
) -> None:
    module, root, base = repository
    file = root / SOURCES[1][0]
    original = file.read_text()
    if fault == "missing-current":
        file.unlink()
    elif fault == "invalid-yaml":
        file.write_text("[")
    elif fault == "invalid-root":
        file.write_text("null")
    else:
        file.unlink()
        _git(root, "add", "--all")
        _git(root, "commit", "-m", "Synthetic missing baseline source")
        base = _git(root, "rev-parse", "HEAD")
        file.write_text(original)
    assert module.inspect_sources(base)["findings"]


def test_bad_git_baseline_fails_closed(
    repository: tuple[ModuleType, Path, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    module, _, _ = repository
    monkeypatch.setattr(sys, "argv", ["check", "--base", "synthetic-missing-ref"])
    assert module.main() == 2
    assert "baseline or kernel unavailable" in capsys.readouterr().err


def test_a_moving_baseline_cannot_hide_a_shared_schema_edit(
    repository: tuple[ModuleType, Path, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module, root, base = repository
    load: Callable[[], ModuleType] = module._kernel

    def move_head() -> ModuleType:
        source = root / "contracts/shared/synthetic.schema.json"
        value = yaml.safe_load(source.read_text())
        value["$defs"]["Input"]["properties"]["nexus"]["enum"] = ["a"]
        _write(source, value)
        _git(root, "add", ".")
        _git(root, "commit", "-m", "Synthetic branch advanced during inspection")
        return load()

    monkeypatch.setattr(module, "_kernel", move_head)
    report = module.inspect_sources("HEAD")
    assert report["baseline"] == base != _git(root, "rev-parse", "HEAD")
    assert any("nexus" in item for item in report["findings"])


@pytest.mark.parametrize("fault", ["remote", "pointer", "escape", "recursive"])
def test_unprovable_reference_is_not_accepted_even_when_unchanged(
    repository: tuple[ModuleType, Path, str],
    fault: str,
) -> None:
    module, root, _ = repository
    path = root / SOURCES[0][0]
    value = yaml.safe_load(path.read_text())
    reference = {
        "remote": "https://deployment.invalid/schema.json",
        "pointer": "#/components/schemas/Missing",
        "escape": "../outside.json",
        "recursive": "#/components/schemas/Recursive",
    }[fault]
    value["paths"][SOURCES[0][1]]["post"]["requestBody"]["content"]["application/json"][
        "schema"
    ] = {"$ref": reference}
    if fault == "recursive":
        value["components"] = {"schemas": {"Recursive": {"$ref": reference}}}
    if fault == "escape":
        _write(root / "outside.json", {"type": "string"})
    _write(path, value)
    _git(root, "add", ".")
    _git(root, "commit", "-m", "Synthetic unsupported reference baseline")
    report = module.inspect_sources(_git(root, "rev-parse", "HEAD"))
    assert report["findings"], "An unchanged unsupported reference is not proof of compatibility"


def test_ci_checks_the_fetched_base_in_the_existing_python_job() -> None:
    jobs = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())["jobs"]
    steps = jobs["python_checks"]["steps"]
    step = next(
        value
        for value in steps
        if value["name"] == "Check known structural changes to versioned HTTP contracts"
    )
    assert step["if"] == "github.event_name == 'pull_request'"
    assert step["env"] == {"CONTRACT_BASE_REF": "origin/${{ github.base_ref }}"}
    assert step["run"] == "make contracts-http-changes"
    fetch = next(
        value for value in steps if value["name"] == "Fetch only the compatibility baseline"
    )
    assert steps.index(fetch) < steps.index(step)
    assert jobs["python"]["name"] == "Python quality and tests"
    assert jobs["integration_shards"]["strategy"]["matrix"]["shard"] == list(range(6))


def test_make_rejects_an_omitted_comparison_baseline() -> None:
    import os

    env = {key: value for key, value in os.environ.items() if key != "CONTRACT_BASE_REF"}
    result = subprocess.run(
        ["make", "-s", "contracts-http-changes"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0 and "CONTRACT_BASE_REF is required" in result.stderr


@pytest.mark.parametrize("broken", [False, True])
def test_default_ci_text_mode_reports_the_result_and_returns_a_failure_for_breaks(
    repository: tuple[ModuleType, Path, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    broken: bool,
) -> None:
    module, root, base = repository
    if broken:
        path = root / SOURCES[1][0]
        value = yaml.safe_load(path.read_text())
        value["paths"] = {}
        _write(path, value)
    monkeypatch.setattr(sys, "argv", ["check", "--base", base])
    assert module.main() == (1 if broken else 0)
    text = capsys.readouterr().out
    assert base in text and all(source in text for source, _ in SOURCES)
    assert ("path removed" in text) is broken
    assert "Not a full protocol compatibility proof" in text


@pytest.mark.parametrize("standard_only", [False, True])
def test_boolean_ref_sibling_type_narrowing_rejects_previously_accepted_42(
    standard_only: bool,
) -> None:
    from jsonschema import Draft202012Validator

    path = "/v1/audio/speech"
    base = document(path, {"$ref": "#/components/schemas/Any"})
    base["components"] = {"schemas": {"Any": True}}
    current = copy.deepcopy(base)
    current["paths"][path]["post"]["requestBody"]["content"]["application/json"]["schema"][
        "type"
    ] = "string"
    old_schema = {"components": base["components"], "$ref": "#/components/schemas/Any"}
    new_schema = {**old_schema, "type": "string"}
    assert Draft202012Validator(old_schema).is_valid(42)
    assert not Draft202012Validator(new_schema).is_valid(42)
    assert Draft202012Validator(new_schema).is_valid("synthetic")
    findings = kernel()._compare_openapi(
        base, current, "synthetic.openapi.yaml", standard_only=standard_only
    )
    assert findings, "Resolved true must not discard an adjacent type restriction"


@pytest.mark.parametrize("standard_only", [False, True])
@pytest.mark.parametrize("target", [True, False])
def test_boolean_ref_annotation_or_false_intersection_preserves_acceptance(
    standard_only: bool,
    target: bool,
) -> None:
    from jsonschema import Draft202012Validator

    path = "/v1/audio/speech"
    base = document(path, {"$ref": "#/components/schemas/Any"})
    base["components"] = {"schemas": {"Any": target}}
    current = copy.deepcopy(base)
    sibling = {"description": "Synthetic explanation"} if target else {"type": "string"}
    current["paths"][path]["post"]["requestBody"]["content"]["application/json"]["schema"].update(
        sibling
    )
    for value in (42, "synthetic", None):
        old = {"components": base["components"], "$ref": "#/components/schemas/Any"}
        assert Draft202012Validator(old).is_valid(value) == Draft202012Validator(
            {**old, **sibling}
        ).is_valid(value)
    assert (
        kernel()._compare_openapi(
            base, current, "synthetic.openapi.yaml", standard_only=standard_only
        )
        == []
    )


def test_cli_rejects_boolean_reference_sibling_narrowing_at_the_same_source_version(
    repository: tuple[ModuleType, Path, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    module, root, _ = repository
    value = document(SOURCES[0][1], {"$ref": "#/components/schemas/Any"})
    value["components"] = {"schemas": {"Any": True}}
    path = root / SOURCES[0][0]
    _write(path, value)
    _git(root, "add", ".")
    _git(root, "commit", "-m", "Synthetic true reference baseline")
    base = _git(root, "rev-parse", "HEAD")
    value["paths"][SOURCES[0][1]]["post"]["requestBody"]["content"]["application/json"]["schema"][
        "type"
    ] = "string"
    _write(path, value)
    monkeypatch.setattr(sys, "argv", ["check", "--base", base, "--format", "json"])
    assert module.main() == 1
    report = json.loads(capsys.readouterr().out)
    assert report["findings"]
    assert report["sources"][0]["base_version"] == report["sources"][0]["current_version"]


def test_false_reference_simplification_keeps_the_closed_reference_policy(
    repository: tuple[ModuleType, Path, str],
) -> None:
    module, root, _ = repository
    value = document(SOURCES[0][1], {"$ref": "#/components/schemas/Never"})
    value["components"] = {"schemas": {"Never": False}}
    path = root / SOURCES[0][0]
    _write(path, value)
    _git(root, "add", ".")
    _git(root, "commit", "-m", "Synthetic false reference baseline")
    base = _git(root, "rev-parse", "HEAD")
    value["paths"][SOURCES[0][1]]["post"]["requestBody"]["content"]["application/json"]["schema"][
        "allOf"
    ] = [{"$ref": "https://deployment.invalid/unavailable.json"}]
    _write(path, value)
    assert module.inspect_sources(base)["findings"]


@pytest.mark.parametrize("as_reference", [False, True])
def test_response_intersection_retains_complete_original_guarantee(as_reference: bool) -> None:
    original = {
        "type": "object",
        "additionalProperties": False,
        "required": ["state"],
        "properties": {"state": {"enum": ["draft", "published"]}},
        "allOf": [{"properties": {"state": {"type": "string"}}}],
    }
    baseline = document("/v1/audio/speech", response=original)
    current = copy.deepcopy(baseline)
    response = current["paths"]["/v1/audio/speech"]["post"]["responses"]["201"]
    if as_reference:
        current["components"] = {"schemas": {"Original": original}}
    response["content"]["application/json"]["schema"] = {
        "allOf": [
            {"$ref": "#/components/schemas/Original"} if as_reference else original,
            {"properties": {"state": {"const": "draft"}}},
        ]
    }
    assert (
        kernel()._compare_openapi(
            baseline, current, "contracts/admin-openapi.yaml", standard_only=False
        )
        == []
    )


@pytest.mark.parametrize("direction", ["request", "response"])
@pytest.mark.parametrize("composition", ["allOf", "anyOf", "oneOf"])
def test_intersection_proof_does_not_allow_lost_guarantee_or_request_narrowing(
    direction: str, composition: str
) -> None:
    original = {
        "type": "object",
        "required": ["state"],
        "properties": {"state": {"type": "string"}},
    }
    changed = copy.deepcopy(original)
    if direction == "response":
        changed.pop("required")
    elif composition != "allOf":
        changed = {"type": "integer"}
    current = {composition: [changed, {"type": "object", "required": ["extra"]}]}
    module = kernel()
    side = module._SchemaSide({}, "contracts/admin-openapi.yaml", None, standard_only=False)
    issues: list[str] = []
    module._compare_schema(
        original,
        current,
        "synthetic",
        issues,
        direction=direction,
        base_side=side,
        current_side=side,
    )
    assert issues


@pytest.mark.parametrize("reference", ["$dynamicRef", "$recursiveRef"])
def test_response_intersection_does_not_prove_dynamic_reference_inclusion(reference: str) -> None:
    module = kernel()
    original = {"type": "object", reference: "#dynamic"}
    current = {"allOf": [original, {"type": "object"}]}
    side = module._SchemaSide({}, "contracts/admin-openapi.yaml", None, standard_only=False)
    issues: list[str] = []
    module._compare_schema(
        original,
        current,
        "synthetic",
        issues,
        direction="response",
        base_side=side,
        current_side=side,
    )
    assert issues


def test_response_intersection_keeps_original_openai_comparison_policy() -> None:
    original = {
        "type": "object",
        "required": ["state"],
        "properties": {"state": {"type": "string"}},
    }
    baseline = document("/v1/audio/speech", response=original)
    current = document("/v1/audio/speech", response={"allOf": [original, {"type": "object"}]})
    assert kernel()._compare_openapi(baseline, current, "contracts/speech-openapi.yaml")


@pytest.mark.parametrize("keyword", ["const", "enum"])
@pytest.mark.parametrize("old,new", [(True, 1), (False, 0), (1, True), (0, False)])
def test_response_intersection_preserves_json_boolean_and_numeric_types(
    keyword: str, old: bool | int, new: bool | int
) -> None:
    from jsonschema import Draft202012Validator

    def schema(value: bool | int) -> dict[str, Any]:
        constraint = [value] if keyword == "enum" else value
        return {
            "type": "object",
            "required": ["value"],
            "properties": {"value": {keyword: constraint}},
        }

    original = schema(old)
    changed = {"allOf": [schema(new), {"type": "object"}]}
    witness = {"value": new}
    assert not Draft202012Validator(original).is_valid(witness)
    assert Draft202012Validator(changed).is_valid(witness)
    baseline = document("/v1/audio/speech", response=original)
    current = document("/v1/audio/speech", response=changed)
    assert kernel()._compare_openapi(
        baseline, current, "contracts/admin-openapi.yaml", standard_only=False
    )


@pytest.mark.parametrize("keyword", ["const", "enum"])
@pytest.mark.parametrize("value", [True, False, 1, 0])
def test_response_intersection_accepts_identical_json_types_and_safe_tightening(
    keyword: str, value: bool | int
) -> None:
    from jsonschema import Draft202012Validator

    original = {
        "type": "object",
        "required": ["value"],
        "properties": {"value": {keyword: [value] if keyword == "enum" else value}},
    }
    tightened = {"allOf": [copy.deepcopy(original), {"required": ["extra"]}]}
    assert Draft202012Validator(original).is_valid({"value": value})
    assert not Draft202012Validator(tightened).is_valid({"value": value})
    assert Draft202012Validator(tightened).is_valid({"value": value, "extra": None})
    assert (
        kernel()._compare_openapi(
            document("/v1/audio/speech", response=original),
            document("/v1/audio/speech", response=tightened),
            "contracts/admin-openapi.yaml",
            standard_only=False,
        )
        == []
    )


@pytest.mark.parametrize("keyword", ["const", "enum"])
def test_response_intersection_never_resolves_literal_json_references(keyword: str) -> None:
    from jsonschema import Draft202012Validator

    literal = {"$ref": "#/components/schemas/A"}
    expanded = {"type": "string"}
    original = {"type": "object", keyword: [literal] if keyword == "enum" else literal}
    changed = {
        "allOf": [
            {"type": "object", keyword: [expanded] if keyword == "enum" else expanded},
            {"type": "object"},
        ]
    }
    assert not Draft202012Validator(original).is_valid(expanded)
    assert Draft202012Validator(changed).is_valid(expanded)
    baseline = document("/v1/audio/speech", response=original)
    current = document("/v1/audio/speech", response=changed)
    for source in (baseline, current):
        source["components"] = {"schemas": {"A": {"type": "string"}}}
    assert kernel()._compare_openapi(
        baseline, current, "contracts/admin-openapi.yaml", standard_only=False
    )


def test_schema_reference_in_property_named_const_is_still_resolved() -> None:
    original = {
        "type": "object",
        "required": ["const"],
        "properties": {"const": {"$ref": "#/components/schemas/A"}},
    }
    baseline = document("/v1/audio/speech", response=original)
    current = document(
        "/v1/audio/speech", response={"allOf": [copy.deepcopy(original), {"required": ["extra"]}]}
    )
    for source in (baseline, current):
        source["components"] = {"schemas": {"A": {"type": "string"}}}
    assert (
        kernel()._compare_openapi(
            baseline, current, "contracts/admin-openapi.yaml", standard_only=False
        )
        == []
    )


@pytest.mark.parametrize("boolean,number", [(True, 1), (False, 0)])
def test_reference_sibling_intersection_preserves_json_types_before_proof(
    boolean: bool, number: int
) -> None:
    from jsonschema import Draft202012Validator

    components = {"schemas": {"A": {"const": number}}}
    original = {"$ref": "#/components/schemas/A", "const": boolean}
    changed = {"allOf": [{"const": boolean}, {"type": "boolean"}]}
    assert not Draft202012Validator({**original, "components": components}).is_valid(boolean)
    assert Draft202012Validator(changed).is_valid(boolean)
    baseline = document("/v1/audio/speech", response=original)
    current = document("/v1/audio/speech", response=changed)
    baseline["components"] = components
    assert kernel()._compare_openapi(
        baseline, current, "contracts/admin-openapi.yaml", standard_only=False
    )
