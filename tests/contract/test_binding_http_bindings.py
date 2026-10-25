"""Independent source acceptance and consumer validation; no binding HTTP service."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any, cast
from urllib.parse import unquote, urldefrag, urlparse

import httpx
import pytest
from jsonschema import Draft202012Validator, FormatChecker
from openapi_spec_validator import validate
from referencing import Registry
from speechnexus_contracts import ContractValidationError
from speechnexus_contracts.admin_http import contract as workspace_contract
from speechnexus_contracts.binding_http import contract, validate_request, validate_response

from tests.contract.test_binding_entrypoints import generator
from tests.support.contracts import document, retrieve

ROOT = Path(__file__).resolve().parents[2]
SOURCE = document(ROOT / "contracts/admin-openapi.yaml")
EXAMPLES = json.loads((ROOT / "tests/fixtures/contracts/binding-http.json").read_text())["examples"]
CONFIG = json.loads((ROOT / "contracts/bindings.json").read_text())["http_surfaces"]["binding"]
POST, GET = "createModelBindingDraft", "getModelBindingRevision"


def resolve(node: dict[str, Any], source: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Resolve OpenAPI objects while retaining their own component context."""
    seen: set[tuple[str, str]] = set()
    while "$ref" in node:
        uri, fragment = urldefrag(node["$ref"])
        identity = (uri or f"local:{id(source)}", fragment)
        assert identity not in seen, "OpenAPI reference cycle"
        seen.add(identity)
        if uri:
            parsed = urlparse(uri)
            assert parsed.scheme == "file"
            source = document(Path(unquote(parsed.path)))
        assert fragment.startswith("/")
        node = source
        for token in fragment[1:].split("/"):
            node = node[unquote(token).replace("~1", "/").replace("~0", "~")]
    return node, source


def source_accepts(example: dict[str, Any], source: dict[str, Any] = SOURCE) -> bool:
    operation = next(
        operation
        for item in source["paths"].values()
        for operation in item.values()
        if isinstance(operation, dict) and operation.get("operationId") == example["operation"]
    )
    value = example["value"]

    def valid(schema: dict[str, Any], item: Any, context: dict[str, Any]) -> bool:
        return Draft202012Validator(
            {"components": context["components"], **schema},
            registry=cast(Any, Registry)(retrieve=retrieve),
            format_checker=FormatChecker(),
        ).is_valid(item)

    if example["kind"] == "response":
        raw = operation["responses"].get(str(example["status"]))
        if raw is None or set(value) - {"body", "headers"}:
            return False
        response, context = resolve(raw, source)
        if "body" not in value or not valid(
            response["content"]["application/json"]["schema"], value["body"], context
        ):
            return False
        headers = value.get("headers", {})
        if not isinstance(headers, dict) or len({k.lower() for k in headers}) != len(headers):
            return False
        normalized = {key.lower(): item for key, item in headers.items()}
        for key, header in response.get("headers", {}).items():
            schema, header_context = resolve(header, context)
            if schema.get("required") and key.lower() not in normalized:
                return False
            if key.lower() in normalized and not valid(
                schema["schema"], normalized[key.lower()], header_context
            ):
                return False
        return True
    if set(value) - {"path", "query", "headers", "body"}:
        return False
    request_body = operation.get("requestBody")
    if request_body is not None:
        request_body, context = resolve(request_body, source)
        if "body" not in value:
            if request_body.get("required"):
                return False
        elif not valid(
            request_body["content"]["application/json"]["schema"], value["body"], context
        ):
            return False
    elif "body" in value:
        return False
    groups = {key: value.get(key, {}) for key in ("path", "query", "headers")}
    if not all(isinstance(group, dict) for group in groups.values()):
        return False
    headers = groups["headers"]
    if len({key.lower() for key in headers}) != len(headers):
        return False
    groups["headers"] = {key.lower(): item for key, item in headers.items()}
    declared: dict[str, set[str]] = {key: set() for key in groups}
    for raw in operation.get("parameters", []):
        parameter, context = resolve(raw, source)
        location = "headers" if parameter["in"] == "header" else parameter["in"]
        key = parameter["name"].lower() if location == "headers" else parameter["name"]
        declared[location].add(key)
        if key not in groups[location]:
            if parameter.get("required"):
                return False
        elif not valid(parameter["schema"], groups[location][key], context):
            return False
    return all(set(groups[key]) <= declared[key] for key in ("path", "query"))


def sample(identity: str) -> dict[str, Any]:
    return copy.deepcopy(next(e for e in EXAMPLES if e["id"] == identity))


@pytest.mark.parametrize("example", EXAMPLES, ids=lambda e: e["id"])
def test_python_agrees_with_original_openapi_without_mutation(example: dict[str, Any]) -> None:
    assert source_accepts(example) is example["valid"]
    value = copy.deepcopy(example["value"])

    def check() -> Any:
        if example["kind"] == "request":
            return validate_request(example["operation"], value)
        return validate_response(example["operation"], example["status"], value)

    if example["valid"]:
        assert check() is value
    else:
        with pytest.raises(ContractValidationError):
            check()
    assert value == example["value"]


def test_binding_surface_selects_only_create_and_fixed_revision_read() -> None:
    actual = contract()
    assert actual == generator().bundle_http_surface(CONFIG)
    validate(actual)
    assert actual["info"]["version"] == "0.1.0.dev4"
    assert set(actual["paths"]) == set(CONFIG["paths"])
    assert set(actual["paths"]["/v1/nexus/admin/model-bindings"]) == {"post"}
    assert set(
        actual["paths"]["/v1/nexus/admin/model-bindings/{binding_id}/revisions/{revision}"]
    ) == {"get"}
    assert all(set(item) == {"get"} for item in workspace_contract()["paths"].values())
    assert "/v1/nexus/admin/model-bindings" not in workspace_contract()["paths"]
    for unknown in ("publishModelBindingRevision", "listModelBindings", "queryBindingCommand"):
        with pytest.raises(ValueError, match="Unknown HTTP operation"):
            validate_request(unknown, {})
    actual["paths"].clear()
    assert len(contract()["paths"]) == 2


@pytest.mark.parametrize("keyed", [True, False])
def test_python_consumer_does_not_auto_retry_or_invent_a_recovery_operation(keyed: bool) -> None:
    request = sample("create-original" if keyed else "create-keyless")["value"]
    unknown, success = sample("create-unknown"), sample("create-draft")
    calls: list[tuple[str, str | None, Any]] = []

    def respond(wire: httpx.Request) -> httpx.Response:
        calls.append((wire.method, wire.headers.get("Idempotency-Key"), json.loads(wire.content)))
        reply = unknown if len(calls) == 1 else success
        return httpx.Response(reply["status"], json=reply["value"]["body"])

    with httpx.Client(
        transport=httpx.MockTransport(respond), base_url="http://deployment.invalid"
    ) as client:
        validate_request(POST, request)
        first = client.post(
            "/v1/nexus/admin/model-bindings", json=request["body"], headers=request.get("headers")
        )
        validate_response(POST, first.status_code, {"body": first.json()})
        assert first.json()["error"]["retryable"] is False and "nexus" not in first.json()["error"]
        assert len(calls) == 1
        if keyed:
            # Explicit caller recovery uses the original POST/body/key, not a new command.
            second = client.post(
                "/v1/nexus/admin/model-bindings", json=request["body"], headers=request["headers"]
            )
            validate_response(POST, second.status_code, {"body": second.json()})
            assert calls[0] == calls[1]
        else:
            assert calls[0][1] is None
