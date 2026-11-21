"""Bounded read-only aggregates of the coordination pool and instance tables.

The projection never reads admission commands, leases or submit intents: the
instance columns already carry the per-instance slot accounting that admission
maintains in the same transaction, so summing them cannot double count or
report quarantined capacity as free capacity. Instances that released their
budget are excluded, so a stopped instance cannot keep a node in the aggregate.
"""

from __future__ import annotations

import time
from typing import Any

import psycopg
from psycopg_pool import PoolClosed, PoolTimeout, TooManyRequests

from speechnexus_backend.speech.domain.resource_pools import (
    ResourcePoolFacts,
    ResourcePoolsUnavailableError,
)
from speechnexus_backend.speech.infrastructure.postgres_pool import SpeechPool

_QUERY = """
SELECT pool.pool_id, pool.pool_state, pool.service_class,
       COALESCE(aggregate.node_ids::text[], ARRAY[]::text[]) AS node_ids,
       COALESCE(aggregate.ready_instances, 0) AS ready_instances,
       COALESCE(aggregate.max_active_slots, 0)::bigint AS max_active_slots,
       COALESCE(aggregate.realtime_reserved_slots, 0)::bigint AS realtime_reserved_slots,
       COALESCE(aggregate.active_slots, 0)::bigint AS active_slots,
       COALESCE(aggregate.quarantine_slots, 0)::bigint AS quarantine_slots
FROM resource_pools AS pool
LEFT JOIN (
    SELECT pool_id,
           array_agg(DISTINCT host_node_id ORDER BY host_node_id) AS node_ids,
           count(*) FILTER (WHERE instance_state = 'ready') AS ready_instances,
           sum(max_slots)::bigint AS max_active_slots,
           sum(realtime_reserved_slots)::bigint AS realtime_reserved_slots,
           sum(held_slots)::bigint AS active_slots,
           sum(quarantined_slots)::bigint AS quarantine_slots
    FROM engine_instances
    WHERE instance_state <> 'stopped'
    GROUP BY pool_id
) AS aggregate ON aggregate.pool_id = pool.pool_id
ORDER BY pool.pool_id
"""


class PostgresResourcePoolReads:
    def __init__(self, pool: SpeechPool, *, timeout_seconds: float = 3) -> None:
        if isinstance(timeout_seconds, bool) or not 0 < timeout_seconds <= 30:
            raise ValueError("Resource pool reads require bounded deadlines")
        self._pool, self._timeout = pool, timeout_seconds

    def scan(self) -> tuple[ResourcePoolFacts, ...]:
        deadline = time.monotonic() + self._timeout
        try:
            with self._pool.connection(timeout=self._timeout) as connection:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ResourcePoolsUnavailableError
                connection.execute("SET TRANSACTION READ ONLY")
                connection.execute(
                    "SELECT set_config('statement_timeout', %s, true)",
                    (str(max(1, int(remaining * 1000))),),
                )
                records = connection.execute(_QUERY).fetchall()
                result = tuple(_facts(row) for row in records)
                if time.monotonic() >= deadline:
                    raise ResourcePoolsUnavailableError
            return result
        except (
            psycopg.Error,
            PoolClosed,
            PoolTimeout,
            TooManyRequests,
            ValueError,
            TypeError,
            KeyError,
        ):
            raise ResourcePoolsUnavailableError from None


def _facts(row: dict[str, Any]) -> ResourcePoolFacts:
    return ResourcePoolFacts(
        pool_id=row["pool_id"],
        pool_state=row["pool_state"],
        service_class=row["service_class"],
        node_ids=tuple(row["node_ids"]),
        ready_instances=row["ready_instances"],
        max_active_slots=row["max_active_slots"],
        realtime_reserved_slots=row["realtime_reserved_slots"],
        active_slots=row["active_slots"],
        quarantine_slots=row["quarantine_slots"],
    )
