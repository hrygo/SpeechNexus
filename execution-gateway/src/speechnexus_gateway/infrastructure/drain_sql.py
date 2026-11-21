"""Plain reads for one instance generation's identity, in-flight work and idle inputs.

Every statement only reads: a drain reports what is still unfinished and never releases
capacity, and neither statement locks a row the controlled writers take in another order.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from speechnexus_gateway.domain.instance_drain import DrainInventory, InstanceIdentity
from speechnexus_gateway.domain.instance_reclaim import ReclaimRecord
from speechnexus_gateway.infrastructure.submit_sql import SubmitCursor, from_unix_micros

#: The states that still hold capacity, in the contract's three drain buckets.
_UNFINISHED_STATES = ("submitting", "executing", "stopping", "unknown_quarantined")

_SELECT_IDENTITY = (
    "SELECT instance_id, instance_generation, host_node_id, pool_id, gateway_id "
    "FROM engine_instances WHERE instance_id = ? AND instance_generation = ?"
)

_SELECT_INVENTORY = (
    "SELECT s.submit_intent_id, s.execution_state FROM submit_intents s "
    "JOIN admission_leases l ON l.lease_id = s.lease_id "
    "WHERE l.instance_id = ? AND l.instance_generation = ? "
    "AND l.host_node_id = ? AND l.pool_id = ? AND l.gateway_id = ? "
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


#: One generation's reclaim inputs: identity columns, its leases and their intents.
#: The observation instant is database time, so an idle span never depends on a host clock.
_SELECT_RECLAIM = (
    "SELECT i.loading_policy, i.binding_revision_id, i.last_fencing_token, i.ready_at_us, "
    "s.submit_intent_id, s.execution_state, s.terminated_at_us, s.dispatched_at_us, "
    "s.updated_at_us, l.lease_id, l.lease_state, "
    "CAST((EXTRACT(EPOCH FROM clock_timestamp()) * 1000000) AS bigint) AS observed_at_us "
    "FROM engine_instances i "
    "LEFT JOIN admission_leases l ON l.instance_id = i.instance_id "
    "AND l.instance_generation = i.instance_generation "
    "AND l.host_node_id = i.host_node_id AND l.pool_id = i.pool_id "
    "AND l.gateway_id = i.gateway_id "
    "LEFT JOIN submit_intents s ON s.lease_id = l.lease_id "
    "WHERE i.instance_id = ? AND i.instance_generation = ? AND i.host_node_id = ? "
    "AND i.pool_id = ? AND i.gateway_id = ? "
    "ORDER BY s.submit_intent_id"
)

#: A lease in this state still holds the slot its execution was admitted against.
_RESERVED = "reserved"


def read_reclaim_record(cursor: SubmitCursor, identity: InstanceIdentity) -> ReclaimRecord | None:
    """Read one generation's reclaim inputs, or nothing when it was never recorded here."""
    cursor.execute(
        _SELECT_RECLAIM,
        (
            identity.instance_id,
            identity.instance_generation,
            identity.host_node_id,
            identity.pool_id,
            identity.gateway_id,
        ),
    )
    rows = cursor.fetchall()
    if not rows:
        return None
    return reclaim_record_from_rows(rows)


def reclaim_record_from_rows(rows: list[dict[str, Any]]) -> ReclaimRecord:
    """Derive the idle anchor and held capacity from one generation's joined rows.

    The anchor is the later of the readiness instant and the newest recorded execution
    activity, so a generation that never served anything still reaches its cooldown from
    readiness. A generation with neither has no anchor, and the judgement table holds it
    instead of guessing an idle span.
    """
    first = rows[0]
    record = _observed_at(first)
    activity = [
        max(
            value
            for value in (
                _optional_int(row["terminated_at_us"]),
                _optional_int(row["dispatched_at_us"]),
                _optional_int(row["updated_at_us"]),
            )
            if value is not None
        )
        for row in rows
        if row["submit_intent_id"] is not None
    ]
    ready_at = _optional_int(first["ready_at_us"])
    candidates = [value for value in (*activity, ready_at) if value is not None]
    intents = [row for row in rows if row["submit_intent_id"] is not None]
    return ReclaimRecord(
        loading_policy=str(first["loading_policy"]),
        binding_revision_id=str(first["binding_revision_id"]),
        last_fencing_token=int(first["last_fencing_token"]),
        observed_at=record,
        inventory=drain_inventory_from_rows(intents),
        holds_capacity=any(str(row["lease_state"]) == _RESERVED for row in rows),
        idle_anchor=None if not candidates else from_unix_micros(max(candidates)),
    )


def _observed_at(row: dict[str, Any]) -> datetime:
    return from_unix_micros(int(row["observed_at_us"]))


def _optional_int(value: Any) -> int | None:
    return None if value is None else int(value)
