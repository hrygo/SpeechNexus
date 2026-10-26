"""JSON literals retain boolean/number distinctions throughout the HTTP gate."""

from __future__ import annotations

import copy
import json
import shutil
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Any, cast

import pytest
from jsonschema import Draft202012Validator

from tests.contract.test_http_contract_changes import (
    ROOT,
    SOURCES,
    _git,
    _write,
    document,
    kernel,
)
from tests.contract.test_http_contract_changes import repository as repository

PATH = "/v1/audio/speech"


def literal_schema(keyword: str, value: Any) -> dict[str, Any]:
    return {
        "type": "object",
        "required": ["value"],
        "properties": {"value": {keyword: [value] if keyword == "enum" else value}},
    }


def wire_document(schema: dict[str, Any], direction: str) -> dict[str, Any]:
    return document(PATH, **{direction: schema})


def accepts(doc: dict[str, Any], direction: str, value: Any) -> bool:
    segment = "requestBody" if direction == "request" else "responses/201"
    # Evaluate the exact OpenAPI schema location with its original reference root.
    pointer = "#/paths/~1v1~1audio~1speech/post/" + segment + "/content/application~1json/schema"
    return Draft202012Validator({**doc, "$ref": pointer}).is_valid(value)


def changed_findings(old: dict[str, Any], new: dict[str, Any]) -> list[str]:
    return cast(
        list[str],
        kernel()._compare_openapi(old, new, "contracts/admin-openapi.yaml", standard_only=False),
    )


@pytest.mark.parametrize("keyword", ["const", "enum"])
@pytest.mark.parametrize("old,new", [(True, 1), (False, 0), (1, True), (0, False)])
@pytest.mark.parametrize("direction", ["request", "response"])
def test_plain_http_literals_cannot_merge_booleans_with_numbers(
    keyword: str, old: bool | int, new: bool | int, direction: str
) -> None:
    before = wire_document(literal_schema(keyword, old), direction)
    after = wire_document(literal_schema(keyword, new), direction)
    witness = {"value": old if direction == "request" else new}
    assert accepts(before, direction, witness) == (direction == "request")
    assert accepts(after, direction, witness) == (direction == "response")
    assert changed_findings(before, after)


@pytest.mark.parametrize("keyword", ["const", "enum"])
@pytest.mark.parametrize("direction", ["request", "response"])
@pytest.mark.parametrize("shape", ["exclusive", "nested-literal", "not", "if"])
def test_composition_and_literal_containers_preserve_the_same_distinction(
    keyword: str, direction: str, shape: str
) -> None:
    def schema(value: bool | int) -> dict[str, Any]:
        if shape == "exclusive":
            return {"oneOf": [literal_schema(keyword, value), {"type": "null"}]}
        if shape == "nested-literal":
            return literal_schema(keyword, {"nested": [value]})
        negative = literal_schema(keyword, value)
        return {"not": negative} if shape == "not" else {"if": negative, "then": False}

    before, after = (wire_document(schema(value), direction) for value in (True, 1))
    value = True if direction == "request" else 1
    if shape in ("not", "if"):
        value = 1 if direction == "request" else True
    witness = {"value": {"nested": [value]} if shape == "nested-literal" else value}
    assert accepts(before, direction, witness) == (direction == "request")
    assert accepts(after, direction, witness) == (direction == "response")
    assert changed_findings(before, after)


@pytest.mark.parametrize("direction", ["request", "response"])
@pytest.mark.parametrize("nested", [False, True])
def test_defaults_keep_boolean_and_number_values_distinct(direction: str, nested: bool) -> None:
    old, new = ({"nested": [False]}, {"nested": [0]}) if nested else (False, 0)
    # Defaults are annotations; both remain valid JSON values, but different defaults.
    before, after = (wire_document({"default": value}, direction) for value in (old, new))
    assert json.dumps(old) != json.dumps(new)
    assert accepts(before, direction, old) and accepts(after, direction, new)
    assert any("default" in item for item in changed_findings(before, after))


@pytest.mark.parametrize("keyword", ["const", "enum", "default"])
@pytest.mark.parametrize("direction", ["request", "response"])
@pytest.mark.parametrize("composed", [False, True])
def test_json_number_equivalence_and_annotation_changes_stay_compatible(
    keyword: str, direction: str, composed: bool
) -> None:
    before_schema = literal_schema(keyword, {"nested": [1]})
    after_schema = literal_schema(keyword, {"nested": [1.0]})
    after_schema["description"] = "Synthetic annotation only."
    if composed:
        before_schema = {"oneOf": [before_schema, {"type": "null"}]}
        after_schema = {"oneOf": [after_schema, {"type": "null"}]}
    before, after = wire_document(before_schema, direction), wire_document(after_schema, direction)
    assert accepts(before, direction, {"value": {"nested": [1]}})
    assert accepts(after, direction, {"value": {"nested": [1]}})
    assert changed_findings(before, after) == []


@pytest.mark.parametrize("standard_only", [False, True])
def test_ref_sibling_intersection_cannot_discard_an_incompatible_boolean_literal(
    standard_only: bool,
) -> None:
    before = document(PATH, response={"$ref": "#/components/schemas/Original", "const": 1})
    before["components"] = {"schemas": {"Original": {"const": True}}}
    after = document(PATH, response={"const": 1})
    assert not accepts(before, "response", 1) and accepts(after, "response", 1)
    assert kernel()._compare_openapi(
        before, after, "contracts/speech-openapi.yaml", standard_only=standard_only
    )


@pytest.mark.parametrize("numeric_equivalent", [False, True])
def test_real_http_cli_uses_literal_comparison_even_after_a_version_bump(
    repository: tuple[ModuleType, Path, str], numeric_equivalent: bool
) -> None:
    _, root, _ = repository
    source, path = SOURCES[0]
    original = document(path, response=literal_schema("enum", 1 if numeric_equivalent else True))
    _write(root / source, original)
    _git(root, "add", ".")
    _git(root, "commit", "-m", "Synthetic literal baseline")
    base = _git(root, "rev-parse", "HEAD")
    changed = copy.deepcopy(original)
    changed["info"]["version"] = "0.2.0.dev0"
    changed["paths"][path]["post"]["responses"]["201"]["content"]["application/json"]["schema"] = (
        literal_schema("enum", 1.0 if numeric_equivalent else 1)
    )
    _write(root / source, changed)
    scripts = root / "scripts/dev-env"
    scripts.mkdir(parents=True)
    for name in ("check-http-contract-changes.py", "check-openai-contract-compat.py"):
        shutil.copyfile(ROOT / "scripts/dev-env" / name, scripts / name)
    result = subprocess.run(
        [
            sys.executable,
            str(scripts / "check-http-contract-changes.py"),
            "--base",
            base,
            "--format",
            "json",
        ],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == (0 if numeric_equivalent else 1), result.stderr
    report = json.loads(result.stdout)
    assert bool(report["findings"]) is not numeric_equivalent
    assert report["unverified"]


@pytest.mark.parametrize("standard_only", [False, True])
def test_ref_sibling_comparison_preserves_original_nexus_exclusion(standard_only: bool) -> None:
    before = document(
        PATH,
        request={
            "$ref": "#/components/schemas/Original",
            "properties": {"nexus": {"const": False}},
        },
    )
    before["components"] = {
        "schemas": {"Original": {"type": "object", "properties": {"nexus": {"const": False}}}}
    }
    after = copy.deepcopy(before)
    after["paths"][PATH]["post"]["requestBody"]["content"]["application/json"]["schema"][
        "properties"
    ]["nexus"]["const"] = 0
    assert accepts(before, "request", {"nexus": False})
    assert not accepts(after, "request", {"nexus": False})
    findings = kernel()._compare_openapi(
        before, after, "contracts/speech-openapi.yaml", standard_only=standard_only
    )
    assert bool(findings) is not standard_only


@pytest.mark.parametrize("keyword", ["const", "enum", "default"])
@pytest.mark.parametrize("standard_only", [False, True])
@pytest.mark.parametrize("literal_key", ["description", "properties"])
def test_literal_keys_are_data_even_inside_exclusive_variants(
    keyword: str, standard_only: bool, literal_key: str
) -> None:
    def schema(value: bool | int) -> dict[str, Any]:
        literal = (
            {"description": value}
            if literal_key == "description"
            else {"properties": {"nexus": value}}
        )
        return {"oneOf": [literal_schema(keyword, literal), {"type": "null"}]}

    before, after = (document(PATH, response=schema(value)) for value in (True, 1))
    assert kernel()._compare_openapi(
        before, after, "contracts/speech-openapi.yaml", standard_only=standard_only
    )
