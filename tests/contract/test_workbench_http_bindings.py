"""Source/Python/TS share transport acceptance, without implementing C's routes."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any, cast

import pytest
from jsonschema import Draft202012Validator, FormatChecker
from openapi_spec_validator import validate
from referencing import Registry
from speechnexus_contracts import ContractValidationError
from speechnexus_contracts.http_validation import validate_http_request
from speechnexus_contracts.workbench_http import contract, validate_request, validate_response

from tests.contract.test_binding_entrypoints import generator
from tests.support.contracts import document, retrieve

ROOT = Path(__file__).resolve().parents[2]
EXAMPLES = json.loads((ROOT / "tests/fixtures/contracts/workbench-http.json").read_text())[
    "examples"
]
SOURCE = document(ROOT / "contracts/workbench-openapi.yaml")


def resolve(node: dict[str, Any]) -> dict[str, Any]:
    while node.get("$ref", "").startswith("#/components/"):
        pointer = node["$ref"]
        node = SOURCE
        for token in pointer[2:].split("/"):
            node = node[token]
    return node


def valid_schema(schema: dict[str, Any], value: Any) -> bool:
    return Draft202012Validator(
        {"components": SOURCE["components"], **schema},
        registry=cast(Any, Registry)(retrieve=retrieve),
        format_checker=FormatChecker(),
    ).is_valid(value)


def source_accepts(example: dict[str, Any]) -> bool:
    operation = next(
        operation
        for path in SOURCE["paths"].values()
        for operation in path.values()
        if isinstance(operation, dict) and operation.get("operationId") == example["operation"]
    )
    value = example["value"]
    if example["kind"] == "request":
        # Strict envelope/duplicate query policies are documented by this boundary.
        if set(value) - {"path", "query", "headers", "body"}:
            return False
        declared: dict[str, set[str]] = {"path": set(), "query": set(), "headers": set()}
        headers = value.get("headers", {})
        if len({key.lower() for key in headers}) != len(headers):
            return False
        values = {**value, "headers": {key.lower(): item for key, item in headers.items()}}
        for raw in operation.get("parameters", []):
            parameter = resolve(raw)
            location = "headers" if parameter["in"] == "header" else parameter["in"]
            key = parameter["name"].lower() if location == "headers" else parameter["name"]
            declared[location].add(key)
            group = values.get(location, {})
            if key not in group:
                if parameter.get("required"):
                    return False
                continue
            item = group[key]
            schema = parameter["schema"]
            if resolve(schema).get("type") == "integer" and isinstance(item, str):
                try:
                    number = int(item)
                except ValueError:
                    return False
                if str(number) != item or abs(number) > 2**53 - 1:
                    return False
                item = number
            if not valid_schema(schema, item):
                return False
        for location in ("path", "query"):
            if set(value.get(location, {})) - declared[location]:
                return False
        body = operation.get("requestBody")
        if not body:
            return "body" not in value
        body = resolve(body)
        if "body" not in value:
            return not body.get("required", False)
        return valid_schema(body["content"]["application/json"]["schema"], value["body"])
    status = example["status"]
    if type(status) is not int or str(status) not in operation["responses"]:
        return False
    response = resolve(operation["responses"][str(status)])
    headers = {key.lower(): item for key, item in value.get("headers", {}).items()}
    for key, raw in response.get("headers", {}).items():
        header = resolve(raw)
        if key.lower() not in headers:
            if header.get("required"):
                return False
            continue
        item = headers[key.lower()]
        if resolve(header["schema"]).get("type") == "integer" and isinstance(item, str):
            try:
                number = int(item)
            except ValueError:
                return False
            if str(number) != item or abs(number) > 2**53 - 1:
                return False
            item = number
        if not valid_schema(header["schema"], item):
            return False
    return "body" in value and valid_schema(
        response["content"]["application/json"]["schema"], value["body"]
    )


@pytest.mark.parametrize("example", EXAMPLES, ids=lambda example: example["id"])
def test_http_source_and_python_agree_without_mutating_input(example: dict[str, Any]) -> None:
    assert source_accepts(example) is example["valid"]
    value = copy.deepcopy(example["value"])

    def call() -> Any:
        if example["kind"] == "request":
            return validate_request(example["operation"], value)
        return validate_response(example["operation"], example["status"], value)

    if example["valid"]:
        assert call() is value
    else:
        with pytest.raises(ContractValidationError, match=f"^Invalid HTTP {example['kind']}$"):
            call()
    assert value == example["value"]


def test_http_document_is_closed_isolated_and_only_declares_selected_paths() -> None:
    actual = contract()
    validate(actual)
    manifest = json.loads((ROOT / "contracts/bindings.json").read_text())
    assert set(actual["paths"]) == set(manifest["http_surfaces"]["workbench"]["paths"])
    assert len(actual["paths"]) == 40
    names: set[str] = set()
    for item in actual["paths"].values():
        for operation in item.values():
            names.add(operation["operationId"])
    for name in names:
        assert any(e["operation"] == name and e["valid"] for e in EXAMPLES), name
        assert any(e["operation"] == name and not e["valid"] for e in EXAMPLES), name
    # A bodyless read has no request example to give; every operation that accepts a
    # request body must still show one accepted request.
    for item in actual["paths"].values():
        for operation in item.values():
            if "requestBody" not in operation:
                continue
            name = operation["operationId"]
            assert any(
                e["operation"] == name and e["kind"] == "request" and e["valid"] for e in EXAMPLES
            ), name
    actual["paths"].clear()
    assert len(contract()["paths"]) == 40


def test_unknown_profile_or_operation_never_selects_user_supplied_files() -> None:
    with pytest.raises(ValueError, match="Unknown HTTP contract profile"):
        validate_http_request("../../private", "getProject", {})
    with pytest.raises(ValueError, match="Unknown HTTP operation"):
        validate_request("RuntimeInstanceIntentRequest", {})
    with pytest.raises(ValueError, match="Unknown HTTP operation"):
        validate_response("createAssistantTurnV2", 200, {})


def test_optional_request_body_and_no_content_response_follow_openapi_defaults() -> None:
    source: dict[str, Any] = {
        "components": {"schemas": {"Payload": {"type": "object"}}},
        "paths": {
            "/synthetic": {
                "post": {
                    "operationId": "optionalBody",
                    "requestBody": {
                        "content": {
                            "application/json": {"schema": {"$ref": "#/components/schemas/Payload"}}
                        }
                    },
                    "responses": {"204": {"description": "No content"}},
                }
            }
        },
    }
    bundle = cast(Any, generator().bundle_http_validation)(source)

    def valid(name: str, value: Any) -> bool:
        return Draft202012Validator({"$defs": bundle["$defs"], "$ref": f"#/$defs/{name}"}).is_valid(
            value
        )

    assert valid("HttpRequest_optionalBody", {})
    assert valid("HttpRequest_optionalBody", {"body": {}})
    assert valid("HttpResponse_optionalBody_204", {})
    assert not valid("HttpResponse_optionalBody_204", {"body": {}})
