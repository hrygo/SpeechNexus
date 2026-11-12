"""Aggregate pool facts and their validated projection; no storage imports.

A logical pool is not owned by one host node: instances of the same pool may run
on several nodes and each instance declares its own slot ceiling, realtime
reservation, held slots and quarantined slots. The administrative projection is
therefore the per-pool aggregate of the instances that still carry budget, not a
copy of one row. The aggregate keeps the two invariants the storage layer
enforces per instance:

* ``realtime_reserved_slots <= max_active_slots``
* ``active_slots + quarantine_slots <= max_active_slots``

Quarantined capacity is never reported as free capacity. A pool is only ``ready``
when at least one contributing node exists, the aggregate capacity is positive
and at least one contributing instance is actually serving; see
``docs/architecture/runtime-governance.md`` §1.4.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, cast

from speechnexus_backend.speech.domain.assets import counter, identifier
from speechnexus_backend.speech.domain.owners import POOLS

type PoolStatus = Literal["ready", "degraded", "unavailable"]
type ServiceClass = Literal["realtime", "interactive", "batch"]

SERVICE_CLASSES: tuple[ServiceClass, ...] = ("realtime", "interactive", "batch")
# The coordination table uses one accepting state; any other stored state means
# the owner is drawing down, disabled or otherwise not taking new work.
ACCEPTING_POOL_STATE = "active"
STATUSES: tuple[PoolStatus, ...] = ("ready", "degraded", "unavailable")
MAX_POOLS = len(POOLS)


class ResourcePoolsUnavailableError(Exception):
    """Authoritative pool aggregates could not be read or trusted."""


@dataclass(frozen=True, slots=True)
class ResourcePoolFacts:
    """One pool's stored state plus the aggregate of its contributing instances.

    ``ready_instances`` counts contributing instances whose stored state is
    ``ready``; it decides ``degraded`` versus ``ready`` and is never exposed.
    """

    pool_id: str
    pool_state: str
    service_class: str
    node_ids: tuple[str, ...]
    ready_instances: int
    max_active_slots: int
    realtime_reserved_slots: int
    active_slots: int
    quarantine_slots: int

    def __post_init__(self) -> None:
        if not isinstance(self.pool_id, str) or self.pool_id not in POOLS:
            raise ValueError("Invalid resource pool identity")
        if not isinstance(self.service_class, str) or self.service_class not in SERVICE_CLASSES:
            raise ValueError("Invalid resource pool service class")
        if not isinstance(self.pool_state, str) or not 1 <= len(self.pool_state) <= 24:
            raise ValueError("Invalid resource pool state")
        if not isinstance(self.node_ids, tuple):
            raise ValueError("Resource pool nodes must be an ordered tuple")
        if len(set(self.node_ids)) != len(self.node_ids):
            raise ValueError("Resource pool nodes must be unique")
        ordered = True
        previous = ""
        for node in self.node_ids:
            identifier(node)
            ordered = ordered and node > previous
            previous = node
        if not ordered:
            raise ValueError("Resource pool nodes are not ordered")
        counter(self.ready_instances)
        if self.ready_instances > 0 and not self.node_ids:
            raise ValueError("Resource pool instances require a contributing node")
        for value in (
            self.max_active_slots,
            self.realtime_reserved_slots,
            self.active_slots,
            self.quarantine_slots,
        ):
            counter(value)


@dataclass(frozen=True, slots=True)
class ResourcePool:
    """One administrative pool row; every field has an authoritative source."""

    pool_id: str
    node_ids: tuple[str, ...]
    service_class: ServiceClass
    max_active_slots: int
    realtime_reserved_slots: int
    active_slots: int
    quarantine_slots: int
    status: PoolStatus

    def __post_init__(self) -> None:
        if not isinstance(self.pool_id, str) or self.pool_id not in POOLS:
            raise ValueError("Invalid resource pool identity")
        if not isinstance(self.service_class, str) or self.service_class not in SERVICE_CLASSES:
            raise ValueError("Invalid resource pool service class")
        if not isinstance(self.status, str) or self.status not in STATUSES:
            raise ValueError("Invalid resource pool status")
        if not isinstance(self.node_ids, tuple) or len(set(self.node_ids)) != len(self.node_ids):
            raise ValueError("Resource pool nodes must be a unique ordered tuple")
        for node in self.node_ids:
            identifier(node)
        for value in (
            self.max_active_slots,
            self.realtime_reserved_slots,
            self.active_slots,
            self.quarantine_slots,
        ):
            counter(value)
        if self.realtime_reserved_slots > self.max_active_slots:
            raise ValueError("Realtime reserved slots exceed the pool capacity")
        if self.active_slots + self.quarantine_slots > self.max_active_slots:
            raise ValueError("Active and quarantined slots exceed the pool capacity")
        if self.status == "ready" and (not self.node_ids or self.max_active_slots < 1):
            raise ValueError("A ready pool requires a contributing node and positive capacity")


def pool_status(facts: ResourcePoolFacts) -> PoolStatus:
    """Map stored state and instance readiness onto the declared status enum."""
    if facts.pool_state != ACCEPTING_POOL_STATE or not facts.node_ids or facts.max_active_slots < 1:
        return "unavailable"
    if facts.ready_instances < 1:
        return "degraded"
    return "ready"


def project(facts: ResourcePoolFacts) -> ResourcePool:
    """Refuse to invent a row: every emitted field comes from the aggregate."""
    return ResourcePool(
        pool_id=facts.pool_id,
        node_ids=facts.node_ids,
        service_class=cast(ServiceClass, facts.service_class),
        max_active_slots=facts.max_active_slots,
        realtime_reserved_slots=facts.realtime_reserved_slots,
        active_slots=facts.active_slots,
        quarantine_slots=facts.quarantine_slots,
        status=pool_status(facts),
    )
