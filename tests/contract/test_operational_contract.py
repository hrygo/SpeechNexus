"""The implemented applications expose the reviewed operational contract."""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

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


def _object(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise AssertionError("expected an object in the operational contract")
    return cast(dict[str, Any], value)


def _load_contract() -> dict[str, Any]:
    document: object = yaml.safe_load(_CONTRACT_PATH.read_text(encoding="utf-8"))
    return _object(document)


def _assert_contract_matches(expected: dict[str, Any], actual: dict[str, Any]) -> None:
    expected_info = _object(expected["info"])
    actual_info = _object(actual["info"])
    assert expected_info["version"] == actual_info["version"]

    expected_paths = _object(expected["paths"])
    actual_paths = _object(actual["paths"])
    assert set(expected_paths) == set(actual_paths)

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
                assert expected_schema == actual_schema


def test_backend_exposes_valid_operational_openapi() -> None:
    document = _backend().openapi()
    validate(document)
    assert set(document["paths"]) == {"/healthz", "/readyz"}
    _assert_contract_matches(_load_contract(), document)


def test_gateway_exposes_valid_operational_openapi() -> None:
    document = _gateway().openapi()
    validate(document)
    assert set(document["paths"]) == {"/healthz", "/readyz"}
    _assert_contract_matches(_load_contract(), document)
