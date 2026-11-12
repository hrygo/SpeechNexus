"""Recorded runtime instance generations and their bounded keyset page; no storage.

An instance is identified by the immutable ``(instance_id, generation)`` pair the
runtime instance store allocates on every restart. That pair is also the keyset
order, so a page boundary is a position in the key frontier rather than an
offset into a scan whose order the database happens to choose.

Every emitted value has an authoritative stored source: the ``engine_instances``
row and the immutable binding revision it names. Facts the instance store does
not record are absent from this projection instead of being synthesized: there
is no loaded artifact digest, no process start time, no heartbeat time and no
instance stop evidence document here. ``accepting_new_work`` is not an extra
observation; it follows directly from ``state`` and mirrors the contract
conditional that ties the two.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from speechnexus_backend.speech.domain.assets import counter, identifier
from speechnexus_backend.speech.domain.configuration import revision_number

type InstanceState = Literal[
    "preparing",
    "loading",
    "warming_up",
    "ready",
    "draining",
    "stopping",
    "stopped",
    "failed",
    "quarantined",
]

INSTANCE_STATES: tuple[InstanceState, ...] = (
    "preparing",
    "loading",
    "warming_up",
    "ready",
    "draining",
    "stopping",
    "stopped",
    "failed",
    "quarantined",
)
# Mirrors shared/identifiers.schema.json#/$defs/PoolId.
POOL_IDS = frozenset(
    {
        "asr-realtime",
        "tts-realtime",
        "asr-batch",
        "tts-batch",
        "voice-creation",
        "auxiliary",
    }
)
MAX_INSTANCE_LIMIT = 100
MAX_TIMESTAMP_US = 253_402_300_799_999_999
MAX_QUARANTINE_REASON = 2000
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")


class ModelInstanceUnavailableError(Exception):
    """Recorded runtime instance facts could not be read or trusted."""


@dataclass(frozen=True, slots=True, order=True)
class InstancePosition:
    """One total-order key: the instance id, then its generation.

    ``order=True`` compares exactly the two key columns, so the Python frontier
    and the SQL row comparison agree.
    """

    instance_id: str
    generation: int

    def __post_init__(self) -> None:
        identifier(self.instance_id)
        counter(self.generation, minimum=1)


@dataclass(frozen=True, slots=True)
class ModelInstance:
    """One recorded generation; every field came from a named stored column."""

    instance_id: str
    host_node_id: str
    gateway_id: str
    binding_id: str
    binding_revision: str
    binding_revision_id: str
    pool_id: str
    generation: int
    config_digest: str
    state: InstanceState
    ready_at_us: int | None
    stopped_at_us: int | None
    quarantine_reason: str | None

    def __post_init__(self) -> None:
        identifier(self.instance_id)
        identifier(self.host_node_id)
        identifier(self.gateway_id)
        identifier(self.binding_id)
        revision_number(self.binding_revision)
        identifier(self.binding_revision_id)
        counter(self.generation, minimum=1)
        if not isinstance(self.pool_id, str) or self.pool_id not in POOL_IDS:
            raise ValueError("Invalid instance pool")
        if not isinstance(self.config_digest, str) or not _DIGEST.fullmatch(self.config_digest):
            raise ValueError("Invalid instance configuration digest")
        if not isinstance(self.state, str) or self.state not in INSTANCE_STATES:
            raise ValueError("Invalid instance state")
        for timestamp in (self.ready_at_us, self.stopped_at_us):
            if timestamp is not None:
                counter(timestamp, minimum=0, maximum=MAX_TIMESTAMP_US)
        if self.state == "ready" and self.ready_at_us is None:
            raise ValueError("A ready instance requires its recorded readiness time")
        if self.state == "stopped" and self.stopped_at_us is None:
            raise ValueError("A stopped instance requires its recorded stop time")
        if self.state == "quarantined" and (
            not isinstance(self.quarantine_reason, str)
            or not self.quarantine_reason.strip()
            or len(self.quarantine_reason) > MAX_QUARANTINE_REASON
        ):
            raise ValueError("A quarantined instance requires its recorded reason")
        if self.quarantine_reason is not None and (
            not isinstance(self.quarantine_reason, str)
            or len(self.quarantine_reason) > MAX_QUARANTINE_REASON
        ):
            raise ValueError("Invalid instance quarantine reason")

    @property
    def accepting_new_work(self) -> bool:
        return self.state == "ready"

    @property
    def current_quarantine_reason(self) -> str | None:
        """The reason only while the instance is quarantined.

        The store keeps the recorded reason after the instance leaves quarantine;
        the contract confines the field to the quarantined state, so the
        projection reports the condition that holds now and keeps the historical
        text out of the response.
        """
        return self.quarantine_reason if self.state == "quarantined" else None

    @property
    def position(self) -> InstancePosition:
        return InstancePosition(self.instance_id, self.generation)


@dataclass(frozen=True, slots=True)
class ModelInstanceQuery:
    """One bounded page request; filters are part of the cursor identity."""

    limit: int
    after: InstancePosition | None = None
    host_node_id: str | None = None
    state: InstanceState | None = None

    def __post_init__(self) -> None:
        counter(self.limit, minimum=1, maximum=MAX_INSTANCE_LIMIT)
        if self.after is not None and not isinstance(self.after, InstancePosition):
            raise ValueError("Invalid instance cursor position")
        if self.host_node_id is not None:
            identifier(self.host_node_id)
        if self.state is not None and (
            not isinstance(self.state, str) or self.state not in INSTANCE_STATES
        ):
            raise ValueError("Invalid instance state filter")

    def matches(self, instance: ModelInstance) -> bool:
        return (
            (self.host_node_id is None or instance.host_node_id == self.host_node_id)
            and (self.state is None or instance.state == self.state)
            and (self.after is None or instance.position > self.after)
        )


@dataclass(frozen=True, slots=True)
class ModelInstancePage:
    """A strictly ascending page and the exact key frontier of its last row."""

    items: tuple[ModelInstance, ...]
    next_position: InstancePosition | None

    def __post_init__(self) -> None:
        if not isinstance(self.items, tuple):
            raise ValueError("An instance page requires a tuple of recorded rows")
        previous: InstancePosition | None = None
        for item in self.items:
            if not isinstance(item, ModelInstance):
                raise ValueError("An instance page may only carry recorded instances")
            if previous is not None and item.position <= previous:
                raise ValueError("An instance page must be strictly ascending")
            previous = item.position
        if self.next_position is not None and (previous is None or self.next_position != previous):
            raise ValueError("An instance page frontier must be its last row")


@dataclass(frozen=True, slots=True)
class ModelInstanceLookup:
    """Two-layer presence decision for one requested generation.

    ``instance_exists`` answers whether the instance id has any recorded
    generation at all; ``instance`` is the requested generation when it exists.
    Both can be false, but a present instance always implies the id exists.
    """

    instance_exists: bool
    instance: ModelInstance | None

    def __post_init__(self) -> None:
        if not isinstance(self.instance_exists, bool):
            raise ValueError("Invalid instance presence fact")
        if self.instance is not None and (
            not isinstance(self.instance, ModelInstance) or not self.instance_exists
        ):
            raise ValueError("A present instance requires its presence fact")
