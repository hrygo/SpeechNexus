"""Runtime request identities and physical preparation decisions; no transport I/O."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Literal

from speechnexus_backend.speech.domain.capacity import (
    MAX_RESOURCE_COUNTER,
    CapacityExceededError,
    MemoryArchitecture,
    PhysicalResources,
    reserve_resources,
)
from speechnexus_backend.speech.domain.owners import (
    IDENTIFIER,
    MAX_VERSION,
    POOLS,
    PoolOwnership,
)

_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
_RFC3339 = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}[Tt][0-9]{2}:[0-9]{2}:[0-9]{2}"
    r"(?:\.[0-9]+)?(?:[Zz]|[+-][0-9]{2}:[0-9]{2})\Z"
)
type RuntimeAction = Literal["prepare", "stop"]
type RuntimeIntentState = Literal["pending", "reconciling", "succeeded", "failed", "quarantined"]
type BaseBudgetKind = Literal["peak", "steady", "q_peak", "q_steady", "released"]
type ServiceClass = Literal["realtime", "interactive", "batch"]
type LoadingPolicy = Literal["resident", "on_demand"]


def identifier(value: str) -> None:
    if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
        raise ValueError("Invalid runtime identity")


def digest(value: str) -> None:
    if not isinstance(value, str) or not _DIGEST.fullmatch(value):
        raise ValueError("Invalid runtime digest")


def integer(value: int, *, minimum: int = 0, maximum: int = MAX_RESOURCE_COUNTER) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ValueError("Invalid runtime counter")
    return value


@dataclass(frozen=True, slots=True)
class RuntimeIdentity:
    target_node_id: str
    intent_id: str

    def __post_init__(self) -> None:
        identifier(self.target_node_id)
        identifier(self.intent_id)


@dataclass(frozen=True, slots=True)
class RuntimeDeadline:
    # Retain the entire original timestamp for identity/dispatch, including
    # sub-microsecond precision. Only eligibility comparisons floor to DB us.
    text: str
    at_us: int = field(init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.text, str) or not _RFC3339.fullmatch(self.text):
            raise ValueError("Invalid runtime deadline")
        try:
            parsed = datetime.fromisoformat(self.text.upper().replace("Z", "+00:00")).astimezone(
                UTC
            )
        except OverflowError:
            raise ValueError("Runtime deadline exceeds supported time range") from None
        elapsed = parsed - datetime(1970, 1, 1, tzinfo=UTC)
        at_us = (elapsed.days * 86400 + elapsed.seconds) * 1000000 + elapsed.microseconds
        integer(at_us)
        object.__setattr__(self, "at_us", at_us)


@dataclass(frozen=True, slots=True)
class RuntimeMemory:
    architecture: MemoryArchitecture
    peak: PhysicalResources
    steady: PhysicalResources

    def __post_init__(self) -> None:
        for value in (self.peak, self.steady):
            value.require_architecture(self.architecture)
            if any(value.values[3:]):
                raise ValueError("Runtime memory reservation carries memory counters only")
        if not self.steady.fits(self.peak):
            raise ValueError("Runtime peak must cover steady memory")


@dataclass(frozen=True, slots=True)
class RuntimeBinding:
    target_node_id: str
    pool_id: str
    gateway_id: str
    approved_template_id: str
    binding_revision_id: str
    manifest_id: str
    config_digest: str

    def __post_init__(self) -> None:
        for value in (
            self.target_node_id,
            self.gateway_id,
            self.approved_template_id,
            self.binding_revision_id,
            self.manifest_id,
        ):
            identifier(value)
        if self.pool_id not in POOLS:
            raise ValueError("Invalid runtime pool")
        digest(self.config_digest)


@dataclass(frozen=True, slots=True)
class RuntimeIntent:
    intent_id: str
    binding: RuntimeBinding
    instance_id: str
    instance_generation: int
    action: RuntimeAction
    fencing_token: int
    reserved_memory: RuntimeMemory
    deadline: RuntimeDeadline

    def __post_init__(self) -> None:
        identifier(self.intent_id)
        identifier(self.instance_id)
        integer(self.instance_generation, minimum=1)
        integer(self.fencing_token, minimum=1)
        if self.action not in ("prepare", "stop"):
            raise ValueError("Invalid runtime action")

    @property
    def identity(self) -> RuntimeIdentity:
        return RuntimeIdentity(self.binding.target_node_id, self.intent_id)


@dataclass(frozen=True, slots=True)
class ApprovedRuntimePlan:
    """Trusted immutable server facts, never resolved from a client-supplied plan.

    The composition must obtain approval/qualification and availability from
    its configured authority. Tests use explicit synthetic facts; this value
    does not itself grant or prove real hardware/model qualification.
    """

    binding: RuntimeBinding
    topology_digest: str
    qualification_digest: str
    memory_architecture: MemoryArchitecture
    service_class: ServiceClass
    loading_policy: LoadingPolicy
    max_slots: int
    realtime_reserved_slots: int
    peak: PhysicalResources
    steady: PhysicalResources
    per_slot: PhysicalResources

    def __post_init__(self) -> None:
        digest(self.topology_digest)
        digest(self.qualification_digest)
        integer(self.max_slots, minimum=1)
        integer(self.realtime_reserved_slots)
        if self.realtime_reserved_slots > self.max_slots:
            raise ValueError("Realtime reservation exceeds instance slots")
        if self.service_class not in (
            "realtime",
            "interactive",
            "batch",
        ) or self.loading_policy not in ("resident", "on_demand"):
            raise ValueError("Invalid runtime policy")
        for vector in (self.peak, self.steady, self.per_slot):
            vector.require_architecture(self.memory_architecture)
        if not self.steady.fits(self.peak):
            raise ValueError("Runtime peak must cover every steady resource dimension")

    @property
    def memory(self) -> RuntimeMemory:
        return RuntimeMemory(
            self.memory_architecture,
            PhysicalResources(*self.peak.values[:3], 0, 0, 0),
            PhysicalResources(*self.steady.values[:3], 0, 0, 0),
        )


@dataclass(frozen=True, slots=True)
class CreateRuntimeIntent:
    request: RuntimeIntent
    expected_head_version: int | None
    approved_plan: ApprovedRuntimePlan | None
    owner_id: str

    def __post_init__(self) -> None:
        identifier(self.owner_id)
        if self.expected_head_version is not None:
            integer(self.expected_head_version, minimum=1, maximum=MAX_VERSION)


@dataclass(frozen=True, slots=True)
class RuntimeHost:
    node_id: str
    memory_architecture: MemoryArchitecture
    topology_digest: str
    node_state: str
    version: int
    capacity: PhysicalResources
    held: PhysicalResources
    quarantined: PhysicalResources

    def __post_init__(self) -> None:
        identifier(self.node_id)
        digest(self.topology_digest)
        integer(self.version, minimum=1, maximum=MAX_VERSION)
        for vector in (self.capacity, self.held, self.quarantined):
            vector.require_architecture(self.memory_architecture)
        reserve_resources(
            capacity=self.capacity,
            held=self.held,
            quarantined=self.quarantined,
            requested=PhysicalResources.zero(),
        )


@dataclass(frozen=True, slots=True)
class RuntimeHead:
    instance_id: str
    current_generation: int
    generation_high_water: int
    prepare_intent_id: str | None
    stop_intent_id: str | None
    version: int

    def __post_init__(self) -> None:
        identifier(self.instance_id)
        integer(self.current_generation, minimum=1)
        integer(self.generation_high_water, minimum=self.current_generation)
        integer(self.version, minimum=1, maximum=MAX_VERSION)
        for identity in (self.prepare_intent_id, self.stop_intent_id):
            if identity is not None:
                identifier(identity)


@dataclass(frozen=True, slots=True)
class RuntimeInstance:
    instance_id: str
    instance_generation: int
    plan: ApprovedRuntimePlan
    instance_state: str
    base_budget_kind: BaseBudgetKind
    held_slots: int
    quarantined_slots: int
    last_fencing_token: int
    version: int
    stopped_at_us: int | None

    def __post_init__(self) -> None:
        identifier(self.instance_id)
        integer(self.instance_generation, minimum=1)
        integer(self.held_slots)
        integer(self.quarantined_slots)
        integer(self.last_fencing_token, minimum=1)
        integer(self.version, minimum=1, maximum=MAX_VERSION)
        if self.held_slots + self.quarantined_slots > self.plan.max_slots:
            raise ValueError("Invalid existing runtime slots")
        if self.base_budget_kind not in ("peak", "steady", "q_peak", "q_steady", "released"):
            raise ValueError("Invalid runtime budget kind")
        if self.stopped_at_us is not None:
            integer(self.stopped_at_us, minimum=1)


@dataclass(frozen=True, slots=True)
class RuntimePrerequisites:
    all_leases_released: bool
    all_submits_terminal: bool
    all_runtime_intents_terminal: bool
    verified_stopped: bool
    fenced_quarantine_cleanup: bool

    def __post_init__(self) -> None:
        if any(
            type(value) is not bool
            for value in (
                self.all_leases_released,
                self.all_submits_terminal,
                self.all_runtime_intents_terminal,
                self.verified_stopped,
                self.fenced_quarantine_cleanup,
            )
        ):
            raise ValueError("Runtime prerequisite facts must be explicit booleans")


class RuntimeRejectedError(Exception):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def require_runtime_authority(
    ownership: PoolOwnership, request: RuntimeIntent, now_us: int, *, owner_id: str
) -> None:
    try:
        integer(now_us, minimum=1)
    except ValueError:
        raise RuntimeRejectedError("invalid_counter") from None
    if (
        ownership.pool_id != request.binding.pool_id
        or ownership.owner_id != owner_id
        or ownership.fencing_token != request.fencing_token
        or ownership.expires_at_us <= now_us
    ):
        raise RuntimeRejectedError("stale_fencing_token")
    if request.action == "prepare" and ownership.owner_state != "active":
        raise RuntimeRejectedError("reconciliation_required")
    if request.deadline.at_us <= now_us:
        raise RuntimeRejectedError("lease_not_active")


def prepare_resources(
    command: CreateRuntimeIntent,
    *,
    ownership: PoolOwnership,
    pool_state: str,
    service_class: str,
    host: RuntimeHost,
    head: RuntimeHead | None,
    previous: RuntimeInstance | None,
    prerequisites: RuntimePrerequisites,
    now_us: int,
) -> PhysicalResources:
    request, plan = command.request, command.approved_plan
    require_runtime_authority(ownership, request, now_us, owner_id=command.owner_id)
    if request.action != "prepare" or plan is None:
        raise RuntimeRejectedError("unapproved_runtime_template")
    if plan.binding != request.binding or plan.memory != request.reserved_memory:
        raise RuntimeRejectedError("invalid_runtime_request")
    if pool_state != "active" or host.node_state != "active" or service_class != plan.service_class:
        raise RuntimeRejectedError("policy_or_resource_disabled")
    if (
        host.node_id != request.binding.target_node_id
        or host.memory_architecture != plan.memory_architecture
        or host.topology_digest != plan.topology_digest
    ):
        raise RuntimeRejectedError("recipe_mismatch")
    if head is None:
        if command.expected_head_version is not None:
            raise RuntimeRejectedError("version_conflict")
        if previous is not None:
            raise RuntimeRejectedError("reconciliation_required")
    else:
        if command.expected_head_version != head.version:
            raise RuntimeRejectedError("version_conflict")
        if (
            request.instance_id != head.instance_id
            or request.instance_generation <= head.generation_high_water
        ):
            raise RuntimeRejectedError("stale_instance_generation")
        if (
            previous is None
            or previous.instance_id != head.instance_id
            or previous.instance_generation != head.current_generation
        ):
            raise RuntimeRejectedError("reconciliation_required")
        if (
            previous.instance_state != "stopped"
            or previous.base_budget_kind != "released"
            or previous.stopped_at_us is None
            or previous.stopped_at_us > now_us
            or previous.held_slots
            or previous.quarantined_slots
            or not all(
                (
                    prerequisites.all_leases_released,
                    prerequisites.all_submits_terminal,
                    prerequisites.all_runtime_intents_terminal,
                    prerequisites.verified_stopped,
                )
            )
        ):
            raise RuntimeRejectedError("gateway_drain_incomplete")
    try:
        integer(host.version + 1, minimum=1, maximum=MAX_VERSION)
        if head is not None:
            integer(head.version + 1, minimum=1, maximum=MAX_VERSION)
        return reserve_resources(
            capacity=host.capacity,
            held=host.held,
            quarantined=host.quarantined,
            requested=plan.peak,
        )
    except CapacityExceededError:
        raise RuntimeRejectedError("capacity_exhausted") from None
    except ValueError:
        raise RuntimeRejectedError("invalid_counter") from None


def require_stop(
    command: CreateRuntimeIntent,
    *,
    ownership: PoolOwnership,
    head: RuntimeHead | None,
    instance: RuntimeInstance | None,
    prerequisites: RuntimePrerequisites,
    now_us: int,
) -> None:
    request = command.request
    require_runtime_authority(ownership, request, now_us, owner_id=command.owner_id)
    if request.action != "stop":
        raise RuntimeRejectedError("invalid_runtime_request")
    if head is None or instance is None:
        raise RuntimeRejectedError("not_found")
    if command.expected_head_version != head.version:
        raise RuntimeRejectedError("version_conflict")
    if (
        request.instance_id != head.instance_id
        or request.instance_generation != head.current_generation
        or instance.instance_id != head.instance_id
        or instance.instance_generation != head.current_generation
    ):
        raise RuntimeRejectedError("stale_instance_generation")
    if request.binding != instance.plan.binding or request.reserved_memory != instance.plan.memory:
        raise RuntimeRejectedError("invalid_runtime_request")
    if request.fencing_token < instance.last_fencing_token:
        raise RuntimeRejectedError("stale_fencing_token")
    if instance.instance_state == "stopped" or instance.base_budget_kind == "released":
        raise RuntimeRejectedError("invalid_runtime_request")
    if (
        instance.held_slots
        or not prerequisites.all_submits_terminal
        or not prerequisites.all_runtime_intents_terminal
    ):
        raise RuntimeRejectedError("gateway_drain_incomplete")
    if not prerequisites.all_leases_released and not prerequisites.fenced_quarantine_cleanup:
        raise RuntimeRejectedError("gateway_drain_incomplete")
    if instance.quarantined_slots and not prerequisites.fenced_quarantine_cleanup:
        raise RuntimeRejectedError("gateway_drain_incomplete")
    try:
        integer(head.version + 1, minimum=1, maximum=MAX_VERSION)
        integer(instance.version + 1, minimum=1, maximum=MAX_VERSION)
    except ValueError:
        raise RuntimeRejectedError("invalid_counter") from None


@dataclass(frozen=True, slots=True)
class RuntimeRecord:
    request: RuntimeIntent
    approved_plan: ApprovedRuntimePlan
    creator_id: str
    request_digest: str
    intent_state: RuntimeIntentState
    recorded_at_us: int
    updated_at_us: int
    version: int
    # Validated original response retained for replay at the boundary. Create
    # decisions never infer readiness or release from this opaque document.
    observation_document: str | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        identifier(self.creator_id)
        digest(self.request_digest)
        integer(self.recorded_at_us, minimum=1)
        integer(self.updated_at_us, minimum=self.recorded_at_us)
        integer(self.version, minimum=1, maximum=MAX_VERSION)
        if (
            self.request.binding != self.approved_plan.binding
            or self.request.reserved_memory != self.approved_plan.memory
        ):
            raise ValueError("Stored runtime request must match its immutable plan")
        if self.intent_state not in (
            "pending",
            "reconciling",
            "succeeded",
            "failed",
            "quarantined",
        ):
            raise ValueError("Invalid runtime intent state")
        if self.observation_document is None:
            if self.intent_state not in ("pending", "reconciling", "quarantined"):
                raise ValueError("An unobserved runtime cannot claim terminal completion")
        elif not isinstance(self.observation_document, str) or not self.observation_document:
            raise ValueError("Invalid original runtime observation")

    @property
    def identity(self) -> RuntimeIdentity:
        return self.request.identity


@dataclass(frozen=True, slots=True)
class RuntimeResult:
    outcome: Literal["committed", "replayed", "conflict", "rejected", "unknown"]
    identity: RuntimeIdentity
    fact: RuntimeRecord | None = None
    error_code: str | None = None
    recovery: Literal["none", "retry_transaction", "query_original", "reconcile"] = "none"

    def __post_init__(self) -> None:
        if self.outcome in ("committed", "replayed"):
            if (
                self.fact is None
                or self.fact.identity != self.identity
                or self.error_code is not None
                or self.recovery != "none"
            ):
                raise ValueError("Runtime success requires the complete original record")
        elif self.fact is not None or not self.error_code:
            raise ValueError("Unresolved runtime outcome cannot expose success")
        if self.outcome == "unknown" and (
            self.error_code != "outcome_unknown" or self.recovery != "query_original"
        ):
            raise ValueError("Unknown runtime outcome only permits original-identity recovery")


def rejected(identity: RuntimeIdentity, code: str) -> RuntimeResult:
    return RuntimeResult(
        "conflict"
        if code in {"version_conflict", "intent_conflict", "idempotency_conflict"}
        else "rejected",
        identity,
        error_code=code,
        recovery="reconcile",
    )


def unknown(identity: RuntimeIdentity) -> RuntimeResult:
    return RuntimeResult(
        "unknown", identity, error_code="outcome_unknown", recovery="query_original"
    )
