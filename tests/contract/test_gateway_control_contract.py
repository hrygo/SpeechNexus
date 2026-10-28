"""Served control routes preserve frozen constraints and safe consumer recovery."""

from pathlib import Path
from typing import Any, cast

import pytest
from httpx import ASGITransport, AsyncClient
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012
from speechnexus_contracts.execution import parse

from tests.contract.test_operational_contract import (
    _assert_schema_guarantees,
    _resolved_schema,
)
from tests.contract.test_p0_public_contracts import (
    _CONTRACTS,
    _component_validator,
    _resolved_yaml,
)
from tests.support.contracts import retrieve
from tests.unit.gateway.test_barrier_http import BODY, HEADERS, PATH, app_for

DOCUMENT = _resolved_yaml(_CONTRACTS / "internal" / "execution-control.openapi.yaml")
PATH_TEMPLATE = "/internal/v1/gateways/{gateway_id}/fencing-barrier"
QUERY_PATH = "/internal/v1/executions/{submit_intent_id}"
RESULT_PATH = "/internal/v1/executions/{submit_intent_id}/staging-result"
AUDIO_RESULT_PATH = "/internal/v1/executions/{submit_intent_id}/audio-staging-result"
SUBMIT_PATH = "/internal/v1/executions/submit"
ABORT_PATH = "/internal/v1/executions/{submit_intent_id}/abort"
ABORT_INTENT_PATH = "/internal/v1/executions/{submit_intent_id}/abort-intents"
DRAIN_PATH = "/internal/v1/instances/{instance_id}/drain"


def frozen_schema(node: Any) -> Any:
    root = cast(Any, Registry)(retrieve=retrieve).resolver_with_root(
        Resource.from_contents(DOCUMENT, default_specification=DRAFT202012)
    )

    def expand(value: Any, resolver: Any) -> Any:
        if isinstance(value, list):
            return [expand(item, resolver) for item in value]
        if not isinstance(value, dict):
            return value
        if "$ref" in value:
            target = resolver.lookup(value["$ref"])
            return expand(
                {**target.contents, **{key: item for key, item in value.items() if key != "$ref"}},
                target.resolver,
            )
        return {key: expand(item, resolver) for key, item in value.items()}

    return expand(node, root)


@pytest.mark.parametrize(
    "path,method",
    [
        (PATH_TEMPLATE, "post"),
        (QUERY_PATH, "get"),
        (RESULT_PATH, "get"),
        (AUDIO_RESULT_PATH, "get"),
        (SUBMIT_PATH, "post"),
        (ABORT_PATH, "post"),
        (ABORT_INTENT_PATH, "post"),
        (DRAIN_PATH, "post"),
    ],
)
def test_served_control_declares_the_frozen_security_statuses_and_full_constraints(
    tmp_path: Path, path: str, method: str
) -> None:
    app, _ = app_for(tmp_path)
    actual = app.openapi()
    operation = actual["paths"][path][method]
    expected = DOCUMENT["paths"][path][method]
    assert operation["operationId"] == expected["operationId"]
    assert operation["summary"] == expected["summary"]
    assert operation["security"] == [{"ComponentBearer": []}]
    assert actual["components"]["securitySchemes"]["ComponentBearer"]["scheme"] == "bearer"
    if "requestBody" in expected:
        _assert_schema_guarantees(
            _resolved_schema(
                frozen_schema(expected["requestBody"]["content"]["application/json"]["schema"]), {}
            ),
            _resolved_schema(
                operation["requestBody"]["content"]["application/json"]["schema"], actual
            ),
        )
    assert set(expected["responses"]) <= set(operation["responses"])
    served_parameters = {item["name"]: item for item in operation["parameters"]}
    for parameter in expected["parameters"]:
        parameter = frozen_schema(parameter)
        served = served_parameters[parameter["name"]]
        assert parameter["in"] == served["in"]
        assert parameter["required"] == served["required"]
        actual_schema = _resolved_schema(served["schema"], actual)
        # Optional Header uses an explicit null branch in the generated OpenAPI.
        if "anyOf" in actual_schema:
            actual_schema = next(
                item for item in actual_schema["anyOf"] if item.get("type") != "null"
            )
        _assert_schema_guarantees(_resolved_schema(parameter["schema"], {}), actual_schema)
    for status, response in expected["responses"].items():
        response = frozen_schema(response)
        served = operation["responses"][status]
        assert {name.lower() for name in response.get("headers", {})} <= {
            name.lower() for name in served.get("headers", {})
        }
        _assert_schema_guarantees(
            _resolved_schema(response["content"]["application/json"]["schema"], {}),
            _resolved_schema(served["content"]["application/json"]["schema"], actual),
        )
    assert {path for path in actual["paths"] if path.startswith("/internal/")} == {
        PATH_TEMPLATE,
        QUERY_PATH,
        RESULT_PATH,
        AUDIO_RESULT_PATH,
        SUBMIT_PATH,
        ABORT_PATH,
        ABORT_INTENT_PATH,
        DRAIN_PATH,
    }


async def test_success_and_error_payloads_validate_for_the_source_and_generated_consumer(
    tmp_path: Path,
) -> None:
    app, _ = app_for(tmp_path)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        success = await client.post(PATH, json=BODY, headers=HEADERS)
        unauthorized = await client.post(PATH, json=BODY)
        invalid = await client.post(PATH, json=BODY | {"unknown": True}, headers=HEADERS)
        abort = await client.post(PATH, json=BODY | {"abort_in_flight": True}, headers=HEADERS)
    _component_validator(DOCUMENT, "GatewayFencingBarrierResponse").validate(success.json())
    parse("GatewayFencingBarrierResponse", success.json())
    for response in (unauthorized, invalid, abort):
        _component_validator(DOCUMENT, "ExecutionControlErrorResponse").validate(response.json())
        parse("ExecutionControlErrorResponse", response.json())


@pytest.mark.parametrize(
    "code,recovery,retryable",
    [
        ("control_store_unavailable", "retry_transaction", True),
        ("control_outcome_unknown", "query_original", False),
        ("control_reconciliation_required", "reconcile", False),
        ("control_state_conflict", "query_current_authority", False),
    ],
)
def test_control_failure_recovery_is_a_schema_constraint(
    code: str, recovery: str, retryable: bool
) -> None:
    from speechnexus_contracts import ContractValidationError

    value: dict[str, Any] = {
        "error": {
            "code": code,
            "message": "Synthetic control failure.",
            "retryable": retryable,
            "recovery": recovery,
        }
    }
    parse("ExecutionControlErrorResponse", value)
    for mutated in (
        value["error"] | {"retryable": not retryable},
        value["error"] | {"recovery": "none"},
        {key: item for key, item in value["error"].items() if key != "recovery"},
    ):
        with pytest.raises(ContractValidationError):
            parse("ExecutionControlErrorResponse", {"error": mutated})
