"""The node controller vocabulary must stay equal to the frozen runtime-control contract."""

from __future__ import annotations

from typing import Any

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
    RuntimeMemoryReservation,
    RuntimeMemoryVector,
)
from speechnexus_node_controller.domain.templates import ApprovedTemplateRegistry
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
            _RESPONSE_VALIDATOR.validate(_response(intent, replay=replay))


def test_stop_response_always_carries_both_stop_confirmations() -> None:
    stop_intent = next(
        intent
        for intent in _producible_intents()
        if intent.action is RuntimeActionKind.STOP
        and intent.intent_state is RuntimeIntentState.SUCCEEDED
    )
    response = _response(stop_intent, replay=False)
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


def _response(intent: RuntimeInstanceIntent, *, replay: bool) -> dict[str, Any]:
    """Project one domain fact onto the contract response fields."""
    request = intent.request
    return {
        "intent_id": request.intent_id,
        "target_node_id": request.target_node_id,
        "pool_id": request.pool_id,
        "gateway_id": request.gateway_id,
        "approved_template_id": request.approved_template_id,
        "binding_revision_id": request.binding_revision_id,
        "manifest_id": request.manifest_id,
        "config_digest": request.config_digest,
        "instance_id": request.instance_id,
        "instance_generation": request.instance_generation,
        "action": request.action.value,
        "fencing_token": request.fencing_token,
        "intent_state": intent.intent_state.value,
        "observed_instance_state": intent.observed_instance_state.value,
        "reserved_memory": _reservation(request.reserved_memory),
        "idempotent_replay": replay,
        "stop_evidence": _stop_evidence(intent),
        "observed_peak_memory": _vector(intent.observed_peak_memory),
        "error_code": intent.error_code.value if intent.error_code is not None else None,
        "created_at": intent.created_at.isoformat(),
        "updated_at": intent.updated_at.isoformat(),
    }


def _reservation(reservation: RuntimeMemoryReservation) -> dict[str, Any]:
    return {
        "architecture": reservation.architecture.value,
        "peak": _vector(reservation.peak),
        "steady": _vector(reservation.steady),
    }


def _vector(vector: RuntimeMemoryVector | None) -> dict[str, int] | None:
    if vector is None:
        return None
    return {
        "unified_memory_bytes": vector.unified_memory_bytes,
        "device_memory_bytes": vector.device_memory_bytes,
        "host_memory_bytes": vector.host_memory_bytes,
    }


def _stop_evidence(intent: RuntimeInstanceIntent) -> dict[str, Any] | None:
    evidence = intent.stop_evidence
    if evidence is None:
        return None
    return {
        "process_or_container_exited": evidence.process_or_container_exited,
        "resources_released_confirmed": evidence.resources_released_confirmed,
        "exit_code": evidence.exit_code,
        "runtime_container_id": evidence.runtime_container_id,
        "verified_at": evidence.verified_at.isoformat(),
    }
