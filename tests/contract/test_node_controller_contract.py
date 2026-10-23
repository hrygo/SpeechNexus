"""The node controller vocabulary must stay equal to the frozen runtime-control contract."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr

from speechnexus_node_controller.app import create_app
from speechnexus_node_controller.application.readiness import ReadinessService
from speechnexus_node_controller.application.runtime_control import NodeRuntimeControlService
from speechnexus_node_controller.domain.intents import (
    POOL_IDS,
    MemoryArchitecture,
    ObservedInstanceState,
    RuntimeActionKind,
    RuntimeControlErrorCode,
    RuntimeInstanceIntent,
    RuntimeInstanceIntentRequest,
    RuntimeIntentState,
)
from speechnexus_node_controller.domain.payloads import (
    intent_response_payload,
    request_payload,
)
from speechnexus_node_controller.domain.templates import ApprovedTemplateRegistry
from speechnexus_node_controller.interfaces.http.errors import _STATUS_BY_CODE
from speechnexus_node_controller.interfaces.http.schemas import (
    NodeReconcileRequestPayload,
    NodeReconcileResponse,
    ObservedNodeInstanceFactPayload,
    RuntimeControlErrorDetail,
    RuntimeControlErrorResponse,
    RuntimeInstanceIntentRequestPayload,
    RuntimeInstanceIntentResponse,
    RuntimeStopEvidencePayload,
)
from speechnexus_node_controller.settings import Settings
from tests.contract.test_p0_public_contracts import (
    _CONTRACTS,
    _component_validator,
    _load_json,
    _resolved_yaml,
)
from tests.fake.controller import (
    FIXED_NOW,
    SAMPLE_INSTANCE_ID,
    SAMPLE_NODE_ID,
    InMemoryIntentStore,
    ScriptedNodeRuntime,
    build_request,
    build_template,
)
from tests.fake.readiness import StaticProbe

_DOCUMENT = _resolved_yaml(_CONTRACTS / "internal" / "runtime-control.openapi.yaml")
_SCHEMAS: dict[str, Any] = _DOCUMENT["components"]["schemas"]
_RESPONSE_VALIDATOR = _component_validator(_DOCUMENT, "RuntimeInstanceIntentResponse")


def _enum(schema: dict[str, Any]) -> set[str]:
    values: list[str] = schema["enum"]
    return set(values)


def test_conflict_codes_equal_the_contract_enum() -> None:
    code_schema = _SCHEMAS["RuntimeControlErrorResponse"]["properties"]["error"]["properties"][
        "code"
    ]
    assert _enum(code_schema) == {code.value for code in RuntimeControlErrorCode}


def test_intent_states_equal_the_contract_enum() -> None:
    assert _enum(_SCHEMAS["RuntimeIntentState"]) == {state.value for state in RuntimeIntentState}


def test_observed_instance_states_equal_the_shared_schema() -> None:
    shared = _load_json(_CONTRACTS / "shared" / "runtime-states.schema.json")
    assert _enum(shared["$defs"]["ObservedInstanceState"]) == {
        state.value for state in ObservedInstanceState
    }


def test_pool_and_memory_vocabulary_equal_the_shared_schemas() -> None:
    identifiers = _load_json(_CONTRACTS / "shared" / "identifiers.schema.json")
    assert _enum(identifiers["$defs"]["PoolId"]) == set(POOL_IDS)
    capability = _load_json(_CONTRACTS / "shared" / "capability-domains.schema.json")
    assert _enum(capability["$defs"]["MemoryArchitecture"]) == {
        architecture.value for architecture in MemoryArchitecture
    }


def test_request_fields_equal_the_frozen_request_schema() -> None:
    schema = _SCHEMAS["RuntimeInstanceIntentRequest"]
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == set(RuntimeInstanceIntentRequest.__dataclass_fields__)


def test_every_producible_intent_validates_as_a_contract_response() -> None:
    """Whatever the controller records must serialize into the frozen response shape."""
    intents = _producible_intents()
    assert {intent.intent_state for intent in intents} == {
        RuntimeIntentState.RECONCILING,
        RuntimeIntentState.SUCCEEDED,
        RuntimeIntentState.FAILED,
        RuntimeIntentState.QUARANTINED,
    }
    for intent in intents:
        for replay in (False, True):
            _RESPONSE_VALIDATOR.validate(intent_response_payload(intent, idempotent_replay=replay))


def test_stop_response_always_carries_both_stop_confirmations() -> None:
    stop_intent = next(
        intent
        for intent in _producible_intents()
        if intent.action is RuntimeActionKind.STOP
        and intent.intent_state is RuntimeIntentState.SUCCEEDED
    )
    response = intent_response_payload(stop_intent, idempotent_replay=False)
    evidence = response["stop_evidence"]
    assert evidence["process_or_container_exited"] is True
    assert evidence["resources_released_confirmed"] is True


def _producible_intents() -> list[RuntimeInstanceIntent]:
    ready_runtime = ScriptedNodeRuntime(ready_on_prepare=True)
    service, _, _ = _service(runtime=ready_runtime)
    ready = service.submit_intent(build_request()).intent
    stopped = service.submit_intent(
        build_request(intent_id="intent-stop-1", action=RuntimeActionKind.STOP, fencing_token=2)
    ).intent

    loading_service, loading_runtime, _ = _service(runtime=ScriptedNodeRuntime())
    request = build_request(intent_id="intent-2", instance_generation=2, fencing_token=3)
    loading = loading_service.submit_intent(request).intent
    loading_runtime.observe_as(request, ObservedInstanceState.ABSENT)
    failed = loading_service.reconcile_instance(SAMPLE_INSTANCE_ID, 2)[0].intent

    unverified_service, _, _ = _service(
        runtime=ScriptedNodeRuntime(ready_on_prepare=False, verify_stop=False)
    )
    quarantined = unverified_service.submit_intent(
        build_request(intent_id="intent-stop-2", action=RuntimeActionKind.STOP, fencing_token=1)
    ).intent
    del ready_runtime, loading_runtime
    return [ready, stopped, loading, failed, quarantined]


def _service(
    *,
    runtime: ScriptedNodeRuntime,
) -> tuple[NodeRuntimeControlService, ScriptedNodeRuntime, InMemoryIntentStore]:
    store = InMemoryIntentStore()
    service = NodeRuntimeControlService(
        node_id=SAMPLE_NODE_ID,
        templates=ApprovedTemplateRegistry((build_template(),)),
        store=store,
        runtime=runtime,
        now=lambda: FIXED_NOW,
    )
    return service, runtime, store


_HTTP_METHODS = frozenset({"get", "put", "post", "delete", "options", "head", "patch", "trace"})
_COMPONENT_TOKEN = "contract-test-component-token"
_HEADERS = {"Authorization": f"Bearer {_COMPONENT_TOKEN}"}
_INTENTS_PATH = f"/internal/v1/nodes/{SAMPLE_NODE_ID}/instance-intents"
_RECONCILE_PATH = f"/internal/v1/nodes/{SAMPLE_NODE_ID}/reconcile"


def _controller_app(tmp_path: Path, *, runtime: ScriptedNodeRuntime | None = None) -> FastAPI:
    """Build the served app with doubles, so the test never touches a daemon."""
    service = NodeRuntimeControlService(
        node_id=SAMPLE_NODE_ID,
        templates=ApprovedTemplateRegistry((build_template(),)),
        store=InMemoryIntentStore(),
        runtime=runtime or ScriptedNodeRuntime(ready_on_prepare=True),
        now=lambda: FIXED_NOW,
    )
    settings = Settings(
        service_name="speechnexus-node-controller-contract",
        node_id=SAMPLE_NODE_ID,
        component_token=SecretStr(_COMPONENT_TOKEN),
        approved_templates_file=tmp_path / "approved-templates.json",
        intent_store_path=tmp_path / "runtime-intents.sqlite3",
        docker_socket_path=tmp_path / "missing-docker.sock",
    )
    return create_app(
        settings=settings,
        service=service,
        readiness=ReadinessService([StaticProbe("container_daemon")], timeout_seconds=1),
    )


def test_every_frozen_operation_is_served_with_the_frozen_identity(tmp_path: Path) -> None:
    document = _controller_app(tmp_path).openapi()
    served_paths = document["paths"]

    for path, operations in _DOCUMENT["paths"].items():
        assert path in served_paths, f"the controller does not serve {path}"
        for method, operation in operations.items():
            if method not in _HTTP_METHODS:
                continue
            served = served_paths[path].get(method)
            assert served is not None, f"{method.upper()} {path} is not served"
            assert served["operationId"] == operation["operationId"]
            assert served["summary"] == operation["summary"]
            for status_code, expected_response in operation["responses"].items():
                assert status_code in served["responses"], (
                    f"{method.upper()} {path} does not declare {status_code}"
                )
                expected_headers = {name.lower() for name in expected_response.get("headers", {})}
                served_headers = {
                    name.lower() for name in served["responses"][status_code].get("headers", {})
                }
                assert expected_headers <= served_headers


def test_the_boundary_models_own_exactly_the_frozen_fields(tmp_path: Path) -> None:
    document = _controller_app(tmp_path).openapi()
    for name, model in (
        ("RuntimeInstanceIntentRequest", RuntimeInstanceIntentRequestPayload),
        ("RuntimeInstanceIntentResponse", RuntimeInstanceIntentResponse),
        ("ObservedNodeInstanceFact", ObservedNodeInstanceFactPayload),
        ("NodeReconcileRequest", NodeReconcileRequestPayload),
        ("NodeReconcileResponse", NodeReconcileResponse),
        ("RuntimeControlErrorResponse", RuntimeControlErrorResponse),
        ("RuntimeStopEvidence", RuntimeStopEvidencePayload),
    ):
        expected = _SCHEMAS[name]
        assert set(model.model_fields) == set(expected["properties"]), name
        assert {
            field for field, definition in model.model_fields.items() if definition.is_required()
        } == set(expected["required"]), name
    assert document["components"]["schemas"]


def test_the_error_detail_requires_exactly_the_frozen_fields() -> None:
    expected = _SCHEMAS["RuntimeControlErrorResponse"]["properties"]["error"]

    assert set(RuntimeControlErrorDetail.model_fields) == set(expected["properties"])
    assert {
        field
        for field, definition in RuntimeControlErrorDetail.model_fields.items()
        if definition.is_required()
    } == set(expected["required"])


def test_every_contract_error_code_has_exactly_one_http_status() -> None:
    assert set(_STATUS_BY_CODE) == set(RuntimeControlErrorCode)
    assert _STATUS_BY_CODE[RuntimeControlErrorCode.COMPONENT_CREDENTIAL_INVALID] == 401
    assert _STATUS_BY_CODE[RuntimeControlErrorCode.RUNTIME_INTENT_NOT_FOUND] == 404
    assert _STATUS_BY_CODE[RuntimeControlErrorCode.RUNTIME_CONTROLLER_UNAVAILABLE] == 503


def test_the_recorded_request_round_trips_through_the_shared_boundary_codec() -> None:
    request = build_request()

    payload = request_payload(request)

    assert set(payload) == set(_SCHEMAS["RuntimeInstanceIntentRequest"]["required"])
    assert payload["deadline_at"] == request.deadline_at.isoformat()
    assert RuntimeInstanceIntentRequestPayload.model_validate(payload).to_domain() == request


async def test_served_responses_validate_against_the_frozen_contract(tmp_path: Path) -> None:
    error_validator = _component_validator(_DOCUMENT, "RuntimeControlErrorResponse")
    async with AsyncClient(
        transport=ASGITransport(app=_controller_app(tmp_path)), base_url="http://test"
    ) as client:
        unauthorized = await client.get(f"{_INTENTS_PATH}/intent-1")
        body = request_payload(build_request())
        recorded = await client.post(_INTENTS_PATH, json=body, headers=_HEADERS)
        replayed = await client.post(_INTENTS_PATH, json=body, headers=_HEADERS)
        missing = await client.get(f"{_INTENTS_PATH}/intent-unknown", headers=_HEADERS)
        foreign = await client.get(
            "/internal/v1/nodes/node-other/instance-intents/intent-1", headers=_HEADERS
        )
        malformed = await client.post(
            _INTENTS_PATH, json={"intent_id": "intent-1"}, headers=_HEADERS
        )
        reconciled = await client.post(_RECONCILE_PATH, json=_reconcile_body(), headers=_HEADERS)

    assert recorded.status_code == 200
    assert replayed.status_code == 200
    assert replayed.json()["idempotent_replay"] is True
    for response in (recorded, replayed):
        _RESPONSE_VALIDATOR.validate(response.json())

    assert unauthorized.status_code == 401
    for response, status_code in ((missing, 404), (foreign, 409), (malformed, 422)):
        assert response.status_code == status_code
        error_validator.validate(response.json())

    assert reconciled.status_code == 200
    _component_validator(_DOCUMENT, "NodeReconcileResponse").validate(reconciled.json())


async def test_an_unavailable_node_runtime_answers_with_a_valid_retryable_error(
    tmp_path: Path,
) -> None:
    runtime = ScriptedNodeRuntime()
    runtime.unavailable = True
    error_validator = _component_validator(_DOCUMENT, "RuntimeControlErrorResponse")
    app = _controller_app(tmp_path, runtime=runtime)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            _INTENTS_PATH,
            json=request_payload(
                build_request(
                    intent_id="intent-stop-silent",
                    action=RuntimeActionKind.STOP,
                    fencing_token=2,
                )
            ),
            headers=_HEADERS,
        )

    assert response.status_code == 503
    error_validator.validate(response.json())
    assert response.json()["error"]["retryable"] is True


def _reconcile_body() -> dict[str, Any]:
    return {
        "controller_id": SAMPLE_NODE_ID,
        "fencing_token": 1,
        "reported_at": FIXED_NOW.isoformat(),
        "instances": [],
    }
