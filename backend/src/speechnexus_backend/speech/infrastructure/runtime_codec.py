"""Boundary/persistence codecs for immutable runtime commands; never perform I/O."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, cast

from speechnexus_contracts.validation import validate_profile

from speechnexus_backend.speech.domain.capacity import PhysicalResources
from speechnexus_backend.speech.domain.runtime_intents import (
    ApprovedRuntimePlan,
    RuntimeBinding,
    RuntimeDeadline,
    RuntimeIntent,
    RuntimeMemory,
    identifier,
)

_PLAN_FIELDS = frozenset(
    {
        "topology_digest",
        "qualification_digest",
        "service_class",
        "loading_policy",
        "max_slots",
        "realtime_reserved_slots",
        "peak",
        "steady",
        "per_slot",
    }
)


def _json(value: object) -> str:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )


def _sha(document: str) -> str:
    return "sha256:" + hashlib.sha256(document.encode()).hexdigest()


def _memory(value: Any) -> PhysicalResources:
    return PhysicalResources(
        int(value["unified_memory_bytes"]),
        int(value["device_memory_bytes"]),
        int(value["host_memory_bytes"]),
        0,
        0,
        0,
    )


def request_from_wire(value: Any) -> RuntimeIntent:
    validate_profile("runtime", "RuntimeInstanceIntentRequest", value)
    memory = value["reserved_memory"]
    return RuntimeIntent(
        value["intent_id"],
        RuntimeBinding(
            value["target_node_id"],
            value["pool_id"],
            value["gateway_id"],
            value["approved_template_id"],
            value["binding_revision_id"],
            value["manifest_id"],
            value["config_digest"],
        ),
        value["instance_id"],
        int(value["instance_generation"]),
        value["action"],
        int(value["fencing_token"]),
        RuntimeMemory(memory["architecture"], _memory(memory["peak"]), _memory(memory["steady"])),
        RuntimeDeadline(value["deadline_at"]),
    )


def request_to_wire(request: RuntimeIntent) -> dict[str, Any]:
    def memory(vector: PhysicalResources) -> dict[str, int]:
        return {
            "unified_memory_bytes": vector.unified_physical_memory_bytes,
            "device_memory_bytes": vector.device_memory_bytes,
            "host_memory_bytes": vector.host_memory_bytes,
        }

    binding = request.binding
    value: dict[str, Any] = {
        "intent_id": request.intent_id,
        "target_node_id": binding.target_node_id,
        "pool_id": binding.pool_id,
        "gateway_id": binding.gateway_id,
        "approved_template_id": binding.approved_template_id,
        "binding_revision_id": binding.binding_revision_id,
        "manifest_id": binding.manifest_id,
        "config_digest": binding.config_digest,
        "instance_id": request.instance_id,
        "instance_generation": request.instance_generation,
        "action": request.action,
        "fencing_token": request.fencing_token,
        "reserved_memory": {
            "architecture": request.reserved_memory.architecture,
            "peak": memory(request.reserved_memory.peak),
            "steady": memory(request.reserved_memory.steady),
        },
        "deadline_at": request.deadline.text,
    }
    validate_profile("runtime", "RuntimeInstanceIntentRequest", value)
    return value


@dataclass(frozen=True, slots=True)
class PreparedRuntimeRequest:
    request: RuntimeIntent
    document: str = field(repr=False)
    request_digest: str
    event_id: str


def prepare_request(request: RuntimeIntent) -> PreparedRuntimeRequest:
    # Complete effective request, not a digest-only reconstruction. Date text
    # is retained; integer DTO representations have already become domain ints.
    document = _json(request_to_wire(request))
    if len(document.encode()) > 65536:
        raise ValueError("Runtime command exceeds its bounded metadata record")
    identity = _json([request.binding.target_node_id, request.intent_id])
    event_id = "runtime-event-" + hashlib.sha256(identity.encode()).hexdigest()
    return PreparedRuntimeRequest(request, document, _sha(document), event_id)


def encode_record(
    prepared: PreparedRuntimeRequest, plan: ApprovedRuntimePlan, creator_id: str
) -> str:
    identifier(creator_id)
    if prepared.request.binding != plan.binding or prepared.request.reserved_memory != plan.memory:
        raise ValueError("Runtime request does not match the resolved plan")
    return _json(
        {
            "record_version": 1,
            "creator_id": creator_id,
            "request": json.loads(prepared.document),
            "plan": {
                "topology_digest": plan.topology_digest,
                "qualification_digest": plan.qualification_digest,
                "service_class": plan.service_class,
                "loading_policy": plan.loading_policy,
                "max_slots": plan.max_slots,
                "realtime_reserved_slots": plan.realtime_reserved_slots,
                "peak": plan.peak.values,
                "steady": plan.steady.values,
                "per_slot": plan.per_slot.values,
            },
        }
    )


def decode_record(document: str) -> tuple[PreparedRuntimeRequest, ApprovedRuntimePlan, str]:
    if not isinstance(document, str) or len(document.encode()) > 131072:
        raise ValueError("Invalid bounded runtime record")
    value = json.loads(document)
    if (
        not isinstance(value, dict)
        or set(value) != {"record_version", "creator_id", "request", "plan"}
        or type(value["record_version"]) is not int
        or value["record_version"] != 1
    ):
        raise ValueError("Runtime record requires controlled legacy reconciliation")
    identifier(value["creator_id"])
    prepared = prepare_request(request_from_wire(value["request"]))
    data = value["plan"]
    if not isinstance(data, dict) or set(data) != _PLAN_FIELDS:
        raise ValueError("Incomplete immutable runtime plan")

    def vector(name: str) -> PhysicalResources:
        fields = data[name]
        if not isinstance(fields, list) or len(fields) != 6:
            raise ValueError("Runtime resource vector must be complete")
        return PhysicalResources(*fields)

    plan = ApprovedRuntimePlan(
        prepared.request.binding,
        data["topology_digest"],
        data["qualification_digest"],
        prepared.request.reserved_memory.architecture,
        data["service_class"],
        data["loading_policy"],
        data["max_slots"],
        data["realtime_reserved_slots"],
        vector("peak"),
        vector("steady"),
        vector("per_slot"),
    )
    if prepared.request.reserved_memory != plan.memory:
        raise ValueError("Stored wire reservation differs from its immutable plan")
    return prepared, plan, value["creator_id"]


def validate_original_observation(document: str, request: RuntimeIntent) -> dict[str, Any]:
    if not isinstance(document, str) or len(document.encode()) > 65536:
        raise ValueError("Invalid original runtime observation")
    value: Any = json.loads(document)
    validate_profile("runtime", "RuntimeInstanceIntentResponse", value)
    expected = request_to_wire(request)
    for name in (
        "intent_id",
        "target_node_id",
        "pool_id",
        "gateway_id",
        "instance_id",
        "instance_generation",
        "approved_template_id",
        "binding_revision_id",
        "manifest_id",
        "config_digest",
        "action",
        "fencing_token",
        "reserved_memory",
    ):
        if value[name] != expected[name]:
            raise ValueError("Observation does not belong to the original runtime request")
    return cast(dict[str, Any], value)
