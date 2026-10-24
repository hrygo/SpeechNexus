"""Drain one engine instance generation and report its in-flight work honestly.

``contracts/internal/execution-control.openapi.yaml`` freezes three drain states, and two
of them are safety assertions: ``drained`` requires every count to be zero and
``safe_to_stop_instance`` must be true, while ``draining``/``quarantined`` must never claim
that the instance may be stopped. So this module derives the state from a freshly read
inventory and never from an intention: unfinished work means ``draining``, and any intent
whose stop was never confirmed means ``quarantined``. Nothing here releases capacity, and
stopping an instance still belongs to the Scheduler.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from speechnexus_gateway.domain.fencing import POOLS
from speechnexus_gateway.domain.submission import (
    InvalidExecutionRequestError,
    contract_instant_text,
)

_IDENTIFIER_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._:-]{1,126}[a-z0-9]$")
_MAX_SAFE_INTEGER = 2**53 - 1
_MIN_DRAIN_WAIT_MS = 100
_MAX_DRAIN_WAIT_MS = 120000


class DrainState(StrEnum):
    """The frozen drain states of one engine instance generation."""

    DRAINING = "draining"
    DRAINED = "drained"
    QUARANTINED = "quarantined"


@dataclass(frozen=True, slots=True)
class InstanceIdentity:
    """The recorded generation identity of one engine instance, as the gateway reads it."""

    instance_id: str
    instance_generation: int
    host_node_id: str
    pool_id: str
    gateway_id: str


@dataclass(frozen=True, slots=True)
class DrainInventory:
    """Unfinished intents of one instance generation, in the contract's three buckets."""

    submitting_intent_ids: tuple[str, ...]
    active_intent_ids: tuple[str, ...]
    quarantined_intent_ids: tuple[str, ...]

    @property
    def unfinished_intent_ids(self) -> tuple[str, ...]:
        """Every intent that still holds capacity and may still own engine work."""
        return tuple(sorted((*self.submitting_intent_ids, *self.active_intent_ids)))

    @property
    def holds_unfinished_work(self) -> bool:
        return bool(self.submitting_intent_ids or self.active_intent_ids)

    @property
    def has_quarantined(self) -> bool:
        return bool(self.quarantined_intent_ids)


def drain_state_for(inventory: DrainInventory) -> DrainState:
    """Derive the frozen state: an unconfirmed stop outranks unfinished work."""
    if inventory.has_quarantined:
        return DrainState.QUARANTINED
    if inventory.holds_unfinished_work:
        return DrainState.DRAINING
    return DrainState.DRAINED


@dataclass(frozen=True, slots=True)
class DrainRequest:
    """One bounded drain of an instance generation, as the caller asked for it."""

    instance_id: str
    host_node_id: str
    pool_id: str
    instance_generation: int
    fencing_token: int
    max_drain_wait_ms: int

    def __post_init__(self) -> None:
        if not _IDENTIFIER_PATTERN.match(self.instance_id):
            raise InvalidExecutionRequestError("invalid instance identity")
        if not _IDENTIFIER_PATTERN.match(self.host_node_id):
            raise InvalidExecutionRequestError("invalid host node identity")
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
            type(self.max_drain_wait_ms) is not int
            or not _MIN_DRAIN_WAIT_MS <= self.max_drain_wait_ms <= _MAX_DRAIN_WAIT_MS
        ):
            raise InvalidExecutionRequestError("drain wait must be within the contract bounds")


@dataclass(frozen=True, slots=True)
class DrainSnapshot:
    """What the gateway observed for one instance generation during this request."""

    request: DrainRequest
    inventory: DrainInventory
    observed_at: datetime

    @property
    def state(self) -> DrainState:
        return drain_state_for(self.inventory)

    def response(self) -> dict[str, object]:
        """Render the frozen response; only a drained generation is safe to stop."""
        return {
            "instance_id": self.request.instance_id,
            "instance_generation": self.request.instance_generation,
            "drain_state": self.state.value,
            "active_intent_count": len(self.inventory.active_intent_ids),
            "submitting_intent_count": len(self.inventory.submitting_intent_ids),
            "quarantined_intent_count": len(self.inventory.quarantined_intent_ids),
            "safe_to_stop_instance": self.state is DrainState.DRAINED,
            "updated_at": contract_instant_text(self.observed_at),
        }
