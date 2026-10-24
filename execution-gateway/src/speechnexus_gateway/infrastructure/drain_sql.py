"""Plain reads for one instance generation's recorded identity and in-flight work.

Both statements only read: a drain reports what is still unfinished and never releases
capacity, and neither statement locks a row the controlled writers take in another order.
"""

from __future__ import annotations

from typing import Any

from speechnexus_gateway.domain.instance_drain import DrainInventory, InstanceIdentity
from speechnexus_gateway.infrastructure.submit_sql import SubmitCursor

#: The states that still hold capacity, in the contract's three drain buckets.
_UNFINISHED_STATES = ("submitting", "executing", "stopping", "unknown_quarantined")

_SELECT_IDENTITY = (
    "SELECT instance_id, instance_generation, host_node_id, pool_id, gateway_id "
    "FROM engine_instances WHERE instance_id = %s AND instance_generation = %s"
)

_SELECT_INVENTORY = (
    "SELECT s.submit_intent_id, s.execution_state FROM submit_intents s "
    "JOIN admission_leases l ON l.lease_id = s.lease_id "
    "WHERE l.instance_id = %s AND l.instance_generation = %s "
    "AND l.host_node_id = %s AND l.pool_id = %s AND l.gateway_id = %s "
    "AND s.execution_state IN ('submitting', 'executing', 'stopping', "
    "'unknown_quarantined') "
    "ORDER BY s.submit_intent_id"
)


def read_instance_identity(
    cursor: SubmitCursor, instance_id: str, instance_generation: int
) -> InstanceIdentity | None:
    """Read one recorded generation identity, or nothing when that generation is unknown."""
    cursor.execute(_SELECT_IDENTITY, (instance_id, instance_generation))
    rows = cursor.fetchall()
    if not rows:
        return None
    row = rows[0]
    return InstanceIdentity(
        instance_id=str(row["instance_id"]),
        instance_generation=int(row["instance_generation"]),
        host_node_id=str(row["host_node_id"]),
        pool_id=str(row["pool_id"]),
        gateway_id=str(row["gateway_id"]),
    )


def read_drain_inventory(cursor: SubmitCursor, identity: InstanceIdentity) -> DrainInventory:
    """Read the unfinished intents this gateway still owns for one instance generation."""
    cursor.execute(
        _SELECT_INVENTORY,
        (
            identity.instance_id,
            identity.instance_generation,
            identity.host_node_id,
            identity.pool_id,
            identity.gateway_id,
        ),
    )
    rows = cursor.fetchall()
    return drain_inventory_from_rows(rows)


def drain_inventory_from_rows(rows: list[dict[str, Any]]) -> DrainInventory:
    """Split stored intent states into the contract's three drain buckets."""
    states = {
        str(row["submit_intent_id"]): str(row["execution_state"])
        for row in rows
        if str(row["execution_state"]) in _UNFINISHED_STATES
    }
    return DrainInventory(
        submitting_intent_ids=tuple(
            key for key, state in sorted(states.items()) if state == "submitting"
        ),
        active_intent_ids=tuple(
            key for key, state in sorted(states.items()) if state in {"executing", "stopping"}
        ),
        quarantined_intent_ids=tuple(
            key for key, state in sorted(states.items()) if state == "unknown_quarantined"
        ),
    )
