"""Runtime instance intent identity, lifecycle and stop-evidence rules.

These rules mirror ``contracts/internal/runtime-control.openapi.yaml``: the
controller only accepts approved templates for its own node, ``(node_id,
intent_id)`` is the idempotency key, a terminal intent is never revived, loading
peak memory is retained until readiness is observed, and a stop intent only
succeeds with both an exit and a resource-release confirmation.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Final

_IDENTIFIER: Final = re.compile(r"^[a-z0-9][a-z0-9._:-]{1,126}[a-z0-9]$")
_DIGEST: Final = re.compile(r"^sha256:[0-9a-f]{64}$")

POOL_IDS: Final = frozenset(
    {"asr-realtime", "tts-realtime", "asr-batch", "tts-batch", "voice-creation", "auxiliary"}
)


class RuntimeControlErrorCode(StrEnum):
    """Stable conflict and validation codes frozen by the runtime-control contract."""

    TARGET_NODE_MISMATCH = "target_node_mismatch"
    UNAPPROVED_RUNTIME_TEMPLATE = "unapproved_runtime_template"
    STALE_FENCING_TOKEN = "stale_fencing_token"
    STALE_INSTANCE_GENERATION = "stale_instance_generation"
    GATEWAY_DRAIN_INCOMPLETE = "gateway_drain_incomplete"
    INTENT_CONFLICT = "intent_conflict"
    RUNTIME_CONTROLLER_UNAVAILABLE = "runtime_controller_unavailable"
    INVALID_RUNTIME_REQUEST = "invalid_runtime_request"
    COMPONENT_CREDENTIAL_INVALID = "component_credential_invalid"


class RuntimeIntentErrorCode(StrEnum):
    """Stable reason codes the controller records on its own intent facts."""

    INSTANCE_PREPARE_FAILED = "instance_prepare_failed"
    INSTANCE_DRAIN_INCOMPLETE = "instance_drain_incomplete"
    STOP_EVIDENCE_UNVERIFIED = "stop_evidence_unverified"
    RUNTIME_UNAVAILABLE = "runtime_unavailable"
    OBSERVED_IDENTITY_MISMATCH = "observed_identity_mismatch"
    UNTRACKED_INSTANCE_OBSERVED = "untracked_instance_observed"


class RuntimeActionKind(StrEnum):
    PREPARE = "prepare"
    STOP = "stop"


class RuntimeIntentState(StrEnum):
    PENDING = "pending"
    RECONCILING = "reconciling"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    QUARANTINED = "quarantined"

    @property
    def is_terminal(self) -> bool:
        return self in {
            RuntimeIntentState.SUCCEEDED,
            RuntimeIntentState.FAILED,
        }

    @property
    def is_unresolved(self) -> bool:
        return not self.is_terminal


class ObservedInstanceState(StrEnum):
    """Controller observation of one instance generation."""

    ABSENT = "absent"
    PREPARING = "preparing"
    LOADING = "loading"
    WARMING_UP = "warming_up"
    READY = "ready"
    DRAINING = "draining"
    STOPPING = "stopping"
    STOPPED = "stopped"
    FAILED = "failed"
    QUARANTINED = "quarantined"

    @property
    def is_serving(self) -> bool:
        return self in {ObservedInstanceState.READY, ObservedInstanceState.DRAINING}


class MemoryArchitecture(StrEnum):
    UNIFIED_MEMORY = "unified_memory"
    DISCRETE_DEVICE_MEMORY = "discrete_device_memory"


class MemoryHold(StrEnum):
    """Which reservation the controller still reports as occupied."""

    PEAK = "peak"
    STEADY = "steady"
    NONE = "none"


class RuntimeControlRejectedError(Exception):
    """A runtime-control request the controller refuses to accept."""

    error_code: RuntimeControlErrorCode
    retryable: bool = False

    def __init__(self, message: str, *, observed_fencing_token: int | None = None) -> None:
        super().__init__(message)
        self.observed_fencing_token = observed_fencing_token


class InvalidRuntimeRequestError(RuntimeControlRejectedError):
    error_code = RuntimeControlErrorCode.INVALID_RUNTIME_REQUEST


class TargetNodeMismatchError(RuntimeControlRejectedError):
    error_code = RuntimeControlErrorCode.TARGET_NODE_MISMATCH


class UnapprovedRuntimeTemplateError(RuntimeControlRejectedError):
    error_code = RuntimeControlErrorCode.UNAPPROVED_RUNTIME_TEMPLATE


class StaleFencingTokenError(RuntimeControlRejectedError):
    error_code = RuntimeControlErrorCode.STALE_FENCING_TOKEN


class StaleInstanceGenerationError(RuntimeControlRejectedError):
    error_code = RuntimeControlErrorCode.STALE_INSTANCE_GENERATION


class GatewayDrainIncompleteError(RuntimeControlRejectedError):
    error_code = RuntimeControlErrorCode.GATEWAY_DRAIN_INCOMPLETE
    retryable = True


class IntentConflictError(RuntimeControlRejectedError):
    error_code = RuntimeControlErrorCode.INTENT_CONFLICT


class RuntimeControllerUnavailableError(RuntimeControlRejectedError):
    error_code = RuntimeControlErrorCode.RUNTIME_CONTROLLER_UNAVAILABLE
    retryable = True


def require_identifier(value: str, field_name: str) -> None:
    """Reject anything outside the shared opaque identifier grammar."""
    if not _IDENTIFIER.match(value):
        raise InvalidRuntimeRequestError(f"{field_name} is not a valid identifier")


def require_digest(value: str, field_name: str) -> None:
    if not _DIGEST.match(value):
        raise InvalidRuntimeRequestError(f"{field_name} must be sha256:<64 lowercase hex>")


@dataclass(frozen=True, slots=True)
class RuntimeMemoryVector:
    """Six-dimension memory reservation vector reduced to the three memory counters."""

    unified_memory_bytes: int = 0
    device_memory_bytes: int = 0
    host_memory_bytes: int = 0

    def __post_init__(self) -> None:
        for name in ("unified_memory_bytes", "device_memory_bytes", "host_memory_bytes"):
            value = getattr(self, name)
            if value < 0:
                raise InvalidRuntimeRequestError(f"{name} must not be negative")

    def active_counters(self, architecture: MemoryArchitecture) -> dict[str, int]:
        """Counters that carry the reservation for one memory architecture."""
        if architecture is MemoryArchitecture.UNIFIED_MEMORY:
            return {"unified_memory_bytes": self.unified_memory_bytes}
        return {
            "device_memory_bytes": self.device_memory_bytes,
            "host_memory_bytes": self.host_memory_bytes,
        }


@dataclass(frozen=True, slots=True)
class RuntimeMemoryReservation:
    """Peak and steady reservation of one instance generation."""

    architecture: MemoryArchitecture
    peak: RuntimeMemoryVector
    steady: RuntimeMemoryVector

    def __post_init__(self) -> None:
        peak_counters = self.peak.active_counters(self.architecture)
        steady_counters = self.steady.active_counters(self.architecture)
        if self.architecture is MemoryArchitecture.UNIFIED_MEMORY:
            if self.peak.device_memory_bytes or self.peak.host_memory_bytes:
                raise InvalidRuntimeRequestError(
                    "unified peak must not carry device or host memory"
                )
            if self.steady.device_memory_bytes or self.steady.host_memory_bytes:
                raise InvalidRuntimeRequestError(
                    "unified steady must not carry device or host memory"
                )
        elif self.peak.unified_memory_bytes or self.steady.unified_memory_bytes:
            raise InvalidRuntimeRequestError("discrete reservation must not carry unified memory")
        for name, peak_bytes in peak_counters.items():
            if peak_bytes < steady_counters[name]:
                raise InvalidRuntimeRequestError(
                    f"peak {name} must be greater than or equal to steady {name}"
                )
        if not any(peak_counters.values()):
            raise InvalidRuntimeRequestError(
                "reservation must reserve memory in the acting counters"
            )


@dataclass(frozen=True, slots=True)
class RuntimeStopEvidence:
    """Verified stop fact; constructing it asserts both confirmations."""

    verified_at: datetime
    exit_code: int | None = None
    runtime_container_id: str | None = None
    process_or_container_exited: bool = True
    resources_released_confirmed: bool = True

    def __post_init__(self) -> None:
        if not (self.process_or_container_exited and self.resources_released_confirmed):
            raise InvalidRuntimeRequestError(
                "stop evidence requires both an exited container and released resources"
            )
        if self.runtime_container_id is not None and len(self.runtime_container_id) > 128:
            raise InvalidRuntimeRequestError("runtime_container_id is longer than 128 characters")


@dataclass(frozen=True, slots=True)
class RuntimeInstanceIntentRequest:
    """Immutable prepare or stop intent as received from the scheduler."""

    intent_id: str
    target_node_id: str
    pool_id: str
    gateway_id: str
    instance_id: str
    instance_generation: int
    approved_template_id: str
    binding_revision_id: str
    manifest_id: str
    config_digest: str
    action: RuntimeActionKind
    fencing_token: int
    reserved_memory: RuntimeMemoryReservation
    deadline_at: datetime

    def __post_init__(self) -> None:
        for name in (
            "intent_id",
            "target_node_id",
            "gateway_id",
            "instance_id",
            "approved_template_id",
            "binding_revision_id",
            "manifest_id",
        ):
            require_identifier(getattr(self, name), name)
        if self.pool_id not in POOL_IDS:
            raise InvalidRuntimeRequestError(f"unknown pool_id {self.pool_id}")
        require_digest(self.config_digest, "config_digest")
        if self.instance_generation < 1:
            raise InvalidRuntimeRequestError("instance_generation must be positive")
        if self.fencing_token < 1:
            raise InvalidRuntimeRequestError("fencing_token must be positive")
        if self.deadline_at.tzinfo is None:
            raise InvalidRuntimeRequestError("deadline_at must carry a timezone")


@dataclass(frozen=True, slots=True)
class InstanceObservation:
    """What the node-local runtime reports for one instance generation."""

    instance_id: str
    instance_generation: int
    approved_template_id: str
    config_digest: str
    observed_instance_state: ObservedInstanceState
    observed_peak_memory: RuntimeMemoryVector | None = None
    stop_evidence: RuntimeStopEvidence | None = None
    quarantine_reason: str | None = None


@dataclass(slots=True)
class RuntimeInstanceIntent:
    """Durable intent fact; unresolved intents keep their memory hold reported."""

    request: RuntimeInstanceIntentRequest
    created_at: datetime
    updated_at: datetime
    intent_state: RuntimeIntentState = RuntimeIntentState.PENDING
    observed_instance_state: ObservedInstanceState = ObservedInstanceState.ABSENT
    memory_hold: MemoryHold = MemoryHold.PEAK
    observed_peak_memory: RuntimeMemoryVector | None = None
    stop_evidence: RuntimeStopEvidence | None = None
    error_code: RuntimeIntentErrorCode | None = None
    version: int = field(default=1)

    def __post_init__(self) -> None:
        if self.request.action is RuntimeActionKind.STOP:
            self.memory_hold = MemoryHold.NONE
        self.validate()

    @property
    def intent_id(self) -> str:
        return self.request.intent_id

    @property
    def instance_id(self) -> str:
        return self.request.instance_id

    @property
    def instance_generation(self) -> int:
        return self.request.instance_generation

    @property
    def action(self) -> RuntimeActionKind:
        return self.request.action

    def validate(self) -> None:
        """Enforce the response invariants of the runtime-control contract."""
        if self.intent_state is RuntimeIntentState.SUCCEEDED and self.error_code is not None:
            raise ValueError("succeeded intent carries no error code")
        if self.intent_state is RuntimeIntentState.FAILED and self.error_code is None:
            raise ValueError("failed intent carries a stable error code")
        if self.stop_evidence is not None:
            if self.action is not RuntimeActionKind.STOP:
                raise ValueError("only a stop intent carries stop evidence")
            if self.intent_state is not RuntimeIntentState.SUCCEEDED:
                raise ValueError("stop evidence requires a succeeded stop intent")
            if self.observed_instance_state not in {
                ObservedInstanceState.STOPPED,
                ObservedInstanceState.ABSENT,
            }:
                raise ValueError("stop evidence requires a stopped or absent observation")
        if (
            self.action is RuntimeActionKind.PREPARE
            and self.intent_state is RuntimeIntentState.SUCCEEDED
            and self.observed_instance_state is not ObservedInstanceState.READY
        ):
            raise ValueError("a succeeded prepare intent observes ready")

    def mark_observed(self, observation: InstanceObservation, *, at: datetime) -> None:
        """Fold one node-local observation into this intent."""
        if observation.observed_instance_state is ObservedInstanceState.QUARANTINED:
            self.intent_state = RuntimeIntentState.QUARANTINED
            self.error_code = RuntimeIntentErrorCode.UNTRACKED_INSTANCE_OBSERVED
            self.observed_instance_state = observation.observed_instance_state
            self._touch(at)
            return

        self.observed_instance_state = observation.observed_instance_state
        if observation.observed_peak_memory is not None:
            self.observed_peak_memory = observation.observed_peak_memory

        if self.action is RuntimeActionKind.PREPARE:
            self._fold_prepare(observation)
        else:
            self._fold_stop(observation)
        self._touch(at)

    def _fold_prepare(self, observation: InstanceObservation) -> None:
        state = observation.observed_instance_state
        if state is ObservedInstanceState.READY:
            self.intent_state = RuntimeIntentState.SUCCEEDED
            self.memory_hold = MemoryHold.STEADY
            self.error_code = None
        elif state in {
            ObservedInstanceState.PREPARING,
            ObservedInstanceState.LOADING,
            ObservedInstanceState.WARMING_UP,
        }:
            self.intent_state = RuntimeIntentState.RECONCILING
            self.memory_hold = MemoryHold.PEAK
            self.error_code = None
        elif state in {ObservedInstanceState.STOPPED, ObservedInstanceState.ABSENT}:
            self.intent_state = RuntimeIntentState.FAILED
            self.memory_hold = MemoryHold.NONE
            self.error_code = RuntimeIntentErrorCode.INSTANCE_PREPARE_FAILED
        else:
            self.intent_state = RuntimeIntentState.FAILED
            self.memory_hold = MemoryHold.PEAK
            self.error_code = RuntimeIntentErrorCode.INSTANCE_PREPARE_FAILED

    def _fold_stop(self, observation: InstanceObservation) -> None:
        state = observation.observed_instance_state
        if state in {ObservedInstanceState.STOPPED, ObservedInstanceState.ABSENT}:
            if observation.stop_evidence is None:
                self.intent_state = RuntimeIntentState.QUARANTINED
                self.memory_hold = MemoryHold.PEAK
                self.error_code = RuntimeIntentErrorCode.STOP_EVIDENCE_UNVERIFIED
                return
            self.intent_state = RuntimeIntentState.SUCCEEDED
            self.memory_hold = MemoryHold.NONE
            self.stop_evidence = observation.stop_evidence
            self.error_code = None
            return
        self.intent_state = RuntimeIntentState.RECONCILING
        self.error_code = RuntimeIntentErrorCode.INSTANCE_DRAIN_INCOMPLETE
        if state is ObservedInstanceState.READY:
            self.memory_hold = MemoryHold.STEADY

    def mark_runtime_unavailable(self, *, at: datetime) -> None:
        """The node runtime did not answer; the intent stays unresolved."""
        self.intent_state = RuntimeIntentState.RECONCILING
        self.error_code = RuntimeIntentErrorCode.RUNTIME_UNAVAILABLE
        self._touch(at)

    def quarantine(self, error_code: RuntimeIntentErrorCode, *, at: datetime) -> None:
        self.intent_state = RuntimeIntentState.QUARANTINED
        self.error_code = error_code
        self._touch(at)

    def _touch(self, at: datetime) -> None:
        self.version += 1
        self.updated_at = at
        self.validate()


@dataclass(frozen=True, slots=True)
class InstanceHead:
    """Derived per-instance lifecycle head used for admission decisions."""

    instance_id: str
    generation_high_water: int | None
    current_generation: int | None
    unresolved_prepare_intent_id: str | None
    unresolved_stop_intent_id: str | None
    last_fencing_token: int
    stopped_generations: frozenset[int]

    @property
    def is_drained(self) -> bool:
        """True when the current generation has no unresolved lifecycle work."""
        if self.current_generation is None:
            return True
        return (
            self.current_generation in self.stopped_generations
            and self.unresolved_prepare_intent_id is None
            and self.unresolved_stop_intent_id is None
        )

    @classmethod
    def derive(cls, instance_id: str, intents: list[RuntimeInstanceIntent]) -> InstanceHead:
        generations = [intent.instance_generation for intent in intents]
        high_water = max(generations) if generations else None
        stopped = frozenset(
            intent.instance_generation
            for intent in intents
            if intent.action is RuntimeActionKind.STOP
            and intent.intent_state is RuntimeIntentState.SUCCEEDED
        )
        unresolved_stop = cls._latest_unresolved(intents, RuntimeActionKind.STOP)
        unresolved_prepare = cls._latest_unresolved(intents, RuntimeActionKind.PREPARE)
        current = high_water
        if high_water is not None and high_water in stopped:
            current = max(
                (
                    intent.instance_generation
                    for intent in intents
                    if intent.instance_generation not in stopped
                ),
                default=None,
            )
        return cls(
            instance_id=instance_id,
            generation_high_water=high_water,
            current_generation=current,
            unresolved_prepare_intent_id=unresolved_prepare,
            unresolved_stop_intent_id=unresolved_stop,
            last_fencing_token=max((intent.request.fencing_token for intent in intents), default=0),
            stopped_generations=stopped,
        )

    @staticmethod
    def _latest_unresolved(
        intents: list[RuntimeInstanceIntent], action: RuntimeActionKind
    ) -> str | None:
        candidates = [
            intent
            for intent in intents
            if intent.action is action and intent.intent_state.is_unresolved
        ]
        if not candidates:
            return None
        latest = max(candidates, key=lambda intent: (intent.instance_generation, intent.version))
        return latest.intent_id
