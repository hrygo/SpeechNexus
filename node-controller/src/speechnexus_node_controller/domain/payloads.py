"""Canonical JSON projections of runtime-control facts.

The HTTP boundary and the durable intent store share these projections, so a
recorded intent, the response that replays it, and the persisted record cannot
drift into three field definitions. Names and value domains are the frozen
contract's; ``tests/contract/test_node_controller_contract.py`` validates the
output of these functions against
``contracts/internal/runtime-control.openapi.yaml``.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any, Final, Literal, overload

from speechnexus_node_controller.domain.intents import (
    InvalidRuntimeRequestError,
    MemoryArchitecture,
    MemoryHold,
    ObservedInstanceState,
    RuntimeActionKind,
    RuntimeInstanceIntent,
    RuntimeInstanceIntentRequest,
    RuntimeIntentErrorCode,
    RuntimeIntentState,
    RuntimeMemoryReservation,
    RuntimeMemoryVector,
    RuntimeStopEvidence,
)

REQUEST_FIELDS: Final = frozenset(RuntimeInstanceIntentRequest.__dataclass_fields__)
# The frozen response schema carries the immutable identity without the caller's
# deadline: the deadline stays in the recorded intent and is not echoed back.
RESPONSE_FIELDS: Final = REQUEST_FIELDS - {"deadline_at"}
INTENT_RECORD_STATE_FIELDS: Final = frozenset(
    {
        "intent_state",
        "observed_instance_state",
        "memory_hold",
        "observed_peak_memory",
        "stop_evidence",
        "error_code",
        "version",
        "created_at",
        "updated_at",
    }
)
_VECTOR_FIELDS: Final = (
    "unified_memory_bytes",
    "device_memory_bytes",
    "host_memory_bytes",
)


def request_payload(request: RuntimeInstanceIntentRequest) -> dict[str, Any]:
    """Project the immutable request identity onto its contract fields."""
    return {
        "intent_id": request.intent_id,
        "target_node_id": request.target_node_id,
        "pool_id": request.pool_id,
        "gateway_id": request.gateway_id,
        "instance_id": request.instance_id,
        "instance_generation": request.instance_generation,
        "approved_template_id": request.approved_template_id,
        "binding_revision_id": request.binding_revision_id,
        "manifest_id": request.manifest_id,
        "config_digest": request.config_digest,
        "action": request.action.value,
        "fencing_token": request.fencing_token,
        "reserved_memory": reservation_payload(request.reserved_memory),
        "deadline_at": request.deadline_at.isoformat(),
    }


def intent_response_payload(
    intent: RuntimeInstanceIntent, *, idempotent_replay: bool | None = None
) -> dict[str, Any]:
    """Project one recorded intent onto the contract response fields.

    ``idempotent_replay`` is only present on a submit response; a status read
    omits it because no caller replayed anything.
    """
    request = {
        name: value
        for name, value in request_payload(intent.request).items()
        if name in RESPONSE_FIELDS
    }
    payload: dict[str, Any] = {
        **request,
        "intent_state": intent.intent_state.value,
        "observed_instance_state": intent.observed_instance_state.value,
        "observed_peak_memory": vector_payload(intent.observed_peak_memory),
        "stop_evidence": stop_evidence_payload(intent.stop_evidence),
        "error_code": intent.error_code.value if intent.error_code is not None else None,
        "created_at": intent.created_at.isoformat(),
        "updated_at": intent.updated_at.isoformat(),
    }
    if idempotent_replay is not None:
        payload["idempotent_replay"] = idempotent_replay
    return payload


def intent_record_payload(intent: RuntimeInstanceIntent) -> dict[str, Any]:
    """Project one recorded intent onto its durable store record."""
    return {
        "request": request_payload(intent.request),
        "state": {
            "intent_state": intent.intent_state.value,
            "observed_instance_state": intent.observed_instance_state.value,
            "memory_hold": intent.memory_hold.value,
            "observed_peak_memory": vector_payload(intent.observed_peak_memory),
            "stop_evidence": stop_evidence_payload(intent.stop_evidence),
            "error_code": intent.error_code.value if intent.error_code is not None else None,
            "version": intent.version,
            "created_at": intent.created_at.isoformat(),
            "updated_at": intent.updated_at.isoformat(),
        },
    }


def reservation_payload(reservation: RuntimeMemoryReservation) -> dict[str, Any]:
    return {
        "architecture": reservation.architecture.value,
        "peak": vector_payload(reservation.peak),
        "steady": vector_payload(reservation.steady),
    }


def vector_payload(vector: RuntimeMemoryVector | None) -> dict[str, int] | None:
    if vector is None:
        return None
    return {name: getattr(vector, name) for name in _VECTOR_FIELDS}


def stop_evidence_payload(evidence: RuntimeStopEvidence | None) -> dict[str, Any] | None:
    if evidence is None:
        return None
    return {
        "process_or_container_exited": evidence.process_or_container_exited,
        "resources_released_confirmed": evidence.resources_released_confirmed,
        "exit_code": evidence.exit_code,
        "runtime_container_id": evidence.runtime_container_id,
        "verified_at": evidence.verified_at.isoformat(),
    }


def parse_request(payload: Mapping[str, Any]) -> RuntimeInstanceIntentRequest:
    """Rebuild the immutable request from its contract fields."""
    _require_known_fields(payload, REQUEST_FIELDS, "runtime intent request")
    raw_action = _string(payload, "action")
    try:
        action = RuntimeActionKind(raw_action)
    except ValueError as error:
        raise InvalidRuntimeRequestError(f"unknown action {raw_action}") from error
    return RuntimeInstanceIntentRequest(
        intent_id=_string(payload, "intent_id"),
        target_node_id=_string(payload, "target_node_id"),
        pool_id=_string(payload, "pool_id"),
        gateway_id=_string(payload, "gateway_id"),
        instance_id=_string(payload, "instance_id"),
        instance_generation=_integer(payload, "instance_generation"),
        approved_template_id=_string(payload, "approved_template_id"),
        binding_revision_id=_string(payload, "binding_revision_id"),
        manifest_id=_string(payload, "manifest_id"),
        config_digest=_string(payload, "config_digest"),
        action=action,
        fencing_token=_integer(payload, "fencing_token"),
        reserved_memory=parse_reservation(payload.get("reserved_memory")),
        deadline_at=_timestamp(payload, "deadline_at"),
    )


def parse_intent_record(record: Mapping[str, Any]) -> RuntimeInstanceIntent:
    """Rebuild one recorded intent from its durable store record."""
    request = parse_request(_mapping(record, "request"))
    state = _mapping(record, "state")
    _require_known_fields(state, INTENT_RECORD_STATE_FIELDS, "runtime intent record")
    return RuntimeInstanceIntent(
        request=request,
        created_at=_timestamp(state, "created_at"),
        updated_at=_timestamp(state, "updated_at"),
        intent_state=_enum(RuntimeIntentState, state, "intent_state"),
        observed_instance_state=_enum(ObservedInstanceState, state, "observed_instance_state"),
        memory_hold=_enum(MemoryHold, state, "memory_hold"),
        observed_peak_memory=parse_vector(state.get("observed_peak_memory")),
        stop_evidence=parse_stop_evidence(state.get("stop_evidence")),
        error_code=_optional_error_code(state.get("error_code")),
        version=_integer(state, "version"),
    )


def parse_reservation(value: object) -> RuntimeMemoryReservation:
    payload = _as_mapping(value, "reserved_memory")
    _require_known_fields(payload, {"architecture", "peak", "steady"}, "reserved_memory")
    raw_architecture = _string(payload, "architecture")
    try:
        architecture = MemoryArchitecture(raw_architecture)
    except ValueError as error:
        raise InvalidRuntimeRequestError(
            f"unknown memory architecture {raw_architecture}"
        ) from error
    return RuntimeMemoryReservation(
        architecture=architecture,
        peak=parse_vector(payload.get("peak"), required=True),
        steady=parse_vector(payload.get("steady"), required=True),
    )


@overload
def parse_vector(value: object, *, required: Literal[True]) -> RuntimeMemoryVector: ...


@overload
def parse_vector(value: object, *, required: bool = False) -> RuntimeMemoryVector | None: ...


def parse_vector(value: object, *, required: bool = False) -> RuntimeMemoryVector | None:
    if value is None:
        if required:
            raise InvalidRuntimeRequestError("a memory vector is required here")
        return None
    payload = _as_mapping(value, "memory vector")
    _require_known_fields(payload, set(_VECTOR_FIELDS), "memory vector")
    return RuntimeMemoryVector(
        unified_memory_bytes=_integer(payload, "unified_memory_bytes"),
        device_memory_bytes=_integer(payload, "device_memory_bytes"),
        host_memory_bytes=_integer(payload, "host_memory_bytes"),
    )


def parse_stop_evidence(value: object) -> RuntimeStopEvidence | None:
    if value is None:
        return None
    payload = _as_mapping(value, "stop_evidence")
    _require_known_fields(
        payload,
        {
            "process_or_container_exited",
            "resources_released_confirmed",
            "exit_code",
            "runtime_container_id",
            "verified_at",
        },
        "stop_evidence",
    )
    if payload["process_or_container_exited"] is not True:
        raise InvalidRuntimeRequestError("stop evidence requires an exited container")
    if payload["resources_released_confirmed"] is not True:
        raise InvalidRuntimeRequestError("stop evidence requires released resources")
    exit_code = payload.get("exit_code")
    if exit_code is not None and not isinstance(exit_code, int):
        raise InvalidRuntimeRequestError("stop evidence exit_code must be an integer or null")
    container_id = payload.get("runtime_container_id")
    if container_id is not None and not isinstance(container_id, str):
        raise InvalidRuntimeRequestError(
            "stop evidence runtime_container_id must be a string or null"
        )
    return RuntimeStopEvidence(
        verified_at=_timestamp(payload, "verified_at"),
        exit_code=exit_code,
        runtime_container_id=container_id,
    )


def _optional_error_code(value: object) -> RuntimeIntentErrorCode | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise InvalidRuntimeRequestError("error_code must be a string or null")
    try:
        return RuntimeIntentErrorCode(value)
    except ValueError as error:
        raise InvalidRuntimeRequestError(f"unknown intent error code {value}") from error


def _enum[T: str](enum_type: type[T], payload: Mapping[str, Any], name: str) -> T:
    value = _string(payload, name)
    try:
        return enum_type(value)
    except ValueError as error:
        raise InvalidRuntimeRequestError(f"unknown {name} {value}") from error


def _require_known_fields(
    payload: Mapping[str, Any], allowed: frozenset[str] | set[str], label: str
) -> None:
    unknown = set(payload) - set(allowed)
    if unknown:
        raise InvalidRuntimeRequestError(f"{label} carries unknown fields {sorted(unknown)}")


def _string(payload: Mapping[str, Any], name: str) -> str:
    value = payload.get(name)
    if not isinstance(value, str):
        raise InvalidRuntimeRequestError(f"{name} must be a string")
    return value


def _integer(payload: Mapping[str, Any], name: str) -> int:
    value = payload.get(name)
    if not isinstance(value, int) or isinstance(value, bool):
        raise InvalidRuntimeRequestError(f"{name} must be an integer")
    return value


def _timestamp(payload: Mapping[str, Any], name: str) -> datetime:
    value = payload.get(name)
    if not isinstance(value, str):
        raise InvalidRuntimeRequestError(f"{name} must be an RFC 3339 timestamp")
    try:
        return datetime.fromisoformat(value)
    except ValueError as error:
        raise InvalidRuntimeRequestError(f"{name} is not an RFC 3339 timestamp") from error


def _mapping(payload: Mapping[str, Any], name: str) -> Mapping[str, Any]:
    return _as_mapping(payload.get(name), name)


def _as_mapping(value: object, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise InvalidRuntimeRequestError(f"{name} must be an object")
    return value
