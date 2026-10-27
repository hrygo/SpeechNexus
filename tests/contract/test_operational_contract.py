"""The implemented applications expose the reviewed operational contract."""

from __future__ import annotations

import copy
import tempfile
from pathlib import Path
from typing import Any, cast

import pytest
import yaml
from fastapi import FastAPI
from openapi_spec_validator import validate
from pydantic import AnyHttpUrl, SecretStr

from speechnexus_backend.app import create_app as create_backend_app
from speechnexus_backend.settings import Settings as BackendSettings
from speechnexus_backend.speech.application.readiness import (
    ReadinessService as BackendReadiness,
)
from speechnexus_gateway.app import create_app as create_gateway_app
from speechnexus_gateway.application.readiness import ReadinessService as GatewayReadiness
from speechnexus_gateway.settings import Settings as GatewaySettings
from speechnexus_node_controller.app import create_app as create_controller_app
from speechnexus_node_controller.application.readiness import (
    ReadinessService as ControllerReadiness,
)
from speechnexus_node_controller.application.runtime_control import (
    NodeRuntimeControlService,
)
from speechnexus_node_controller.domain.templates import ApprovedTemplateRegistry
from speechnexus_node_controller.settings import Settings as ControllerSettings
from tests.fake.controller import InMemoryIntentStore, ScriptedNodeRuntime, build_template
from tests.fake.readiness import StaticProbe

_CONTRACT_PATH = Path(__file__).resolve().parents[2] / "contracts" / "operations.openapi.yaml"
_HTTP_METHODS = frozenset({"get", "put", "post", "delete", "options", "head", "patch", "trace"})


def _backend() -> FastAPI:
    settings = BackendSettings(
        database_url=SecretStr("postgresql://unused@127.0.0.1/unused"),
        object_storage_endpoint=AnyHttpUrl("http://127.0.0.1:9000"),
        object_storage_healthcheck_url=AnyHttpUrl("http://127.0.0.1:9000/health"),
        object_storage_bucket="test",
    )
    readiness = BackendReadiness([StaticProbe("postgresql")], timeout_seconds=1)
    return create_backend_app(settings=settings, readiness=readiness)


def _gateway() -> FastAPI:
    settings = GatewaySettings(
        control_database_url=SecretStr("postgresql://unused@127.0.0.1/unused")
    )
    readiness = GatewayReadiness([StaticProbe("control_postgresql")], timeout_seconds=1)
    return create_gateway_app(settings=settings, readiness=readiness)


def _controller() -> FastAPI:
    """The node controller serves the shared operational contract next to its own routes."""
    root = Path(tempfile.mkdtemp(prefix="speechnexus-operational-contract-"))
    settings = ControllerSettings(
        service_name="speechnexus-node-controller-test",
        node_id="node-contract-test",
        component_token=SecretStr("operational-contract-component-token"),
        approved_templates_file=root / "approved-templates.json",
        intent_store_path=root / "runtime-intents.sqlite3",
        docker_socket_path=root / "missing-docker.sock",
    )
    service = NodeRuntimeControlService(
        node_id="node-contract-test",
        templates=ApprovedTemplateRegistry((build_template(),)),
        store=InMemoryIntentStore(),
        runtime=ScriptedNodeRuntime(),
    )
    return create_controller_app(
        settings=settings,
        service=service,
        readiness=ControllerReadiness([StaticProbe("container_daemon")], timeout_seconds=1),
    )


def _object(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise AssertionError("expected an object in the operational contract")
    return cast(dict[str, Any], value)


def _load_contract() -> dict[str, Any]:
    document: object = yaml.safe_load(_CONTRACT_PATH.read_text(encoding="utf-8"))
    return _object(document)


def _resolved_schema(node: Any, document: dict[str, Any]) -> Any:
    if isinstance(node, list):
        return [_resolved_schema(item, document) for item in node]
    if not isinstance(node, dict):
        return node
    if "$ref" in node:
        target: Any = document
        for part in node["$ref"].removeprefix("#/").split("/"):
            target = target[part.replace("~1", "/").replace("~0", "~")]
        node = {**target, **{key: value for key, value in node.items() if key != "$ref"}}
    result = {
        key: _resolved_schema(value, document)
        for key, value in node.items()
        if key not in {"title", "description", "default"}
    }
    # Pydantic emits nullable scalar types as anyOf rather than a type array.
    variants = result.get("anyOf")
    if variants and all(set(item) == {"type"} for item in variants):
        result.pop("anyOf")
        result["type"] = [item["type"] for item in variants]
    if isinstance(result.get("type"), list):
        result["type"] = sorted(result["type"])
    if "required" in result:
        result["required"] = sorted(result["required"])
    return result


def _assert_schema_guarantees(expected: Any, actual: Any) -> None:
    if isinstance(expected, dict):
        assert isinstance(actual, dict)
        for key, value in expected.items():
            assert key in actual, f"missing schema constraint {key}"
            if key == "required":
                assert set(value) <= set(actual[key]), "response required guarantee removed"
            else:
                _assert_schema_guarantees(value, actual[key])
    else:
        assert expected == actual


def _assert_contract_matches(
    expected: dict[str, Any], actual: dict[str, Any], *, require_exact_paths: bool = True
) -> None:
    expected_info = _object(expected["info"])
    actual_info = _object(actual["info"])
    assert expected_info["version"] == actual_info["version"]

    expected_paths = _object(expected["paths"])
    actual_paths = _object(actual["paths"])
    if require_exact_paths:
        assert set(expected_paths) == set(actual_paths)
    else:
        assert set(expected_paths) <= set(actual_paths)

    for path, expected_path_value in expected_paths.items():
        expected_path = _object(expected_path_value)
        actual_path = _object(actual_paths[path])
        expected_methods = {method for method in expected_path if method in _HTTP_METHODS}
        actual_methods = {method for method in actual_path if method in _HTTP_METHODS}
        assert expected_methods == actual_methods

        for method in expected_methods:
            expected_operation = _object(expected_path[method])
            actual_operation = _object(actual_path[method])
            assert expected_operation["operationId"] == actual_operation["operationId"]
            assert expected_operation["summary"] == actual_operation["summary"]

            expected_responses = _object(expected_operation["responses"])
            actual_responses = _object(actual_operation["responses"])
            assert set(expected_responses) <= set(actual_responses)

            for status_code, expected_response_value in expected_responses.items():
                expected_response = _object(expected_response_value)
                actual_response = _object(actual_responses[status_code])
                expected_headers = {
                    header.lower() for header in _object(expected_response.get("headers", {}))
                }
                actual_headers = {
                    header.lower() for header in _object(actual_response.get("headers", {}))
                }
                assert expected_headers <= actual_headers

                expected_schema = _object(
                    _object(_object(expected_response["content"])["application/json"])["schema"]
                )
                actual_schema = _object(
                    _object(_object(actual_response["content"])["application/json"])["schema"]
                )
                _assert_schema_guarantees(
                    _resolved_schema(expected_schema, expected),
                    _resolved_schema(actual_schema, actual),
                )


def test_backend_exposes_valid_operational_openapi() -> None:
    document = _backend().openapi()
    validate(document)
    assert set(document["paths"]) == {
        "/healthz",
        "/readyz",
        "/v1/nexus/assets",
        "/v1/nexus/assets/{asset_id}",
        "/v1/nexus/assets/{asset_id}/content",
        "/v1/nexus/workspaces",
        "/v1/nexus/workspaces/{workspace_id}",
        "/v1/nexus/admin/profiles",
        "/v1/nexus/admin/profiles/{profile_id}/revisions/{revision}",
        "/v1/nexus/admin/model-bindings",
        "/v1/nexus/admin/model-bindings/{binding_id}/revisions/{revision}",
        "/v1/nexus/pronunciation-sets/{pronunciation_id}/revisions/{revision}",
        "/v1/nexus/capabilities",
        "/v1/models",
        "/v1/models/{model}",
        "/v1/nexus/artifacts/{artifact_id}",
        "/v1/nexus/artifacts/{artifact_id}/content",
        "/v1/nexus/jobs",
        "/v1/nexus/jobs/{job_id}",
        "/v1/audio/speech",
        "/v1/audio/transcriptions",
    }
    _assert_contract_matches(_load_contract(), document, require_exact_paths=False)


def test_gateway_exposes_valid_operational_openapi() -> None:
    document = _gateway().openapi()
    validate(document)
    assert set(document["paths"]) == {"/healthz", "/readyz"}
    _assert_contract_matches(_load_contract(), document)


def test_controller_exposes_valid_operational_openapi() -> None:
    document = _controller().openapi()
    validate(document)
    assert {"/healthz", "/readyz"} <= set(document["paths"])
    _assert_contract_matches(_load_contract(), document, require_exact_paths=False)


@pytest.mark.parametrize(
    "component,field,keyword,value",
    [
        ("HealthResponse", "status", "type", "integer"),
        ("HealthResponse", "status", "const", "broken"),
        ("HealthResponse", "service", "minLength", 0),
        ("HealthResponse", "service", "maxLength", 128),
        ("ReadinessResponse", "checks", "minItems", 0),
        ("ReadinessCheckResponse", "status", "enum", ["ok", "unknown"]),
    ],
)
def test_operational_parity_detects_component_drift(
    component: str,
    field: str,
    keyword: str,
    value: Any,
) -> None:
    actual = copy.deepcopy(_backend().openapi())
    actual["components"]["schemas"][component]["properties"][field][keyword] = value
    with pytest.raises(AssertionError):
        _assert_contract_matches(_load_contract(), actual, require_exact_paths=False)


def test_operational_parity_detects_removed_response_guarantee() -> None:
    actual = _gateway().openapi()
    actual["components"]["schemas"]["HealthResponse"]["required"].remove("service")
    with pytest.raises(AssertionError):
        _assert_contract_matches(_load_contract(), actual)


@pytest.mark.parametrize("factory", [_backend, _gateway, _controller])
async def test_operational_http_bodies_satisfy_resolved_contract(factory: Any) -> None:
    from httpx import ASGITransport, AsyncClient
    from jsonschema import Draft202012Validator

    expected = _load_contract()
    async with AsyncClient(
        transport=ASGITransport(app=factory()), base_url="http://test"
    ) as client:
        for path, component in (("/healthz", "HealthResponse"), ("/readyz", "ReadinessResponse")):
            response = await client.get(path)
            assert response.status_code == 200
            assert response.headers["X-Request-ID"]
            schema = _resolved_schema(expected["components"]["schemas"][component], expected)
            Draft202012Validator(schema).validate(response.json())
