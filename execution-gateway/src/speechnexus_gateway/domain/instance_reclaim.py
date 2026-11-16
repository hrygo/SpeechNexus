"""One judgement table for reclaiming an idle on-demand instance generation.

Reclaiming returns capacity, so the conservative direction is the only correct one: a
generation with unfinished work, a quarantined execution, a held lease or an unverified
stop stays, and only a verified process or container termination reports ``reclaimed``. The
table lives here once so the drain report, the release gate and the reclaim answer cannot
disagree about what "idle and stopped" means.

Three inputs are deliberately not clocks or liveness: the idle span is measured from the
database observation instant against recorded execution activity, the cooldown is the
manifest value rather than a default of this module's own, and release comes from
:class:`~speechnexus_gateway.domain.voice_design_release.VoiceDesignReleaseEvidence`, whose
only releasing kinds are a process exit and a container stop. A heartbeat, a status change,
an engine sleep acknowledgment or a timeout cannot reach ``reclaimed`` through this table.

The decision authorizes nothing. It is a report the scheduler reads before it stops an
instance, exactly like the drain inventory the same scheduler already reads.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from speechnexus_gateway.domain.fencing import POOLS
from speechnexus_gateway.domain.instance_drain import (
    DrainInventory,
    DrainState,
    drain_state_for,
)
from speechnexus_gateway.domain.submission import InvalidExecutionRequestError
from speechnexus_gateway.domain.voice_design_release import VoiceDesignReleaseEvidence

#: The contract's bounds for a manifest ``idle_reclaim_cooldown_ms`` on an on-demand load.
#: A deployment that configures less than the floor is refused rather than silently
#: reclaimed faster than the contract allows; the ceiling only bounds arithmetic.
MIN_IDLE_RECLAIM_COOLDOWN_MS = 1000
MAX_IDLE_RECLAIM_COOLDOWN_MS = 7 * 24 * 60 * 60 * 1000

#: One millisecond, the unit both the cooldown and the reported idle span use.
_MS = timedelta(milliseconds=1)
_IDENTIFIER = re.compile(r"^[a-z0-9][a-z0-9._:-]{1,126}[a-z0-9]$")
_MAX_SAFE_INTEGER = 2**53 - 1
_ON_DEMAND = "on_demand"
_RESIDENT = "resident"


class ReclaimState(StrEnum):
    """What the recorded facts say about reclaiming one instance generation."""

    #: Idle past its cooldown with nothing left to prove; the scheduler may stop it.
    RECLAIM_DUE = "reclaim_due"
    #: The generation is already terminated and its resources are confirmed released.
    RECLAIMED = "reclaimed"
    #: A recorded condition forbids reclaiming; ``refusal`` names which one.
    HELD = "held"


class ReclaimRefusal(StrEnum):
    """The one condition that keeps a generation; each names a distinct missing fact."""

    #: A resident loading policy never reclaims on idleness.
    RESIDENT_POLICY = "resident_policy"
    #: The gateway still holds submitting or in-flight work for this generation.
    UNFINISHED_WORK = "unfinished_work"
    #: An execution of this generation is quarantined, so its stop was never verified.
    UNCONFIRMED_STOP = "unconfirmed_stop"
    #: A lease admitted against this generation still reserves its capacity.
    CAPACITY_HELD = "capacity_held"
    #: Neither readiness nor execution activity was recorded, so idleness is unknown.
    ACTIVITY_UNKNOWN = "activity_unknown"
    #: Less than the manifest cooldown has passed since the last recorded activity.
    COOLDOWN_ACTIVE = "cooldown_active"
    #: A stop was reported but it does not prove the process or container released.
    RELEASE_UNCONFIRMED = "release_unconfirmed"


@dataclass(frozen=True, slots=True)
class ReclaimFacts:
    """Everything the table reads about one generation, already recorded and read-only."""

    loading_policy: str
    inventory: DrainInventory
    holds_capacity: bool
    cooldown_ms: int
    observed_at: datetime
    idle_anchor: datetime | None = None
    release: VoiceDesignReleaseEvidence | None = None

    def __post_init__(self) -> None:
        if self.loading_policy not in {_ON_DEMAND, _RESIDENT}:
            raise InvalidExecutionRequestError("unknown loading policy")
        if not isinstance(self.inventory, DrainInventory):
            raise InvalidExecutionRequestError("reclaim requires the recorded drain inventory")
        if type(self.holds_capacity) is not bool:
            raise InvalidExecutionRequestError("holds_capacity must be a recorded boolean")
        if (
            type(self.cooldown_ms) is not int
            or not MIN_IDLE_RECLAIM_COOLDOWN_MS <= self.cooldown_ms <= MAX_IDLE_RECLAIM_COOLDOWN_MS
        ):
            raise InvalidExecutionRequestError(
                "idle reclaim cooldown must be the manifest value within its bounds"
            )
        if not isinstance(self.observed_at, datetime) or self.observed_at.tzinfo is None:
            raise InvalidExecutionRequestError("reclaim instants require a timezone")
        if self.idle_anchor is not None and (
            not isinstance(self.idle_anchor, datetime)
            or self.idle_anchor.tzinfo is None
            or self.idle_anchor > self.observed_at
        ):
            raise InvalidExecutionRequestError("an idle anchor cannot follow the observation")
        if self.release is not None and (
            not isinstance(self.release, VoiceDesignReleaseEvidence)
            or self.release.observed_at > self.observed_at
        ):
            raise InvalidExecutionRequestError("release evidence cannot follow the observation")


@dataclass(frozen=True, slots=True)
class ReclaimDecision:
    """The table's answer for one generation: reclaim due, reclaimed, or held and why."""

    state: ReclaimState
    cooldown_ms: int
    refusal: ReclaimRefusal | None = None
    idle_ms: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.state, ReclaimState):
            raise InvalidExecutionRequestError("unknown reclaim state")
        if (
            type(self.cooldown_ms) is not int
            or not MIN_IDLE_RECLAIM_COOLDOWN_MS <= self.cooldown_ms <= MAX_IDLE_RECLAIM_COOLDOWN_MS
        ):
            raise InvalidExecutionRequestError("a decision carries the cooldown it applied")
        if (self.state is ReclaimState.HELD) != (self.refusal is not None):
            raise InvalidExecutionRequestError("only a held decision names its refusal")
        if self.refusal is not None and not isinstance(self.refusal, ReclaimRefusal):
            raise InvalidExecutionRequestError("unknown reclaim refusal")
        if self.idle_ms is not None and (type(self.idle_ms) is not int or self.idle_ms < 0):
            raise InvalidExecutionRequestError("an idle span is a non-negative millisecond count")
        if self.state is ReclaimState.RECLAIM_DUE and self.idle_ms is None:
            raise InvalidExecutionRequestError("a reclaimable decision reports its idle span")

    @property
    def reclaim_due(self) -> bool:
        """Whether the scheduler may stop this generation now."""
        return self.state is ReclaimState.RECLAIM_DUE

    @property
    def reclaimed(self) -> bool:
        """Whether the generation is already terminated and its capacity is free."""
        return self.state is ReclaimState.RECLAIMED

    def value(self) -> dict[str, object]:
        """The stable, secret-free rendering of this decision."""
        return {
            "reclaim_state": str(self.state),
            "refusal": None if self.refusal is None else str(self.refusal),
            "idle_ms": self.idle_ms,
            "idle_reclaim_cooldown_ms": self.cooldown_ms,
        }


@dataclass(frozen=True, slots=True)
class ReclaimRecord:
    """One generation's recorded reclaim inputs, read in a single statement.

    The identity columns come from the instance row and the rest from the leases and
    intents admitted against it, so ``loading_policy``, the idle anchor, the drain
    inventory and the held capacity cannot disagree with one another.
    """

    loading_policy: str
    binding_revision_id: str
    last_fencing_token: int
    observed_at: datetime
    inventory: DrainInventory
    holds_capacity: bool
    idle_anchor: datetime | None = None

    def __post_init__(self) -> None:
        if self.loading_policy not in {_ON_DEMAND, _RESIDENT}:
            raise InvalidExecutionRequestError("unknown loading policy")
        if (
            type(self.binding_revision_id) is not str
            or not 1 <= len(self.binding_revision_id) <= 128
        ):
            raise InvalidExecutionRequestError("reclaim requires the recorded binding revision")
        if type(self.last_fencing_token) is not int or self.last_fencing_token < 0:
            raise InvalidExecutionRequestError("reclaim requires the recorded fencing token")
        if not isinstance(self.observed_at, datetime) or self.observed_at.tzinfo is None:
            raise InvalidExecutionRequestError("reclaim instants require a timezone")
        if not isinstance(self.inventory, DrainInventory):
            raise InvalidExecutionRequestError("reclaim requires the recorded drain inventory")
        if type(self.holds_capacity) is not bool:
            raise InvalidExecutionRequestError("holds_capacity must be a recorded boolean")
        if self.idle_anchor is not None and (
            not isinstance(self.idle_anchor, datetime)
            or self.idle_anchor.tzinfo is None
            or self.idle_anchor > self.observed_at
        ):
            raise InvalidExecutionRequestError("an idle anchor cannot follow the observation")

    def facts(
        self, *, cooldown_ms: int, release: VoiceDesignReleaseEvidence | None = None
    ) -> ReclaimFacts:
        """Pair the recorded facts with the manifest cooldown and the release report."""
        return ReclaimFacts(
            loading_policy=self.loading_policy,
            inventory=self.inventory,
            holds_capacity=self.holds_capacity,
            cooldown_ms=cooldown_ms,
            observed_at=self.observed_at,
            idle_anchor=self.idle_anchor,
            release=release,
        )


@dataclass(frozen=True, slots=True)
class ReclaimRequest:
    """One generation the scheduler asks about; it authorizes no stop by itself."""

    instance_id: str
    host_node_id: str
    pool_id: str
    instance_generation: int
    fencing_token: int
    cooldown_ms: int

    def __post_init__(self) -> None:
        for name in ("instance_id", "host_node_id"):
            value = getattr(self, name)
            if type(value) is not str or not _IDENTIFIER.fullmatch(value):
                raise InvalidExecutionRequestError(f"invalid reclaim {name}")
        if self.pool_id not in POOLS:
            raise InvalidExecutionRequestError("unknown ingress pool")
        if (
            type(self.instance_generation) is not int
            or not 1 <= self.instance_generation <= _MAX_SAFE_INTEGER
        ):
            raise InvalidExecutionRequestError("instance_generation must be a positive integer")
        if type(self.fencing_token) is not int or not 1 <= self.fencing_token <= _MAX_SAFE_INTEGER:
            raise InvalidExecutionRequestError("fencing token exceeds the safe integer domain")
        if (
            type(self.cooldown_ms) is not int
            or not MIN_IDLE_RECLAIM_COOLDOWN_MS <= self.cooldown_ms <= MAX_IDLE_RECLAIM_COOLDOWN_MS
        ):
            raise InvalidExecutionRequestError(
                "idle reclaim cooldown must be the manifest value within its bounds"
            )


def decide(facts: ReclaimFacts) -> ReclaimDecision:
    """Apply the reclaim judgement table to one generation's recorded facts.

    Release evidence is checked after the gateway's own unfinished work: a terminated
    generation whose executions this gateway has not closed yet is not fully released
    here, and the recovery sweep must close them first.
    """
    if facts.loading_policy != _ON_DEMAND:
        return _held(ReclaimRefusal.RESIDENT_POLICY, cooldown_ms=facts.cooldown_ms)
    drain_state = drain_state_for(facts.inventory)
    if drain_state is DrainState.QUARANTINED:
        return _held(ReclaimRefusal.UNCONFIRMED_STOP, cooldown_ms=facts.cooldown_ms)
    if drain_state is DrainState.DRAINING:
        return _held(ReclaimRefusal.UNFINISHED_WORK, cooldown_ms=facts.cooldown_ms)
    release = facts.release
    if release is not None:
        if release.is_release_evidence:
            return ReclaimDecision(state=ReclaimState.RECLAIMED, cooldown_ms=facts.cooldown_ms)
        return _held(ReclaimRefusal.RELEASE_UNCONFIRMED, cooldown_ms=facts.cooldown_ms)
    if facts.holds_capacity:
        return _held(ReclaimRefusal.CAPACITY_HELD, cooldown_ms=facts.cooldown_ms)
    anchor = facts.idle_anchor
    if anchor is None:
        return _held(ReclaimRefusal.ACTIVITY_UNKNOWN, cooldown_ms=facts.cooldown_ms)
    idle_ms = (facts.observed_at - anchor) // _MS
    if idle_ms < facts.cooldown_ms:
        return _held(ReclaimRefusal.COOLDOWN_ACTIVE, cooldown_ms=facts.cooldown_ms, idle_ms=idle_ms)
    return ReclaimDecision(
        state=ReclaimState.RECLAIM_DUE, cooldown_ms=facts.cooldown_ms, idle_ms=idle_ms
    )


def _held(
    refusal: ReclaimRefusal, *, cooldown_ms: int, idle_ms: int | None = None
) -> ReclaimDecision:
    return ReclaimDecision(
        state=ReclaimState.HELD, cooldown_ms=cooldown_ms, refusal=refusal, idle_ms=idle_ms
    )


__all__ = [
    "MAX_IDLE_RECLAIM_COOLDOWN_MS",
    "MIN_IDLE_RECLAIM_COOLDOWN_MS",
    "ReclaimDecision",
    "ReclaimFacts",
    "ReclaimRecord",
    "ReclaimRefusal",
    "ReclaimRequest",
    "ReclaimState",
    "decide",
]
