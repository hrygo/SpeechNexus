"""Decide the bounded operator release of a completed execution's reservation.

``#852`` measured an accepted cancellation that could not return its instance
slot: the execution had already reached ``succeeded``, and the isolation rule
accepts only an ``unknown_quarantined``/``quarantined`` execution, so neither the
automatic judgment nor the quarantine release could reach a determinate state.
Two rules, each of them right, left the reservation held by nobody's decision.

This module owns the second, explicitly modeled operator exit for exactly that
shape. It does not borrow the unknown execution's testimony: a completed
execution is not an unknown one, and its release is a different claim. The claim
here is *"the engine instance generation this reservation reserved has nothing
left to run and may be stopped"*, and it is only accepted when it is pinned to
the instance and generation of the lease being released -- a testimony about any
other instance, any other generation, or a generation that is not drained
authorizes nothing.

Nothing here rewrites an execution fact. The gateway's own terminal response
stays the proof that the execution ended; the operator's testimony authorizes
returning the reservation that no other owner releases. The quarantine release
of an unknown execution keeps its own evidence rule (``quarantine_release``),
unchanged.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any, Final, Literal

import rfc8785

from speechnexus_backend.speech.domain.owners import MAX_VERSION
from speechnexus_backend.speech.domain.quarantine_release import QuarantineReleaseRejectedError
from speechnexus_backend.speech.domain.runtime_intents import (
    identifier,
    integer,
    runtime_timestamp_us,
)

#: Version of the canonical testimony document the evidence digest covers.
COMPLETED_EVIDENCE_VERSION: Final = "completed-release-evidence/v1"
#: The only drain state that may authorize returning a generation's reservation.
DRAINED: Final = "drained"
#: The gateway's frozen drain states; ``draining``/``quarantined`` never qualify.
DRAIN_STATES: Final = frozenset({"draining", "drained", "quarantined"})
#: What the gateway's own terminal recording writes for a successful execution.
TERMINAL_EVIDENCE_KIND: Final = "engine_terminal_response"
MAX_REASON: Final = 2000


class CompletedReleaseRejectedError(QuarantineReleaseRejectedError):
    """One stable refusal in the release vocabulary; the caller maps the code.

    It subclasses the release rejection so every adapter that already turns a
    release refusal into one stable code keeps doing exactly that for this exit;
    the name is still its own, because the two exits refuse different facts.
    """


@dataclass(frozen=True, slots=True)
class DrainRecord:
    """One gateway drain response, as the operator recorded it.

    Every field comes from the response body of
    ``POST /internal/v1/instances/{instance_id}/drain``; nothing is derived
    locally. ``drained`` is the contract's safety assertion, so it is the only
    value that can authorize a release: unreported or unfinished engine work must
    never be described as capacity that is safe to return.
    """

    instance_id: str
    instance_generation: int
    drain_state: str
    active_intent_count: int
    submitting_intent_count: int
    quarantined_intent_count: int
    safe_to_stop_instance: bool
    updated_at_us: int

    def __post_init__(self) -> None:
        identifier(self.instance_id)
        integer(self.instance_generation, minimum=1)
        if self.drain_state not in DRAIN_STATES:
            raise ValueError("Invalid drain state")
        integer(self.active_intent_count)
        integer(self.submitting_intent_count)
        integer(self.quarantined_intent_count)
        if type(self.safe_to_stop_instance) is not bool:
            raise ValueError("Invalid drain safety assertion")
        integer(self.updated_at_us, minimum=1)

    @property
    def unfinished_intent_count(self) -> int:
        return (
            self.active_intent_count + self.submitting_intent_count + self.quarantined_intent_count
        )

    @property
    def drained(self) -> bool:
        """Only zero unfinished work plus the gateway's own assertion qualifies."""
        return (
            self.drain_state == DRAINED
            and self.safe_to_stop_instance
            and self.unfinished_intent_count == 0
        )


@dataclass(frozen=True, slots=True)
class CompletedIntent:
    """The lease's single submit intent, as the completed-execution gate needs it."""

    submit_intent_id: str
    execution_state: str
    cleanup_state: str
    recorded_at_us: int
    dispatched_at_us: int | None
    terminated_at_us: int | None
    version: int

    def __post_init__(self) -> None:
        identifier(self.submit_intent_id)
        integer(self.recorded_at_us)
        if self.dispatched_at_us is not None:
            integer(self.dispatched_at_us)
        if self.terminated_at_us is not None:
            integer(self.terminated_at_us)
        integer(self.version, minimum=1, maximum=MAX_VERSION)


@dataclass(frozen=True, slots=True)
class CompletedStopEvidence:
    """The gateway's recorded terminal fact for the lease's submit intent."""

    evidence_kind: str
    observed_at_us: int
    execution_stopped: int
    resources_released: int

    def __post_init__(self) -> None:
        integer(self.observed_at_us)
        integer(self.execution_stopped)
        integer(self.resources_released)

    @property
    def proves_terminal_release(self) -> bool:
        """A complete claim only: the machine's own stop and release, both flags."""
        return (
            self.evidence_kind == TERMINAL_EVIDENCE_KIND
            and self.execution_stopped == 1
            and self.resources_released == 1
        )


@dataclass(frozen=True, slots=True)
class CompletedReleaseFacts:
    """Every locked fact the completed-execution release decides on."""

    lease_id: str
    operation_id: str
    instance_id: str
    instance_generation: int
    lease_state: str
    lease_cleanup_state: str
    budget_released_at_us: int | None
    quarantine_observed_at_us: int | None
    intents: tuple[CompletedIntent, ...]
    stop: CompletedStopEvidence | None
    drain: DrainRecord
    observed_at_us: int
    reason: str

    def __post_init__(self) -> None:
        identifier(self.lease_id)
        identifier(self.operation_id)
        identifier(self.instance_id)
        integer(self.instance_generation, minimum=1)
        if self.lease_state not in ("reserved", "executing", "stopping", "quarantined", "released"):
            raise ValueError("Invalid lease state")
        if self.lease_cleanup_state not in ("not_required", "pending", "complete", "quarantined"):
            raise ValueError("Invalid lease cleanup")
        if self.budget_released_at_us is not None:
            integer(self.budget_released_at_us)
        if self.quarantine_observed_at_us is not None:
            integer(self.quarantine_observed_at_us)
        if len(self.intents) > 1:
            raise ValueError("A lease carries at most one submit intent")
        integer(self.observed_at_us)
        if (
            not isinstance(self.reason, str)
            or len(self.reason) > MAX_REASON
            or not self.reason.strip()
        ):
            raise ValueError("Invalid completed release reason")


def completed_evidence_digest(
    *,
    lease_id: str,
    submit_intent_id: str,
    instance_id: str,
    instance_generation: int,
    drain: DrainRecord,
    observed_at_us: int,
    reason: str,
) -> str:
    """Canonical testimony digest: the reservation identity plus the drain record.

    The digest is what makes the testimony *pinned*. It names the lease, its
    submit intent and the exact instance generation, so a digest minted for
    another lease, another instance or another generation cannot authorize this
    one, and it carries the whole drain response, so "drained" and "not drained"
    never share one testimony.
    """
    value: Any = [
        COMPLETED_EVIDENCE_VERSION,
        {
            "lease_id": lease_id,
            "submit_intent_id": submit_intent_id,
            "instance_id": instance_id,
            "instance_generation": instance_generation,
            "drain": {
                "active_intent_count": drain.active_intent_count,
                "drain_state": drain.drain_state,
                "instance_generation": drain.instance_generation,
                "instance_id": drain.instance_id,
                "quarantined_intent_count": drain.quarantined_intent_count,
                "safe_to_stop_instance": drain.safe_to_stop_instance,
                "submitting_intent_count": drain.submitting_intent_count,
                "updated_at_us": drain.updated_at_us,
            },
            "observed_at_us": observed_at_us,
            "reason": reason,
        },
    ]
    try:
        encoded = rfc8785.dumps(value)
    except ValueError, TypeError, RecursionError, OverflowError:
        raise ValueError("Invalid completed release evidence") from None
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def prepared_digest(
    *,
    lease_id: str,
    submit_intent_id: str,
    instance_id: str,
    instance_generation: int,
    drain: DrainRecord,
    observed_at: str,
    reason: str,
) -> str:
    """Digest a caller can compute from the wire-shaped observed time."""
    return completed_evidence_digest(
        lease_id=lease_id,
        submit_intent_id=submit_intent_id,
        instance_id=instance_id,
        instance_generation=instance_generation,
        drain=drain,
        observed_at_us=runtime_timestamp_us(observed_at),
        reason=reason,
    )


def _require_pinned(facts: CompletedReleaseFacts) -> None:
    """The drain must name the generation this lease reserved, not one nearby."""
    if (
        facts.drain.instance_id != facts.instance_id
        or facts.drain.instance_generation != facts.instance_generation
    ):
        raise CompletedReleaseRejectedError("completed_release_drain_mismatch")


def _require_drained(facts: CompletedReleaseFacts) -> None:
    if not facts.drain.drained:
        raise CompletedReleaseRejectedError("completed_release_not_drained")


def require_observed_at(facts: CompletedReleaseFacts, intent: CompletedIntent, now_us: int) -> None:
    """The testimony is about a past observation of this execution, by database time.

    It may not precede the recorded submission, its dispatch or the execution's
    own end, and it may not be later than the server time taken after the last
    possible lock wait. Both are request content errors, never state conflicts.
    """
    integer(now_us)
    lower = intent.recorded_at_us
    for bound in (intent.dispatched_at_us, intent.terminated_at_us):
        if bound is not None:
            lower = max(lower, bound)
    if facts.observed_at_us < lower or facts.observed_at_us > now_us:
        raise CompletedReleaseRejectedError("invalid")


def require_completed_releasable(facts: CompletedReleaseFacts) -> CompletedIntent:
    """The one shape a completed-execution operator release may act on.

    Every clause is a refusal of a *different* fact, so an operator can tell why
    the exit does not apply instead of retrying evidence that cannot become true:
    the lease must be a first release, the execution must be terminal and
    successful, the gateway must have recorded its terminal response, and the
    drain must be of this lease's own instance generation and must be drained.
    """
    if (
        facts.lease_state != "reserved"
        or facts.lease_cleanup_state != "pending"
        or facts.budget_released_at_us is not None
        or facts.quarantine_observed_at_us is not None
    ):
        # Another release, an isolation or a stop owns this lease; the completed
        # exit is not a second way to do their work.
        raise CompletedReleaseRejectedError("quarantine_release_conflict")
    if len(facts.intents) != 1:
        raise CompletedReleaseRejectedError("quarantine_release_conflict")
    intent = facts.intents[0]
    if intent.execution_state != "succeeded" or intent.cleanup_state != "complete":
        # The exit exists for a completed execution only; an unknown or running
        # one belongs to isolation or to the stop path.
        raise CompletedReleaseRejectedError("quarantine_release_conflict")
    if facts.stop is None or not facts.stop.proves_terminal_release:
        # A successful execution without the gateway's own terminal and release
        # evidence is the shape #338 must repair, not something an operator
        # testimony may paper over.
        raise CompletedReleaseRejectedError("completed_release_requires_terminal_evidence")
    _require_pinned(facts)
    _require_drained(facts)
    return intent


def replay_conflict(code: str) -> CompletedReleaseRejectedError:
    """One refusal the ledger already decided, surfaced with the same vocabulary."""
    return CompletedReleaseRejectedError(code)


#: The two decisions the level-two transaction reaches, named for its caller.
type CompletedDecision = Literal["released", "replayed"]

__all__ = [
    "COMPLETED_EVIDENCE_VERSION",
    "DRAINED",
    "DRAIN_STATES",
    "TERMINAL_EVIDENCE_KIND",
    "CompletedDecision",
    "CompletedIntent",
    "CompletedReleaseFacts",
    "CompletedReleaseRejectedError",
    "CompletedStopEvidence",
    "DrainRecord",
    "completed_evidence_digest",
    "prepared_digest",
    "replay_conflict",
    "require_completed_releasable",
    "require_observed_at",
]
