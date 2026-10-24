"""Closed, bounded runtime observations and private readiness provenance."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, cast

from speechnexus_contracts.validation import validate_profile

from speechnexus_backend.speech.domain.runtime_intents import (
    ApprovedRuntimePlan,
    RuntimeBinding,
    RuntimeIntent,
    RuntimeMemory,
    runtime_timestamp_key,
    runtime_timestamp_us,
)
from speechnexus_backend.speech.domain.runtime_observations import (
    RuntimeObservation,
    RuntimeObservedStop,
    VerifiedRuntimeReadiness,
)
from speechnexus_backend.speech.infrastructure.runtime_codec import _json, _memory

_BINDING_FIELDS = (
    "target_node_id",
    "pool_id",
    "gateway_id",
    "approved_template_id",
    "binding_revision_id",
    "manifest_id",
    "config_digest",
)
_PROOF_FIELDS = frozenset(
    {
        "record_version",
        "binding",
        "instance_id",
        "instance_generation",
        "fencing_token",
        "topology_digest",
        "qualification_digest",
        "evidence_digest",
        "verified_at_us",
        "expires_at_us",
    }
)


@dataclass(frozen=True, slots=True)
class PreparedRuntimeObservation:
    observation: RuntimeObservation
    document: str = field(repr=False)
    semantic_document: str = field(repr=False)
    stop_document: str | None = field(default=None, repr=False)


def prepare_observation(value: Any) -> PreparedRuntimeObservation:
    validate_profile("runtime", "RuntimeInstanceIntentResponse", value)
    document = _json(value)
    if len(document.encode()) > 65536:
        raise ValueError("Runtime observation exceeds its bounded metadata record")
    wire = cast(dict[str, Any], json.loads(document))
    for name in ("instance_generation", "fencing_token"):
        wire[name] = int(wire[name])
    memory = wire["reserved_memory"]
    for vector in (memory["peak"], memory["steady"], wire.get("observed_peak_memory")):
        if vector is not None:
            for name, amount in vector.items():
                vector[name] = int(amount)
    stop = wire.get("stop_evidence")
    if stop is not None and stop.get("exit_code") is not None:
        stop["exit_code"] = int(stop["exit_code"])
    observation = RuntimeObservation(
        wire["intent_id"],
        RuntimeBinding(*(wire[name] for name in _BINDING_FIELDS)),
        wire["instance_id"],
        wire["instance_generation"],
        wire["action"],
        wire["fencing_token"],
        RuntimeMemory(memory["architecture"], _memory(memory["peak"]), _memory(memory["steady"])),
        wire["intent_state"],
        wire["observed_instance_state"],
        runtime_timestamp_us(wire["created_at"]),
        runtime_timestamp_us(wire["updated_at"]),
        _memory(wire["observed_peak_memory"])
        if wire.get("observed_peak_memory") is not None
        else None,
        RuntimeObservedStop(
            stop["process_or_container_exited"],
            stop["resources_released_confirmed"],
            runtime_timestamp_us(stop["verified_at"]),
            runtime_timestamp_key(stop["verified_at"])[1],
        )
        if stop is not None
        else None,
        wire.get("error_code"),
        created_submicrosecond=runtime_timestamp_key(wire["created_at"])[1],
        updated_submicrosecond=runtime_timestamp_key(wire["updated_at"])[1],
    )
    document = _json(wire)
    # The POST-only replay flag and omitted optional nulls are transport
    # presentation, not new runtime facts. Persist the first actual response.
    wire.pop("idempotent_replay", None)
    for name in ("observed_peak_memory", "stop_evidence", "error_code"):
        wire.setdefault(name, None)
    if stop is not None:
        stop.setdefault("exit_code", None)
        stop.setdefault("runtime_container_id", None)
    return PreparedRuntimeObservation(
        observation, document, _json(wire), _json(stop) if stop is not None else None
    )


def decode_observation(document: str) -> PreparedRuntimeObservation:
    if not isinstance(document, str) or len(document.encode()) > 65536:
        raise ValueError("Invalid bounded runtime observation record")
    return prepare_observation(json.loads(document))


def readiness_to_document(receipt: VerifiedRuntimeReadiness) -> str:
    return _json(
        {
            "record_version": 1,
            "binding": {name: getattr(receipt.binding, name) for name in _BINDING_FIELDS},
            "instance_id": receipt.instance_id,
            "instance_generation": receipt.instance_generation,
            "fencing_token": receipt.fencing_token,
            "topology_digest": receipt.topology_digest,
            "qualification_digest": receipt.qualification_digest,
            "evidence_digest": receipt.evidence_digest,
            "verified_at_us": receipt.verified_at_us,
            "expires_at_us": receipt.expires_at_us,
        }
    )


def readiness_from_document(document: str) -> VerifiedRuntimeReadiness:
    if not isinstance(document, str) or len(document.encode()) > 8192:
        raise ValueError("Invalid bounded runtime readiness record")
    value = json.loads(document)
    if (
        not isinstance(value, dict)
        or set(value) != _PROOF_FIELDS
        or type(value["record_version"]) is not int
        or value["record_version"] != 1
    ):
        raise ValueError("Invalid runtime readiness format")
    binding = value["binding"]
    if not isinstance(binding, dict) or set(binding) != set(_BINDING_FIELDS):
        raise ValueError("Invalid readiness binding")
    return VerifiedRuntimeReadiness(
        RuntimeBinding(*(binding[name] for name in _BINDING_FIELDS)),
        value["instance_id"],
        value["instance_generation"],
        value["fencing_token"],
        value["topology_digest"],
        value["qualification_digest"],
        value["evidence_digest"],
        value["verified_at_us"],
        value["expires_at_us"],
    )


def validate_stored_readiness(
    document: str, request: RuntimeIntent, plan: ApprovedRuntimePlan
) -> VerifiedRuntimeReadiness:
    receipt = readiness_from_document(document)
    if (
        receipt.binding != request.binding
        or receipt.binding != plan.binding
        or receipt.instance_id != request.instance_id
        or receipt.instance_generation != request.instance_generation
        or receipt.fencing_token != request.fencing_token
        or receipt.topology_digest != plan.topology_digest
        or receipt.qualification_digest != plan.qualification_digest
        or request.action != "prepare"
    ):
        raise ValueError("Stored readiness does not match its immutable runtime plan")
    return receipt


def observation_for_boundary(value: Any) -> tuple[RuntimeObservation, str]:
    prepared = prepare_observation(value)
    return prepared.observation, prepared.document
