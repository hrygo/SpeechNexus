"""Boundary payloads of ``internal/runtime-control``.

These models are the HTTP shape only: every request is immediately rebuilt as a
domain intent through ``domain.payloads``, and every response is projected from
the recorded domain fact by the same module, so the boundary does not own a
second copy of the semantics. ``tests/contract/test_node_controller_contract.py``
keeps the generated OpenAPI equal to the frozen contract.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from speechnexus_node_controller.domain.intents import (
    MemoryArchitecture,
    ObservedInstanceState,
    RuntimeActionKind,
    RuntimeControlErrorCode,
    RuntimeInstanceIntentRequest,
    RuntimeIntentState,
)
from speechnexus_node_controller.domain.payloads import (
    parse_request,
    parse_stop_evidence,
)
from speechnexus_node_controller.domain.ports import (
    NodeReconcileSnapshot,
    ReportedInstanceFact,
)

_MODEL_CONFIG = ConfigDict(extra="forbid", json_schema_serialization_defaults_required=True)


class RuntimeMemoryVectorPayload(BaseModel):
    model_config = _MODEL_CONFIG

    unified_memory_bytes: int = Field(ge=0)
    device_memory_bytes: int = Field(ge=0)
    host_memory_bytes: int = Field(ge=0)


class RuntimeMemoryReservationPayload(BaseModel):
    model_config = _MODEL_CONFIG

    architecture: MemoryArchitecture
    peak: RuntimeMemoryVectorPayload
    steady: RuntimeMemoryVectorPayload


class RuntimeStopEvidencePayload(BaseModel):
    model_config = _MODEL_CONFIG

    process_or_container_exited: Literal[True]
    resources_released_confirmed: Literal[True]
    exit_code: int | None = Field(default=None)
    runtime_container_id: str | None = Field(default=None, max_length=128)
    verified_at: datetime


class RuntimeInstanceIntentRequestPayload(BaseModel):
    model_config = _MODEL_CONFIG

    intent_id: str
    target_node_id: str
    pool_id: str
    gateway_id: str
    instance_id: str
    instance_generation: int = Field(ge=1)
    approved_template_id: str
    binding_revision_id: str
    manifest_id: str
    config_digest: str
    action: RuntimeActionKind
    fencing_token: int = Field(ge=1)
    reserved_memory: RuntimeMemoryReservationPayload
    deadline_at: datetime

    def to_domain(self) -> RuntimeInstanceIntentRequest:
        """Rebuild the immutable intent through the shared request parser."""
        return parse_request(self.model_dump(mode="json"))


class RuntimeInstanceIntentResponse(BaseModel):
    model_config = _MODEL_CONFIG

    intent_id: str
    target_node_id: str
    pool_id: str
    gateway_id: str
    approved_template_id: str
    binding_revision_id: str
    manifest_id: str
    config_digest: str
    instance_id: str
    instance_generation: int = Field(ge=1)
    action: RuntimeActionKind
    fencing_token: int = Field(ge=1)
    intent_state: RuntimeIntentState
    observed_instance_state: ObservedInstanceState
    reserved_memory: RuntimeMemoryReservationPayload
    idempotent_replay: bool | None = Field(default=None)
    stop_evidence: RuntimeStopEvidencePayload | None = Field(default=None)
    observed_peak_memory: RuntimeMemoryVectorPayload | None = Field(default=None)
    error_code: str | None = Field(default=None, pattern="^[a-z][a-z0-9_]{2,63}$")
    created_at: datetime
    updated_at: datetime


class ObservedNodeInstanceFactPayload(BaseModel):
    model_config = _MODEL_CONFIG

    instance_id: str
    instance_generation: int = Field(ge=1)
    approved_template_id: str
    config_digest: str
    observed_instance_state: ObservedInstanceState
    stop_evidence: RuntimeStopEvidencePayload | None = Field(default=None)

    def to_domain(self) -> ReportedInstanceFact:
        payload = self.model_dump(mode="json")
        return ReportedInstanceFact(
            instance_id=self.instance_id,
            instance_generation=self.instance_generation,
            approved_template_id=self.approved_template_id,
            config_digest=self.config_digest,
            observed_instance_state=self.observed_instance_state,
            stop_evidence=parse_stop_evidence(payload["stop_evidence"]),
        )


class NodeReconcileRequestPayload(BaseModel):
    model_config = _MODEL_CONFIG

    controller_id: str
    fencing_token: int = Field(ge=1)
    reported_at: datetime
    instances: list[ObservedNodeInstanceFactPayload]

    def to_domain(self) -> NodeReconcileSnapshot:
        return NodeReconcileSnapshot(
            controller_id=self.controller_id,
            fencing_token=self.fencing_token,
            reported_at=self.reported_at,
            instances=tuple(fact.to_domain() for fact in self.instances),
        )


class NodeReconcileResponse(BaseModel):
    model_config = _MODEL_CONFIG

    node_id: str
    accepted_at: datetime
    quarantined_instance_ids: list[str]


class RuntimeControlErrorDetail(BaseModel):
    model_config = _MODEL_CONFIG

    code: RuntimeControlErrorCode
    message: str = Field(min_length=1)
    retryable: bool
    observed_fencing_token: int | None = Field(default=None, ge=1)


class RuntimeControlErrorResponse(BaseModel):
    model_config = _MODEL_CONFIG

    error: RuntimeControlErrorDetail


class HealthResponse(BaseModel):
    """Operational liveness response; shape owned by ``contracts/operations``."""

    model_config = _MODEL_CONFIG

    status: Literal["ok"] = "ok"
    service: str = Field(min_length=1, max_length=64)
    version: str = Field(min_length=1)


class ReadinessCheckResponse(BaseModel):
    model_config = _MODEL_CONFIG

    name: str = Field(min_length=1)
    status: Literal["ok", "failed"]
    detail: str | None = None


class ReadinessResponse(BaseModel):
    model_config = _MODEL_CONFIG

    status: Literal["ready", "unready"]
    checks: list[ReadinessCheckResponse] = Field(min_length=1)
