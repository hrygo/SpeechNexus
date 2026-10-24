"""Canonicalize original commands outside transactions; never resolve defaults."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

import rfc8785
from speechnexus_contracts.validation import validate_coordination

from speechnexus_backend.speech.domain.admission import (
    AdmissionIdentity,
    AdmissionRequest,
    AdmitRecipe,
    OriginalBusinessCommand,
    StageAllocation,
)
from speechnexus_backend.speech.domain.jobs import CreateJob
from speechnexus_backend.speech.domain.owners import OwnerAuthority
from speechnexus_backend.speech.domain.runtime_intents import integer


def _sha(document: str) -> str:
    return "sha256:" + hashlib.sha256(document.encode()).hexdigest()


def business_command_from_value(value: Any, *, max_command_bytes: int) -> OriginalBusinessCommand:
    """Input must already be source-validated, effective and pinned by the caller.

    This is a private server context, not a new JSON request field or a semantic
    validator. No network route or default producer is installed by this codec.
    """
    integer(max_command_bytes, minimum=1)
    try:
        if not isinstance(value, dict):
            raise ValueError
        encoded = rfc8785.dumps(value)
        if len(encoded) > max_command_bytes:
            raise ValueError
        document = encoded.decode()
        return OriginalBusinessCommand(document, _sha(document))
    except ValueError, TypeError, UnicodeError, RecursionError:
        raise ValueError("Invalid original effective business command") from None


def validate_business_command(command: OriginalBusinessCommand, *, max_command_bytes: int) -> None:
    integer(max_command_bytes, minimum=1)
    try:
        raw = command.document.encode()
        if len(raw) > max_command_bytes or _sha(command.document) != command.request_digest:
            raise ValueError
        parsed = json.loads(command.document)
        if not isinstance(parsed, dict) or rfc8785.dumps(parsed) != raw:
            raise ValueError
    except ValueError, TypeError, UnicodeError, RecursionError:
        raise ValueError("Invalid original effective business command") from None


def request_from_wire(value: Any, business_command: OriginalBusinessCommand) -> AdmissionRequest:
    return request_with_business_digest(value, business_command.request_digest)


def request_with_business_digest(value: Any, business_digest: str) -> AdmissionRequest:
    """Decode immutable admission metadata without reconstructing a deleted body."""
    validate_coordination("admit_recipe", value)
    identity = AdmissionIdentity(value["identity"]["operation_id"], value["identity"]["attempt_id"])
    job = value["job"]
    return AdmissionRequest(
        identity,
        value["workspace_id"],
        value["binding_revision_id"],
        value["manifest_id"],
        value["recipe_digest"],
        value["policy_revision_id"],
        int(value["expected_quota_version"]),
        value["service_class"],
        tuple(
            OwnerAuthority(a["pool_id"], a["owner_id"], int(a["fencing_token"]))
            for a in value["authorities"]
        ),
        tuple(
            StageAllocation(
                s["stage_id"],
                s["lease_id"],
                s["pool_id"],
                s["host_node_id"],
                s["gateway_id"],
                s["instance_id"],
                int(s["instance_generation"]),
                int(s["expected_instance_version"]),
                int(s["reserved_slots"]),
            )
            for s in value["stages"]
        ),
        int(value["deadline_at_us"]),
        CreateJob(
            job["job_id"],
            identity.operation_id,
            identity.attempt_id,
            job["event_id"],
            job["event_digest"],
            business_digest,
        ),
    )


def request_to_wire(request: AdmissionRequest) -> dict[str, Any]:
    value: dict[str, Any] = {
        "identity": {
            "operation_id": request.identity.operation_id,
            "attempt_id": request.identity.attempt_id,
        },
        "workspace_id": request.workspace_id,
        "binding_revision_id": request.binding_revision_id,
        "manifest_id": request.manifest_id,
        "recipe_digest": request.recipe_digest,
        "policy_revision_id": request.policy_revision_id,
        "expected_quota_version": str(request.expected_quota_version),
        "service_class": request.service_class,
        "authorities": [
            {"pool_id": a.pool_id, "owner_id": a.owner_id, "fencing_token": a.fencing_token}
            for a in request.authorities
        ],
        "stages": [
            {
                "stage_id": s.stage_id,
                "lease_id": s.lease_id,
                "pool_id": s.pool_id,
                "host_node_id": s.host_node_id,
                "gateway_id": s.gateway_id,
                "instance_id": s.instance_id,
                "instance_generation": s.instance_generation,
                "expected_instance_version": str(s.expected_instance_version),
                "reserved_slots": s.reserved_slots,
            }
            for s in request.stages
        ],
        "deadline_at_us": request.deadline_at_us,
        "job": {
            "job_id": request.job.job_id,
            "event_id": request.job.event_id,
            "event_digest": request.job.event_digest,
        },
    }
    validate_coordination("admit_recipe", value)
    return value


@dataclass(frozen=True, slots=True)
class PreparedAdmission:
    command: AdmitRecipe
    document: str = field(repr=False)
    request_digest: str


def prepare_admission(command: AdmitRecipe, *, max_command_bytes: int) -> PreparedAdmission:
    validate_business_command(command.business_command, max_command_bytes=max_command_bytes)
    document = rfc8785.dumps(request_to_wire(command.request)).decode()
    if len(document.encode()) > max_command_bytes:
        raise ValueError("Admission envelope exceeds the configured bound")
    return PreparedAdmission(command, document, _sha(document))
