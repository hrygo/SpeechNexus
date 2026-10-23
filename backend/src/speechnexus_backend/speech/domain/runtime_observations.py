"""Actual runtime facts and budget decisions, independent of wire formats/SQL."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from speechnexus_backend.speech.domain.capacity import PhysicalResources
from speechnexus_backend.speech.domain.owners import MAX_VERSION, PoolOwnership
from speechnexus_backend.speech.domain.runtime_intents import (
    ApprovedRuntimePlan,
    BaseBudgetKind,
    RuntimeAction,
    RuntimeBinding,
    RuntimeHead,
    RuntimeHost,
    RuntimeIdentity,
    RuntimeInstance,
    RuntimeIntent,
    RuntimeIntentState,
    RuntimeMemory,
    RuntimeRecord,
    RuntimeRejectedError,
    digest,
    identifier,
    integer,
)

_OBSERVED_STATES = frozenset(
    {
        "absent",
        "preparing",
        "loading",
        "warming_up",
        "ready",
        "draining",
        "stopping",
        "stopped",
        "failed",
        "quarantined",
    }
)
_INTENT_STATES = frozenset({"pending", "reconciling", "succeeded", "failed", "quarantined"})
_PHASE = {"preparing": 0, "loading": 1, "warming_up": 2}


def _fraction(value: str) -> None:
    if (
        not isinstance(value, str)
        or len(value) > 65536
        or (value and (not re.fullmatch(r"[0-9]+", value) or value.endswith("0")))
    ):
        raise ValueError("Sub-microsecond time requires a canonical decimal fraction")


@dataclass(frozen=True, slots=True)
class RuntimeObservedStop:
    process_or_container_exited: bool
    resources_released_confirmed: bool
    verified_at_us: int
    verified_submicrosecond: str = ""

    def __post_init__(self) -> None:
        if (
            self.process_or_container_exited is not True
            or self.resources_released_confirmed is not True
        ):
            raise ValueError("Both physical stop facts are required")
        integer(self.verified_at_us)
        _fraction(self.verified_submicrosecond)


@dataclass(frozen=True, slots=True)
class RuntimeObservation:
    intent_id: str
    binding: RuntimeBinding
    instance_id: str
    instance_generation: int
    action: RuntimeAction
    fencing_token: int
    reserved_memory: RuntimeMemory
    intent_state: RuntimeIntentState
    observed_instance_state: str
    created_at_us: int
    updated_at_us: int
    observed_peak_memory: PhysicalResources | None = None
    stop_evidence: RuntimeObservedStop | None = None
    error_code: str | None = None
    created_submicrosecond: str = ""
    updated_submicrosecond: str = ""

    def __post_init__(self) -> None:
        identifier(self.intent_id)
        identifier(self.instance_id)
        integer(self.instance_generation, minimum=1)
        integer(self.fencing_token, minimum=1)
        integer(self.created_at_us)
        integer(self.updated_at_us, minimum=self.created_at_us)
        _fraction(self.created_submicrosecond)
        _fraction(self.updated_submicrosecond)
        if (self.updated_at_us, self.updated_submicrosecond) < (
            self.created_at_us,
            self.created_submicrosecond,
        ):
            raise ValueError("Runtime observation update precedes its creation")
        if (
            self.action not in ("prepare", "stop")
            or self.intent_state not in _INTENT_STATES
            or self.observed_instance_state not in _OBSERVED_STATES
        ):
            raise ValueError("Invalid runtime observation state")
        if self.error_code is not None and (
            not isinstance(self.error_code, str)
            or not re.fullmatch(r"[a-z][a-z0-9_]{2,63}", self.error_code)
        ):
            raise ValueError("Invalid runtime observation error")
        if self.intent_state == "failed" and self.error_code is None:
            raise ValueError("Failed runtime intent requires its actual stable error")
        if self.intent_state == "succeeded" and self.error_code is not None:
            raise ValueError("Successful runtime intent cannot carry an error")
        if self.observed_peak_memory is not None:
            self.observed_peak_memory.require_architecture(self.reserved_memory.architecture)
            if any(self.observed_peak_memory.values[3:]):
                raise ValueError("Observed memory carries memory dimensions only")
        if self.stop_evidence is not None:
            if (
                self.action != "stop"
                or self.intent_state != "succeeded"
                or self.observed_instance_state not in ("stopped", "absent")
            ):
                raise ValueError("Stop evidence must accompany actual stop success")
            integer(
                self.stop_evidence.verified_at_us,
                minimum=self.created_at_us,
                maximum=self.updated_at_us,
            )
            verified = (
                self.stop_evidence.verified_at_us,
                self.stop_evidence.verified_submicrosecond,
            )
            if (
                not (self.created_at_us, self.created_submicrosecond)
                <= verified
                <= (self.updated_at_us, self.updated_submicrosecond)
            ):
                raise ValueError("Physical stop time must belong to its actual observation")
        if self.intent_state == "succeeded":
            if self.action == "prepare" and self.observed_instance_state != "ready":
                raise ValueError("Prepare success must observe ready")
            if self.action == "stop" and self.stop_evidence is None:
                raise ValueError("Stop success requires actual release evidence")

    @property
    def identity(self) -> RuntimeIdentity:
        return RuntimeIdentity(self.binding.target_node_id, self.intent_id)

    def matches(self, request: RuntimeIntent) -> bool:
        return (
            self.identity == request.identity
            and self.binding == request.binding
            and self.instance_id == request.instance_id
            and self.instance_generation == request.instance_generation
            and self.action == request.action
            and self.fencing_token == request.fencing_token
            and self.reserved_memory == request.reserved_memory
        )


@dataclass(frozen=True, slots=True)
class VerifiedRuntimeReadiness:
    """Trusted provider receipt, never a client assertion or proof by construction."""

    binding: RuntimeBinding
    instance_id: str
    instance_generation: int
    fencing_token: int
    topology_digest: str
    qualification_digest: str
    evidence_digest: str
    verified_at_us: int
    expires_at_us: int

    def __post_init__(self) -> None:
        identifier(self.instance_id)
        integer(self.instance_generation, minimum=1)
        integer(self.fencing_token, minimum=1)
        for value in (self.topology_digest, self.qualification_digest, self.evidence_digest):
            digest(value)
        integer(self.verified_at_us, minimum=1)
        integer(self.expires_at_us, minimum=self.verified_at_us + 1)

    def require_valid(
        self, observation: RuntimeObservation, plan: ApprovedRuntimePlan, now_us: int
    ) -> None:
        if (
            self.binding != observation.binding
            or self.binding != plan.binding
            or self.instance_id != observation.instance_id
            or self.instance_generation != observation.instance_generation
            or self.fencing_token != observation.fencing_token
            or self.topology_digest != plan.topology_digest
            or self.qualification_digest != plan.qualification_digest
            or not self.verified_at_us <= now_us < self.expires_at_us
        ):
            raise RuntimeRejectedError("reconciliation_required")


@dataclass(frozen=True, slots=True)
class ObserveRuntimeIntent:
    observation: RuntimeObservation
    expected_instance_version: int
    expected_intent_version: int
    owner_id: str
    original_document: str = field(repr=False)
    readiness: VerifiedRuntimeReadiness | None = None

    def __post_init__(self) -> None:
        identifier(self.owner_id)
        if (
            not isinstance(self.original_document, str)
            or not self.original_document
            or len(self.original_document.encode()) > 65536
        ):
            raise ValueError("Observation requires its complete bounded original document")
        integer(self.expected_instance_version, minimum=1, maximum=MAX_VERSION)
        integer(self.expected_intent_version, minimum=1, maximum=MAX_VERSION)


@dataclass(frozen=True, slots=True)
class RuntimeObservationPrerequisites:
    all_leases_released: bool
    all_submits_terminal: bool
    other_runtime_intents_terminal: bool

    def __post_init__(self) -> None:
        if any(
            type(value) is not bool
            for value in (
                self.all_leases_released,
                self.all_submits_terminal,
                self.other_runtime_intents_terminal,
            )
        ):
            raise ValueError("Observation prerequisites must be explicit facts")


@dataclass(frozen=True, slots=True)
class RuntimeObservationDecision:
    held: PhysicalResources
    quarantined: PhysicalResources
    host_state: str
    instance_state: str
    base_budget_kind: BaseBudgetKind
    intent_state: RuntimeIntentState
    ready_at_us: int | None
    stopped_at_us: int | None
    quarantine_reason: str | None


def _base(instance: RuntimeInstance) -> PhysicalResources:
    if instance.base_budget_kind in ("peak", "q_peak"):
        return instance.plan.peak
    if instance.base_budget_kind in ("steady", "q_steady"):
        return instance.plan.steady
    raise RuntimeRejectedError("reconciliation_required")


def decide_runtime_observation(
    command: ObserveRuntimeIntent,
    *,
    record: RuntimeRecord,
    head: RuntimeHead,
    instance: RuntimeInstance,
    host: RuntimeHost,
    ownership: PoolOwnership,
    prerequisites: RuntimeObservationPrerequisites,
    now_us: int,
) -> RuntimeObservationDecision:
    observation = command.observation
    request = record.request
    if not observation.matches(request) or instance.plan != record.approved_plan:
        raise RuntimeRejectedError("invalid_runtime_request")
    if (
        command.expected_instance_version != instance.version
        or command.expected_intent_version != record.version
    ):
        raise RuntimeRejectedError("version_conflict")
    if (
        head.instance_id != request.instance_id
        or head.current_generation != request.instance_generation
        or instance.instance_id != request.instance_id
        or instance.instance_generation != request.instance_generation
    ):
        raise RuntimeRejectedError("stale_instance_generation")
    pointer = head.prepare_intent_id if request.action == "prepare" else head.stop_intent_id
    if pointer != request.intent_id or (
        request.action == "prepare" and head.stop_intent_id is not None
    ):
        raise RuntimeRejectedError("version_conflict")
    try:
        integer(now_us, minimum=1)
    except ValueError:
        raise RuntimeRejectedError("invalid_counter") from None
    if (
        ownership.owner_id != command.owner_id
        or ownership.pool_id != request.binding.pool_id
        or ownership.expires_at_us <= now_us
        or ownership.fencing_token < request.fencing_token
        or instance.last_fencing_token != request.fencing_token
    ):
        raise RuntimeRejectedError("stale_fencing_token")
    if host.node_id != request.binding.target_node_id or (
        observation.updated_at_us,
        observation.updated_submicrosecond,
    ) > (now_us, ""):
        raise RuntimeRejectedError("reconciliation_required")
    if instance.base_budget_kind.startswith("q_") and not instance.quarantine_reason:
        raise RuntimeRejectedError("reconciliation_required")
    if instance.base_budget_kind == "released" or instance.stopped_at_us is not None:
        raise RuntimeRejectedError("reconciliation_required")
    try:
        integer(instance.version + 1, minimum=1, maximum=MAX_VERSION)
        integer(record.version + 1, minimum=1, maximum=MAX_VERSION)
        integer(host.version + 1, minimum=1, maximum=MAX_VERSION)
    except ValueError:
        raise RuntimeRejectedError("invalid_counter") from None
    held, quarantined, host_state = host.held, host.quarantined, host.node_state
    state = instance.instance_state
    kind: BaseBudgetKind = instance.base_budget_kind
    ready_at, stopped_at, reason = (
        instance.ready_at_us,
        instance.stopped_at_us,
        instance.quarantine_reason,
    )
    intent_state = observation.intent_state
    stale = ownership.fencing_token != request.fencing_token
    over_peak = (
        observation.observed_peak_memory is not None
        and not observation.observed_peak_memory.fits(instance.plan.peak)
    )
    stop_success = request.action == "stop" and observation.intent_state == "succeeded"
    isolate = stale or (
        not stop_success
        and (
            over_peak
            or observation.intent_state == "quarantined"
            or observation.observed_instance_state == "quarantined"
        )
    )
    try:
        base = _base(instance)
        if isolate:
            if kind in ("peak", "steady"):
                held, quarantined = held.subtract(base), quarantined.add(base)
                kind = "q_peak" if kind == "peak" else "q_steady"
            state = "quarantined"
            reason = reason or (
                "runtime observation belongs to a superseded owner epoch"
                if stale
                else "observed runtime memory exceeds the approved peak"
                if over_peak
                else "controller reports quarantined runtime intent or instance"
            )
            if host_state == "active" and (
                over_peak
                or any(
                    q * 1000000 > capacity * host.quarantine_threshold_ppm
                    for q, capacity in zip(quarantined.values, host.capacity.values, strict=True)
                )
            ):
                host_state = "quarantined"
        elif request.action == "prepare":
            if observation.intent_state == "succeeded":
                if kind in ("q_peak", "q_steady") or state in ("quarantined", "failed"):
                    # Record the actual terminal fact so fenced cleanup can
                    # proceed, without restoring admission or shrinking a hold.
                    state = "quarantined" if kind.startswith("q_") else state
                else:
                    if command.readiness is None or ownership.owner_state != "active":
                        raise RuntimeRejectedError("reconciliation_required")
                    command.readiness.require_valid(observation, instance.plan, now_us)
                    if (
                        host.topology_digest != instance.plan.topology_digest
                        or host.memory_architecture != instance.plan.memory_architecture
                        or host.node_state != "active"
                    ):
                        raise RuntimeRejectedError("recipe_mismatch")
                    if (
                        kind != "peak"
                        or state not in _PHASE
                        or instance.held_slots
                        or instance.quarantined_slots
                        or not prerequisites.all_leases_released
                        or not prerequisites.all_submits_terminal
                    ):
                        raise RuntimeRejectedError("reconciliation_required")
                    held = held.subtract(base).add(instance.plan.steady)
                    kind, state = "steady", "ready"
                    ready_at = ready_at if ready_at is not None else now_us
            elif observation.intent_state == "failed":
                state = "quarantined" if kind.startswith("q_") else "failed"
            elif not kind.startswith("q_"):
                phase = observation.observed_instance_state
                if phase == "absent":
                    phase = "preparing"
                if phase not in _PHASE or state not in _PHASE or _PHASE[phase] < _PHASE[state]:
                    raise RuntimeRejectedError("reconciliation_required")
                state = phase
        elif observation.intent_state == "succeeded":
            if (
                all(
                    (
                        prerequisites.all_leases_released,
                        prerequisites.all_submits_terminal,
                        prerequisites.other_runtime_intents_terminal,
                    )
                )
                and not instance.held_slots
                and not instance.quarantined_slots
            ):
                if observation.stop_evidence is None or (
                    observation.stop_evidence.verified_at_us,
                    observation.stop_evidence.verified_submicrosecond,
                ) > (now_us, ""):
                    raise RuntimeRejectedError("reconciliation_required")
                if kind.startswith("q_"):
                    quarantined = quarantined.subtract(base)
                else:
                    held = held.subtract(base)
                kind, state, stopped_at = "released", "stopped", now_us
            else:
                # Physical stop is recorded for downstream cleanup. Lease
                # release must finish before the base can be returned.
                intent_state = "reconciling"
                state = "quarantined" if kind.startswith("q_") else "stopping"
        elif observation.intent_state == "failed":
            state = "quarantined" if kind.startswith("q_") else "failed"
        elif not kind.startswith("q_"):
            phase = (
                "stopping"
                if observation.observed_instance_state in ("stopping", "stopped", "absent")
                else "draining"
            )
            if state not in ("draining", "stopping") or (
                state == "stopping" and phase == "draining"
            ):
                raise RuntimeRejectedError("reconciliation_required")
            state = phase
        if host_state == "active" and any(
            q * 1000000 > capacity * host.quarantine_threshold_ppm
            for q, capacity in zip(quarantined.values, host.capacity.values, strict=True)
        ):
            host_state = "quarantined"
        if not held.add(quarantined).fits(host.capacity):
            raise ValueError("Corrupt physical ledger")
    except ValueError:
        raise RuntimeRejectedError("invalid_counter") from None
    return RuntimeObservationDecision(
        held, quarantined, host_state, state, kind, intent_state, ready_at, stopped_at, reason
    )


def require_observation_progress(previous: RuntimeObservation, current: RuntimeObservation) -> None:
    if (
        previous.identity != current.identity
        or previous.binding != current.binding
        or previous.instance_id != current.instance_id
        or previous.instance_generation != current.instance_generation
        or previous.action != current.action
        or previous.fencing_token != current.fencing_token
        or previous.reserved_memory != current.reserved_memory
        or (previous.created_at_us, previous.created_submicrosecond)
        != (current.created_at_us, current.created_submicrosecond)
    ):
        raise RuntimeRejectedError("intent_conflict")
    if (current.updated_at_us, current.updated_submicrosecond) < (
        previous.updated_at_us,
        previous.updated_submicrosecond,
    ):
        raise RuntimeRejectedError("version_conflict")
    if previous.intent_state in ("succeeded", "failed") and previous != current:
        raise RuntimeRejectedError("intent_conflict")
    if previous.observed_peak_memory is not None and (
        current.observed_peak_memory is None
        or not previous.observed_peak_memory.fits(current.observed_peak_memory)
    ):
        raise RuntimeRejectedError("intent_conflict")
    if previous.stop_evidence is not None and previous.stop_evidence != current.stop_evidence:
        raise RuntimeRejectedError("intent_conflict")
