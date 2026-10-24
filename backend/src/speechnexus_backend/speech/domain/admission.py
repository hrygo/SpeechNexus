"""Complete recipe decisions; no database, DTO, engine or provider dependencies."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Literal

from speechnexus_backend.speech.domain.capacity import (
    CapacityExceededError,
    PhysicalResources,
    reserve_resources,
)
from speechnexus_backend.speech.domain.jobs import CreateJob
from speechnexus_backend.speech.domain.owners import (
    MAX_VERSION,
    OwnerAuthority,
    OwnerRejectedError,
    PoolOwnership,
    require_authority,
)
from speechnexus_backend.speech.domain.owners import (
    identity as validate_pool_identity,
)
from speechnexus_backend.speech.domain.runtime_intents import (
    ApprovedRuntimePlan,
    RuntimeHead,
    RuntimeHost,
    RuntimeInstance,
    RuntimeRejectedError,
    digest,
    identifier,
    integer,
)
from speechnexus_backend.speech.domain.runtime_observations import (
    RuntimeObservation,
    VerifiedRuntimeReadiness,
)


@dataclass(frozen=True, slots=True)
class AdmissionIdentity:
    operation_id: str
    attempt_id: str

    def __post_init__(self) -> None:
        identifier(self.operation_id)
        identifier(self.attempt_id)


@dataclass(frozen=True, slots=True)
class StageAllocation:
    stage_id: str
    lease_id: str
    pool_id: str
    host_node_id: str
    gateway_id: str
    instance_id: str
    instance_generation: int
    expected_instance_version: int
    reserved_slots: int

    def __post_init__(self) -> None:
        for value in (
            self.stage_id,
            self.lease_id,
            self.host_node_id,
            self.gateway_id,
            self.instance_id,
        ):
            identifier(value)
        # Pool vocabulary and owner identities use the existing authority type.
        validate_pool_identity(self.pool_id, None)
        integer(self.instance_generation, minimum=1)
        integer(self.expected_instance_version, minimum=1, maximum=MAX_VERSION)
        integer(self.reserved_slots, minimum=1)

    @property
    def instance_key(self) -> tuple[str, int]:
        return self.instance_id, self.instance_generation


@dataclass(frozen=True, slots=True)
class AdmissionRequest:
    identity: AdmissionIdentity
    workspace_id: str
    binding_revision_id: str
    manifest_id: str
    recipe_digest: str
    policy_revision_id: str
    expected_quota_version: int
    service_class: str
    authorities: tuple[OwnerAuthority, ...]
    stages: tuple[StageAllocation, ...]
    deadline_at_us: int
    job: CreateJob

    def __post_init__(self) -> None:
        for value in (
            self.workspace_id,
            self.binding_revision_id,
            self.manifest_id,
            self.policy_revision_id,
        ):
            identifier(value)
        digest(self.recipe_digest)
        integer(self.expected_quota_version, minimum=1, maximum=MAX_VERSION)
        integer(self.deadline_at_us, minimum=1)
        if self.service_class not in ("realtime", "interactive", "batch"):
            raise ValueError("Invalid admission service class")
        if not isinstance(self.authorities, tuple) or not 1 <= len(self.authorities) <= 6:
            raise ValueError("Admission requires a bounded immutable set of authorities")
        if not isinstance(self.stages, tuple) or not 1 <= len(self.stages) <= 64:
            raise ValueError("Admission requires a bounded immutable complete stage set")
        if (self.job.operation_id, self.job.attempt_id) != (
            self.identity.operation_id,
            self.identity.attempt_id,
        ):
            raise ValueError("Admission Job belongs to another attempt")
        if (
            len({a.pool_id for a in self.authorities}) != len(self.authorities)
            or {a.pool_id for a in self.authorities} != {s.pool_id for s in self.stages}
            or len({s.stage_id for s in self.stages}) != len(self.stages)
            or len({s.lease_id for s in self.stages}) != len(self.stages)
        ):
            raise ValueError("Admission has duplicated or incomplete identities")
        versions: dict[tuple[str, int], int] = {}
        parents: dict[tuple[str, int], tuple[str, str, str]] = {}
        generations: dict[str, int] = {}
        for stage in self.stages:
            if (
                generations.setdefault(stage.instance_id, stage.instance_generation)
                != stage.instance_generation
            ):
                raise ValueError("One instance cannot admit two current generations")
            previous = versions.setdefault(stage.instance_key, stage.expected_instance_version)
            if previous != stage.expected_instance_version:
                raise ValueError("Shared generation has inconsistent expected versions")
            parent = stage.host_node_id, stage.pool_id, stage.gateway_id
            if parents.setdefault(stage.instance_key, parent) != parent:
                raise ValueError("Shared generation has inconsistent resource parents")


@dataclass(frozen=True, slots=True)
class OriginalBusinessCommand:
    """Trusted complete effective command; the business producer resolves pins/defaults.

    The infrastructure codec checks canonical JSON before accepting this value.
    Neither this value nor a valid digest proves source semantics or permission.
    The body never belongs in a public admission snapshot or exception message.
    """

    document: str = field(repr=False)
    request_digest: str

    def __post_init__(self) -> None:
        digest(self.request_digest)
        try:
            if not isinstance(self.document, str) or not self.document:
                raise ValueError
            expected = "sha256:" + hashlib.sha256(self.document.encode()).hexdigest()
            if expected != self.request_digest:
                raise ValueError
        except ValueError, UnicodeError:
            raise ValueError("Invalid original business command") from None


@dataclass(frozen=True, slots=True)
class ApprovedStage:
    stage_id: str
    minimum_slots: int
    runtime_plan: ApprovedRuntimePlan

    def __post_init__(self) -> None:
        identifier(self.stage_id)
        integer(self.minimum_slots, minimum=1)


@dataclass(frozen=True, slots=True)
class ApprovedAdmissionRecipe:
    """Explicit server authority; construction does not prove real qualification.

    No absent quota dimension means zero. Until a dimension has a transactional
    reservation ledger, a policy requiring it cannot be admitted by this port.
    """

    binding_revision_id: str
    manifest_id: str
    recipe_digest: str
    policy_revision_id: str
    service_class: str
    stages: tuple[ApprovedStage, ...]
    admission_expires_at_us: int
    quota_dimensions: frozenset[str]

    def __post_init__(self) -> None:
        for value in (self.binding_revision_id, self.manifest_id, self.policy_revision_id):
            identifier(value)
        digest(self.recipe_digest)
        integer(self.admission_expires_at_us, minimum=1)
        if self.service_class not in ("realtime", "interactive", "batch"):
            raise ValueError("Invalid approved admission class")
        if (
            not isinstance(self.stages, tuple)
            or not 1 <= len(self.stages) <= 64
            or len({s.stage_id for s in self.stages}) != len(self.stages)
            or not isinstance(self.quota_dimensions, frozenset)
            or not self.quota_dimensions
        ):
            raise ValueError("Approved admission requires explicit stages and quota dimensions")
        if any(not isinstance(d, str) or not d for d in self.quota_dimensions):
            raise ValueError("Invalid quota dimension")
        for stage in self.stages:
            binding = stage.runtime_plan.binding
            if (binding.binding_revision_id, binding.manifest_id) != (
                self.binding_revision_id,
                self.manifest_id,
            ):
                raise ValueError("Stage plan does not belong to the approved recipe")


@dataclass(frozen=True, slots=True)
class AdmitRecipe:
    request: AdmissionRequest
    business_command: OriginalBusinessCommand = field(repr=False)
    approved_recipe: ApprovedAdmissionRecipe

    def __post_init__(self) -> None:
        if (
            not isinstance(self.request, AdmissionRequest)
            or not isinstance(self.business_command, OriginalBusinessCommand)
            or not isinstance(self.approved_recipe, ApprovedAdmissionRecipe)
        ):
            raise ValueError("Admission requires explicit trusted server context")
        if self.request.job.request_digest != self.business_command.request_digest:
            raise ValueError("Admission and original business command digests differ")


@dataclass(frozen=True, slots=True)
class AdmissionQuota:
    workspace_id: str
    max_attempts: int
    held_attempts: int
    quarantined_attempts: int
    policy_revision_id: str
    version: int

    def __post_init__(self) -> None:
        identifier(self.workspace_id)
        identifier(self.policy_revision_id)
        for value in (self.max_attempts, self.held_attempts, self.quarantined_attempts):
            integer(value)
        integer(self.version, minimum=1, maximum=MAX_VERSION)
        if self.held_attempts + self.quarantined_attempts > self.max_attempts:
            raise ValueError("Invalid existing attempt quota")


@dataclass(frozen=True, slots=True)
class AdmissionPool:
    ownership: PoolOwnership
    pool_state: str
    service_class: str


@dataclass(frozen=True, slots=True)
class AdmissionRuntime:
    instance: RuntimeInstance
    head: RuntimeHead
    observation: RuntimeObservation
    readiness: VerifiedRuntimeReadiness
    batch_slots: int

    def __post_init__(self) -> None:
        integer(self.batch_slots)
        if self.batch_slots > self.instance.held_slots + self.instance.quarantined_slots:
            raise ValueError("Batch slots exceed all unreleased slots")


@dataclass(frozen=True, slots=True)
class StageReservation:
    stage: StageAllocation
    physical: PhysicalResources


@dataclass(frozen=True, slots=True)
class AdmissionDecision:
    reservations: tuple[StageReservation, ...]
    host_held: tuple[tuple[str, PhysicalResources], ...]
    instance_held_slots: tuple[tuple[tuple[str, int], int], ...]
    quota_held_attempts: int


class AdmissionRejectedError(Exception):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def require_live_authority(
    command: AdmitRecipe,
    pools: tuple[AdmissionPool, ...],
    runtimes: tuple[AdmissionRuntime, ...],
    now_us: int,
) -> None:
    """Use again after the final potentially waiting insert, before committing."""
    request, approved = command.request, command.approved_recipe
    try:
        integer(now_us, minimum=1)
    except ValueError:
        raise AdmissionRejectedError("invalid_counter") from None
    if now_us >= request.deadline_at_us:
        raise AdmissionRejectedError("lease_not_active")
    if now_us >= approved.admission_expires_at_us:
        raise AdmissionRejectedError("policy_or_resource_disabled")
    authorities = {a.pool_id: a for a in request.authorities}
    if {p.ownership.pool_id for p in pools} != set(authorities) or len(pools) != len(authorities):
        raise AdmissionRejectedError("reconciliation_required")
    for pool in pools:
        try:
            require_authority(pool.ownership, authorities[pool.ownership.pool_id], now_us)
        except OwnerRejectedError as error:
            raise AdmissionRejectedError(error.code) from None
        if pool.pool_state != "active":
            raise AdmissionRejectedError("policy_or_resource_disabled")
        if pool.ownership.owner_state != "active":
            raise AdmissionRejectedError("reconciliation_required")
    for runtime in runtimes:
        try:
            runtime.readiness.require_valid(runtime.observation, runtime.instance.plan, now_us)
        except RuntimeRejectedError as error:
            raise AdmissionRejectedError(error.code) from None
        if (
            runtime.instance.ready_at_us is None
            or not runtime.readiness.verified_at_us <= runtime.instance.ready_at_us <= now_us
        ):
            raise AdmissionRejectedError("reconciliation_required")


def decide_admission(
    command: AdmitRecipe,
    *,
    quota: AdmissionQuota,
    pools: tuple[AdmissionPool, ...],
    hosts: tuple[RuntimeHost, ...],
    runtimes: tuple[AdmissionRuntime, ...],
    now_us: int,
) -> AdmissionDecision:
    request, approved = command.request, command.approved_recipe
    if (
        request.binding_revision_id != approved.binding_revision_id
        or request.manifest_id != approved.manifest_id
        or request.recipe_digest != approved.recipe_digest
        or request.policy_revision_id != approved.policy_revision_id
        or request.service_class != approved.service_class
        or {s.stage_id for s in request.stages} != {s.stage_id for s in approved.stages}
    ):
        raise AdmissionRejectedError("recipe_mismatch")
    if approved.quota_dimensions != frozenset({"concurrent_attempts"}):
        raise AdmissionRejectedError("policy_or_resource_disabled")
    require_live_authority(command, pools, runtimes, now_us)
    if (
        quota.workspace_id != request.workspace_id
        or quota.policy_revision_id != request.policy_revision_id
    ):
        raise AdmissionRejectedError("policy_or_resource_disabled")
    if quota.version != request.expected_quota_version:
        raise AdmissionRejectedError("version_conflict")
    if quota.held_attempts + quota.quarantined_attempts >= quota.max_attempts:
        raise AdmissionRejectedError("quota_exceeded")
    pool_by_id = {p.ownership.pool_id: p for p in pools}
    host_by_id = {h.node_id: h for h in hosts}
    by_instance = {(r.instance.instance_id, r.instance.instance_generation): r for r in runtimes}
    if (
        len(host_by_id) != len(hosts)
        or set(host_by_id) != {s.host_node_id for s in request.stages}
        or len(by_instance) != len(runtimes)
        or set(by_instance) != {s.instance_key for s in request.stages}
    ):
        raise AdmissionRejectedError("reconciliation_required")
    by_stage = {s.stage_id: s for s in approved.stages}
    host_deltas = {key: PhysicalResources.zero() for key in host_by_id}
    slot_deltas = dict.fromkeys(by_instance, 0)
    reservations: list[StageReservation] = []
    try:
        for stage in sorted(request.stages, key=lambda s: s.stage_id):
            required = by_stage[stage.stage_id]
            runtime = by_instance[stage.instance_key]
            instance, plan = runtime.instance, required.runtime_plan
            binding = plan.binding
            host = host_by_id[stage.host_node_id]
            if (
                instance.plan != plan
                or (binding.pool_id, binding.target_node_id, binding.gateway_id)
                != (stage.pool_id, stage.host_node_id, stage.gateway_id)
                or runtime.head.instance_id != stage.instance_id
                or runtime.head.current_generation != stage.instance_generation
                or runtime.head.prepare_intent_id != runtime.observation.intent_id
                or runtime.observation.instance_id != stage.instance_id
                or runtime.observation.instance_generation != stage.instance_generation
                or host.topology_digest != plan.topology_digest
                or host.memory_architecture != plan.memory_architecture
                or stage.reserved_slots < required.minimum_slots
            ):
                raise AdmissionRejectedError("recipe_mismatch")
            if instance.version != stage.expected_instance_version:
                raise AdmissionRejectedError("version_conflict")
            if (
                instance.instance_state != "ready"
                or instance.base_budget_kind != "steady"
                or instance.stopped_at_us is not None
                or host.node_state != "active"
                or runtime.head.stop_intent_id is not None
                or runtime.observation.action != "prepare"
                or runtime.observation.intent_state != "succeeded"
                or runtime.observation.observed_instance_state != "ready"
                or instance.last_fencing_token != runtime.readiness.fencing_token
                or pool_by_id[stage.pool_id].service_class != plan.service_class
            ):
                raise AdmissionRejectedError("policy_or_resource_disabled")
            vector = plan.per_slot.scaled(stage.reserved_slots)
            vector.require_architecture(host.memory_architecture)
            host_deltas[host.node_id] = host_deltas[host.node_id].add(vector)
            slot_deltas[stage.instance_key] += stage.reserved_slots
            integer(slot_deltas[stage.instance_key], minimum=1)
            reservations.append(StageReservation(stage, vector))
        for key, extra in slot_deltas.items():
            runtime = by_instance[key]
            instance = runtime.instance
            plan = instance.plan
            if instance.held_slots + instance.quarantined_slots + extra > plan.max_slots or (
                request.service_class != "realtime"
                and runtime.batch_slots + extra + plan.realtime_reserved_slots > plan.max_slots
            ):
                raise AdmissionRejectedError("capacity_exhausted")
            integer(instance.version + 1, minimum=1, maximum=MAX_VERSION)
        new_held = tuple(
            (
                node,
                reserve_resources(
                    capacity=host_by_id[node].capacity,
                    held=host_by_id[node].held,
                    quarantined=host_by_id[node].quarantined,
                    requested=host_deltas[node],
                ),
            )
            for node in sorted(host_by_id)
        )
        for host in hosts:
            integer(host.version + 1, minimum=1, maximum=MAX_VERSION)
        integer(quota.version + 1, minimum=1, maximum=MAX_VERSION)
    except CapacityExceededError:
        raise AdmissionRejectedError("capacity_exhausted") from None
    except ValueError:
        raise AdmissionRejectedError("invalid_counter") from None
    return AdmissionDecision(
        tuple(reservations),
        new_held,
        tuple(
            (key, by_instance[key].instance.held_slots + slot_deltas[key])
            for key in sorted(by_instance)
        ),
        quota.held_attempts + 1,
    )


@dataclass(frozen=True, slots=True)
class AdmissionFact:
    identity: AdmissionIdentity
    reservations: tuple[StageReservation, ...]
    version: int

    def __post_init__(self) -> None:
        integer(self.version, minimum=1, maximum=MAX_VERSION)
        if not 1 <= len(self.reservations) <= 64:
            raise ValueError("Admission fact requires all original reservations")


@dataclass(frozen=True, slots=True)
class AdmissionResult:
    outcome: Literal["committed", "replayed", "conflict", "rejected", "unknown"]
    identity: AdmissionIdentity
    fact: AdmissionFact | None = None
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
                raise ValueError("Successful admission requires its original complete fact")
        elif self.fact is not None or self.error_code is None:
            raise ValueError("Unresolved admission cannot expose a committed fact")
        if self.outcome == "unknown" and (
            self.error_code != "outcome_unknown" or self.recovery != "query_original"
        ):
            raise ValueError("Unknown admission can only query its original identity")


def rejected(identity: AdmissionIdentity, code: str) -> AdmissionResult:
    conflict = code in ("version_conflict", "idempotency_conflict")
    return AdmissionResult(
        "conflict" if conflict else "rejected",
        identity,
        error_code=code,
        recovery="query_original"
        if conflict
        else "reconcile"
        if code == "reconciliation_required"
        else "none",
    )


def unknown(identity: AdmissionIdentity) -> AdmissionResult:
    return AdmissionResult(
        "unknown", identity, error_code="outcome_unknown", recovery="query_original"
    )
