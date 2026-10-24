"""Transport leases for original runtime commands; never physical authority."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from speechnexus_backend.speech.domain.owners import MAX_VERSION
from speechnexus_backend.speech.domain.runtime_intents import (
    RuntimeIntent,
    digest,
    identifier,
    integer,
)

DeliveryPhase = Literal["unsent", "unknown"]
ClaimOutcome = Literal["claimed", "empty", "blocked"]
DeliveryWriteOutcome = Literal["committed", "stale", "unknown", "rejected"]


class DeliveryUnavailableError(Exception):
    """The enclosing storage transaction did not establish a usable permit."""


@dataclass(frozen=True, slots=True)
class DeliveryClaimRequest:
    holder_id: str
    target_node_ids: tuple[str, ...]
    lease_us: int

    def __post_init__(self) -> None:
        identifier(self.holder_id)
        if (
            not isinstance(self.target_node_ids, tuple)
            or not 1 <= len(self.target_node_ids) <= 64
            or any(not isinstance(node, str) for node in self.target_node_ids)
            or len(set(self.target_node_ids)) != len(self.target_node_ids)
        ):
            raise ValueError("Delivery requires explicit unique configured targets")
        for node in self.target_node_ids:
            identifier(node)
        integer(self.lease_us, minimum=1000000, maximum=300000000)


@dataclass(frozen=True, slots=True)
class DeliveryTicket:
    event_id: str
    request_digest: str
    holder_id: str
    version: int
    claim_expires_at_us: int
    phase: DeliveryPhase
    original: RuntimeIntent = field(repr=False)

    def __post_init__(self) -> None:
        identifier(self.event_id)
        identifier(self.holder_id)
        digest(self.request_digest)
        integer(self.version, minimum=1, maximum=MAX_VERSION)
        integer(self.claim_expires_at_us, minimum=1)
        if self.phase not in ("unsent", "unknown") or not isinstance(self.original, RuntimeIntent):
            raise ValueError("Delivery ticket requires the complete original runtime intent")


@dataclass(frozen=True, slots=True)
class DeliveryClaim:
    outcome: ClaimOutcome
    ticket: DeliveryTicket | None = None

    def __post_init__(self) -> None:
        if self.outcome not in ("claimed", "empty", "blocked") or (
            (self.outcome == "claimed") != (self.ticket is not None)
        ):
            raise ValueError("A claimed delivery requires exactly one original ticket")


@dataclass(frozen=True, slots=True)
class DeliveryStart:
    outcome: DeliveryWriteOutcome
    ticket: DeliveryTicket | None = None
    submit_allowed: bool = False

    def __post_init__(self) -> None:
        if self.outcome not in ("committed", "stale", "unknown", "rejected"):
            raise ValueError("Invalid delivery start result")
        if type(self.submit_allowed) is not bool or (
            (self.outcome == "committed") != (self.ticket is not None)
        ):
            raise ValueError("A committed start requires its acknowledged storage version")
        if self.ticket is not None and self.ticket.phase != "unknown":
            raise ValueError("A committed start must retain a durable unknown marker")
        if self.submit_allowed and (self.outcome != "committed" or self.ticket is None):
            raise ValueError("A submit permit requires a durable unknown marker")


@dataclass(frozen=True, slots=True)
class DeliveryWrite:
    outcome: DeliveryWriteOutcome

    def __post_init__(self) -> None:
        if self.outcome not in ("committed", "stale", "unknown", "rejected"):
            raise ValueError("Invalid delivery write result")


@dataclass(slots=True)
class DeliveryBatch:
    claimed: int = 0
    acknowledged: int = 0
    unknown: int = 0
    stale: int = 0
    blocked: int = 0
