"""Workspace read contracts are portable; route implementation remains separate."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any, Literal, cast

import pytest
from jsonschema import Draft202012Validator, FormatChecker
from openapi_spec_validator import validate
from referencing import Registry
from speechnexus_contracts import ContractValidationError
from speechnexus_contracts.admin import parse
from speechnexus_contracts.admin_http import contract, validate_request, validate_response

from tests.contract.test_binding_entrypoints import generator
from tests.support.contracts import document, retrieve

ROOT = Path(__file__).resolve().parents[2]
EXAMPLES = json.loads((ROOT / "tests/fixtures/contracts/admin-http.json").read_text())["examples"]
CONFIG = json.loads((ROOT / "contracts/bindings.json").read_text())["http_surfaces"]["admin"]


SOURCE = document(ROOT / "contracts/admin-openapi.yaml")
HTTP_METHODS = frozenset({"get", "post", "put", "patch", "delete", "head", "options", "trace"})


def resolve(node: dict[str, Any]) -> dict[str, Any]:
    while node.get("$ref", "").startswith("#/components/"):
        reference = node["$ref"]
        node = SOURCE
        for token in reference[2:].split("/"):
            node = node[token]
    return node


def operation_source(operation_id: str) -> dict[str, Any]:
    return next(
        item[method]
        for item in SOURCE["paths"].values()
        for method in HTTP_METHODS
        if isinstance(item.get(method), dict) and item[method].get("operationId") == operation_id
    )


def source_accepts(example: dict[str, Any]) -> bool:
    """Read original OpenAPI constraints independently of the generated bundle."""
    operation = operation_source(example["operation"])
    value = example["value"]

    def valid(schema: dict[str, Any], candidate: Any) -> bool:
        return Draft202012Validator(
            {"components": SOURCE["components"], **schema},
            registry=cast(Any, Registry)(retrieve=retrieve),
            format_checker=FormatChecker(),
        ).is_valid(candidate)

    if example["kind"] == "response":
        response = operation["responses"].get(str(example["status"]))
        if response is None:
            return False
        schema = resolve(response)["content"]["application/json"]["schema"]
        return "body" in value and valid(schema, value["body"])
    # Only the declared body media type may carry a body; a read declares none.
    groups_declared = {"path", "query", "headers"}
    if "requestBody" in operation:
        groups_declared.add("body")
    if set(value) - groups_declared:
        return False
    headers = value.get("headers", {})
    if len({key.lower() for key in headers}) != len(headers):
        return False
    groups = {**value, "headers": {key.lower(): item for key, item in headers.items()}}
    declared: dict[str, set[str]] = {"path": set(), "query": set(), "headers": set()}
    for raw in operation.get("parameters", []):
        parameter = resolve(raw)
        location = "headers" if parameter["in"] == "header" else parameter["in"]
        name = parameter["name"].lower() if location == "headers" else parameter["name"]
        declared[location].add(name)
        if name not in groups.get(location, {}):
            if parameter.get("required"):
                return False
            continue
        candidate = groups[location][name]
        schema = parameter["schema"]
        if resolve(schema).get("type") == "integer" and isinstance(candidate, str):
            try:
                number = int(candidate)
            except ValueError:
                return False
            if str(number) != candidate:
                return False
            candidate = number
        if not valid(schema, candidate):
            return False
    if "requestBody" in operation:
        body = operation["requestBody"]["content"]["application/json"]["schema"]
        if "body" not in value or not valid(resolve(body), value["body"]):
            return False
    elif "body" in value:
        return False
    return all(not (set(value.get(key, {})) - declared[key]) for key in ("path", "query"))


def test_only_selected_operations_are_closed_without_changing_source_defaults() -> None:
    actual = contract()
    assert actual == generator().bundle_http_surface(CONFIG)
    validate(actual)
    assert set(actual["paths"]) == set(CONFIG["paths"])
    assert set(actual["paths"]["/v1/nexus/admin/resource-pools"]) == {"get"}
    assert set(actual["paths"]["/v1/nexus/admin/model-bindings"]) == {"get"}
    assert set(actual["paths"]["/v1/nexus/admin/model-instances"]) == {"get"}
    assert set(actual["paths"]["/v1/nexus/admin/capacity-manifests/{manifest_id}"]) == {"get"}
    assert set(actual["paths"]["/v1/nexus/admin/qualification-reports/{report_id}"]) == {"get"}
    assert set(actual["paths"]["/v1/nexus/admin/diagnostics"]) == {"get"}
    assert set(
        actual["paths"]["/v1/nexus/admin/model-instances/{instance_id}/generations/{generation}"]
    ) == {"get"}
    assert set(actual["paths"]["/v1/nexus/audit-events"]) == {"get"}
    assert set(
        actual["paths"]["/v1/nexus/admin/admission-leases/{lease_id}/quarantine-releases"]
    ) == {"post"}
    # The workspace collection carries the read and the create; the selected
    # revision path carries the read and the update. Nothing else is closed.
    assert set(actual["paths"]["/v1/nexus/workspaces"]) == {"get", "post"}
    assert set(actual["paths"]["/v1/nexus/workspaces/{workspace_id}"]) == {"get", "patch"}
    assert actual["components"]["parameters"]["Limit"]["schema"]["default"] == 50
    assert actual["security"] == [{"SharedServiceKey": []}, {}]
    actual["paths"].clear()
    assert len(contract()["paths"]) == 11


@pytest.mark.parametrize("example", EXAMPLES, ids=lambda example: example["id"])
def test_python_transport_preserves_shared_examples(example: dict[str, Any]) -> None:
    assert source_accepts(example) is example["valid"]
    value = copy.deepcopy(example["value"])

    def check() -> Any:
        if example["kind"] == "request":
            return validate_request(example["operation"], value)
        return validate_response(example["operation"], example["status"], value)

    if example["valid"]:
        assert check() is value
        if example["kind"] == "response":
            name: Literal["ErrorResponse", "WorkspaceList", "Workspace"] = (
                "ErrorResponse"
                if example["status"] >= 400
                else "WorkspaceList"
                if example["operation"] == "listWorkspaces"
                else "Workspace"
            )
            assert parse(name, value["body"]) is value["body"]
    else:
        with pytest.raises(ContractValidationError):
            check()
    assert value == example["value"]


@pytest.mark.parametrize("operation", ["publishProfileRevision", "publishModelBindingRevision"])
def test_unselected_operations_are_rejected(operation: str) -> None:
    with pytest.raises(ValueError, match="Unknown HTTP operation"):
        validate_request(operation, {})


@pytest.mark.parametrize(
    "operations", [None, [], "listWorkspaces", [""], [3], ["listWorkspaces"] * 2]
)
def test_invalid_operation_selectors_fail_closed(operations: Any) -> None:
    with pytest.raises(ValueError, match="unique nonempty operation IDs"):
        generator().bundle_http_surface(CONFIG | {"operations": operations})


@pytest.mark.parametrize("operation", ["unknownOperation", "publishModelBindingRevision"])
def test_selection_cannot_escape_reviewed_paths(operation: str) -> None:
    with pytest.raises(ValueError, match="outside the reviewed paths"):
        generator().bundle_http_surface(CONFIG | {"operations": [operation]})


def test_path_metadata_survives_and_duplicate_or_unsupported_operations_fail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = generator()
    monkeypatch.setattr(module, "CONTRACTS", tmp_path)
    source: dict[str, Any] = {
        "openapi": "3.1.0",
        "info": {"title": "Synthetic", "version": "1"},
        "paths": {
            "/synthetic": {
                "parameters": [
                    {"in": "header", "name": "X-Synthetic", "schema": {"type": "string"}}
                ],
                "get": {
                    "operationId": "readSynthetic",
                    "responses": {"204": {"description": "Empty"}},
                },
                "post": {
                    "operationId": "writeSynthetic",
                    "responses": {"204": {"description": "Empty"}},
                },
            }
        },
    }
    path = tmp_path / "synthetic.json"
    config = {"source": path.name, "paths": ["/synthetic"], "operations": ["readSynthetic"]}
    path.write_text(json.dumps(source))
    actual = module.bundle_http_surface(config)
    assert set(actual["paths"]["/synthetic"]) == {"parameters", "get"}
    assert (
        actual["paths"]["/synthetic"]["parameters"] == source["paths"]["/synthetic"]["parameters"]
    )
    source["paths"]["/synthetic"]["post"]["operationId"] = "readSynthetic"
    path.write_text(json.dumps(source))
    with pytest.raises(ValueError, match="Duplicate selected HTTP operation"):
        module.bundle_http_surface(config)
    source["paths"]["/synthetic"]["trace"] = source["paths"]["/synthetic"].pop("post")
    del source["paths"]["/synthetic"]["get"]
    path.write_text(json.dumps(source))
    with pytest.raises(ValueError, match="unsupported by validation"):
        module.bundle_http_surface(config)
