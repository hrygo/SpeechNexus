"""Read-only pool listing; no budget, lease or owner mutation in this path."""

from __future__ import annotations

from typing import Protocol

from speechnexus_backend.speech.domain.resource_pools import (
    MAX_POOLS,
    ResourcePool,
    ResourcePoolFacts,
    ResourcePoolsUnavailableError,
    project,
)


class ResourcePoolReads(Protocol):
    def scan(self) -> tuple[ResourcePoolFacts, ...]:
        """Return every pool once, ordered by pool identity."""
        ...


class ResourcePoolService:
    """Aggregate facts stay in storage; the projection is validated on read."""

    def __init__(self, reads: ResourcePoolReads) -> None:
        self._reads = reads

    def list(self) -> tuple[ResourcePool, ...]:
        facts = self._reads.scan()
        if len(facts) > MAX_POOLS:
            # A pool identity is a closed domain: more rows means the read did
            # not come from the reviewed coordination table shape.
            raise ResourcePoolsUnavailableError
        pools: list[ResourcePool] = []
        previous = ""
        for item in facts:
            if not isinstance(item, ResourcePoolFacts) or item.pool_id <= previous:
                raise ResourcePoolsUnavailableError
            previous = item.pool_id
            try:
                pools.append(project(item))
            except ValueError:
                raise ResourcePoolsUnavailableError from None
        return tuple(pools)
