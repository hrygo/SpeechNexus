"""Original bounded abort identity and the bounded attempt that carries it out."""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from speechnexus_gateway.domain.submission import (
    InvalidExecutionRequestError,
    SubmissionIntent,
)

ABORT_REASONS = frozenset(
    {
        "client_cancelled",
        "deadline_exceeded",
        "quota_exceeded",
        "resource_force_disabled",
        "gateway_takeover_reconcile",
        "stream_disconnected",
    }
)

#: Allowance between the claimed engine wait and the lease the attempt holds.
#: The adapter must settle within ``wait_timeout_ms``; the rest covers the commit
#: that stored the claim and the scheduling gap before the call starts.
ABORT_DISPATCH_GRACE_MS = 1_000
#: Longest lease any single attempt may hold; the contract wait is at most 60 s.
MAX_ABORT_DISPATCH_LEASE_MS = 65_000
#: Bounded retries for an attempt that learned nothing. Eight attempts with the
#: backoff below span about an hour, after which the receipt is given up.
MAX_ABORT_DISPATCH_ATTEMPTS = 8
#: First retry delay; each undecided attempt doubles it up to the cap.
ABORT_RETRY_BASE_MS = 30_000
ABORT_RETRY_MAX_MS = 1_800_000


def abort_retry_delay_ms(attempts: int) -> int:
    """Delay before the next attempt after the given number of undecided attempts."""
    if type(attempts) is not int or attempts < 1:
        raise ValueError("a retry delay requires at least one recorded attempt")
    return int(min(ABORT_RETRY_BASE_MS * 2 ** (attempts - 1), ABORT_RETRY_MAX_MS))


def abort_dispatch_lease_ms(wait_timeout_ms: int) -> int:
    """Bound one external attempt by the engine wait it was given."""
    lease_ms = wait_timeout_ms + ABORT_DISPATCH_GRACE_MS
    if not 100 <= lease_ms <= MAX_ABORT_DISPATCH_LEASE_MS:
        raise InvalidExecutionRequestError("abort dispatch lease is outside its bounds")
    return lease_ms


@dataclass(frozen=True, slots=True)
class AbortExecutionRequest:
    """One original cancellation request, optionally carrying its recorded origin.

    The HTTP body names only the first five contract fields. Recovery adds the
    instance and generation the receipt was recorded against, so it can re-verify
    the authoritative execution before any external send.
    """

    submit_intent_id: str
    operation_id: str
    attempt_id: str
    fencing_token: int
    reason: str
    wait_timeout_ms: int
    instance_id: str | None = None
    instance_generation: int | None = None

    def __post_init__(self) -> None:
        if self.reason not in ABORT_REASONS:
            raise InvalidExecutionRequestError("unknown abort reason")
        if type(self.fencing_token) is not int or self.fencing_token < 1:
            raise InvalidExecutionRequestError("abort requires a positive fencing token")
        if type(self.wait_timeout_ms) is not int or not 100 <= self.wait_timeout_ms <= 60000:
            raise InvalidExecutionRequestError("abort wait must be within the contract bounds")
        if (self.instance_id is None) != (self.instance_generation is None):
            raise InvalidExecutionRequestError(
                "abort origin requires both the instance and its generation"
            )
        if self.instance_id is not None and not 1 <= len(self.instance_id) <= 128:
            raise InvalidExecutionRequestError("abort origin requires a bounded instance id")
        if self.instance_generation is not None and (
            type(self.instance_generation) is not int or self.instance_generation < 1
        ):
            raise InvalidExecutionRequestError("abort origin requires a positive generation")

    @property
    def origin_known(self) -> bool:
        return self.instance_id is not None and self.instance_generation is not None


@dataclass(frozen=True, slots=True)
class AbortDispatchClaim:
    """The bounded lease exactly one caller holds for one external abort attempt."""

    intent: SubmissionIntent
    attempt_id: str
    started_at: datetime
    deadline_at: datetime
    attempts: int

    def __post_init__(self) -> None:
        if not 1 <= len(self.attempt_id) <= 128:
            raise ValueError("an abort attempt requires a bounded identity")
        if self.deadline_at <= self.started_at:
            raise ValueError("an abort dispatch lease must end after it starts")
        if type(self.attempts) is not int or not 1 <= self.attempts <= MAX_ABORT_DISPATCH_ATTEMPTS:
            raise ValueError("an abort attempt must stay inside the bounded attempts")


class AbortDispatchOutcome(StrEnum):
    """The durable disposition of one external abort attempt."""

    #: The engine gave a decidable answer for the original request identity.
    ACKNOWLEDGED = "acknowledged"
    #: The attempt ended without learning anything; the original intent is retried.
    UNCONFIRMED = "unconfirmed"
    #: The authoritative execution no longer matches the receipt; never resend.
    REJECTED_FENCED = "rejected_fenced"
    #: The bounded attempts are spent; the original intent stops being retried.
    EXHAUSTED = "exhausted"

    @property
    def decided(self) -> bool:
        return self is not AbortDispatchOutcome.UNCONFIRMED


@dataclass(frozen=True, slots=True)
class AbortDispatchResult:
    """What one attempt established, written only for the attempt that produced it."""

    submit_intent_id: str
    attempt_id: str
    outcome: AbortDispatchOutcome
    observed_at: datetime

    def __post_init__(self) -> None:
        if not 1 <= len(self.submit_intent_id) <= 128:
            raise ValueError("an abort dispatch result requires an original intent")
        if not 1 <= len(self.attempt_id) <= 128:
            raise ValueError("an abort dispatch result requires an attempt identity")
        if not isinstance(self.outcome, AbortDispatchOutcome):
            raise ValueError("an abort dispatch result requires a known outcome")
