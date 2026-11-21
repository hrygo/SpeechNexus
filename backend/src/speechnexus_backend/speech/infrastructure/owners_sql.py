"""Explicit owner statements; connection/commit belong to the enclosing adapter.

All controlled writers to a pool, its instances, barriers and submits must take
the pool row first. Activation additionally locks current rows in global order.
No physical fencing, network calls or budget release happen in this helper.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Protocol

from speechnexus_backend.speech.domain.owners import (
    POOLS,
    AcquireOwner,
    ActivateOwner,
    ActivationFacts,
    BarrierFact,
    OwnerRejectedError,
    OwnerResult,
    PoolOwnership,
    RenewOwner,
    acquired,
    activated,
    renewed,
    unknown,
)
from speechnexus_backend.speech.infrastructure.leases_sql import _record as lease_record


class OwnerCursor(Protocol):
    @property
    def rowcount(self) -> int: ...

    def execute(self, query: str, parameters: tuple[object, ...] = ()) -> object: ...

    def fetchall(self) -> list[dict[str, Any]]: ...


def read_ownership(cursor: OwnerCursor, pool_id: str, *, lock: bool) -> PoolOwnership | None:
    if pool_id not in POOLS:
        raise ValueError("Invalid owner pool")
    cursor.execute(
        "SELECT pool_id, owner_id, fencing_token, owner_expires_at_us AS expires_at_us, "
        "owner_state, version FROM resource_pools WHERE pool_id = %s"
        + (" FOR UPDATE" if lock else ""),
        (pool_id,),
    )
    rows = cursor.fetchall()
    if not rows:
        return None
    if len(rows) != 1:
        raise OwnerRejectedError("reconciliation_required")
    try:
        return PoolOwnership(**rows[0])
    except ValueError, TypeError:
        raise OwnerRejectedError("reconciliation_required") from None


def query_in_transaction(cursor: OwnerCursor, pool_id: str) -> OwnerResult:
    fact = read_ownership(cursor, pool_id, lock=False)
    if fact is None or fact.owner_id is None:
        return unknown(pool_id)
    return OwnerResult("replayed", pool_id, fact)


def activation_facts(cursor: OwnerCursor, pool_id: str) -> ActivationFacts:
    cursor.execute(
        "SELECT gateway_id FROM engine_instances WHERE pool_id = %s "
        "AND instance_state <> 'stopped' ORDER BY instance_id, instance_generation FOR UPDATE",
        (pool_id,),
    )
    gateways = frozenset(row["gateway_id"] for row in cursor.fetchall())
    cursor.execute(
        "SELECT * FROM admission_leases WHERE pool_id = %s "
        "AND lease_state <> 'released' ORDER BY lease_id FOR UPDATE",
        (pool_id,),
    )
    try:
        accounted = {
            lease.lease_id
            for row in cursor.fetchall()
            if (lease := lease_record(row)).lease_state == "quarantined"
        }
    except ValueError, TypeError, KeyError:
        raise OwnerRejectedError("reconciliation_required") from None
    cursor.execute(
        "SELECT gateway_id, min_fencing_token, sealed_evidence_digest AS evidence_digest "
        "FROM gateway_fencing_barriers WHERE pool_id = %s ORDER BY gateway_id FOR UPDATE",
        (pool_id,),
    )
    try:
        barriers = tuple(BarrierFact(**row) for row in cursor.fetchall())
    except ValueError, TypeError:
        raise OwnerRejectedError("reconciliation_required") from None
    cursor.execute(
        "SELECT submit_intent_id, lease_id, execution_state, cleanup_state "
        "FROM submit_intents WHERE lease_id IN "
        "(SELECT lease_id FROM admission_leases WHERE pool_id = %s) "
        "AND execution_state IN ('submitting', 'executing', 'stopping', 'unknown_quarantined') "
        "ORDER BY submit_intent_id FOR UPDATE",
        (pool_id,),
    )
    submissions = cursor.fetchall()
    # The A lease marker commits with all quarantine projections and threshold
    # handling. The B unknown marker alone cannot establish that completion.
    return ActivationFacts(
        gateways,
        barriers,
        any(row["execution_state"] != "unknown_quarantined" for row in submissions),
        any(
            row["execution_state"] == "unknown_quarantined"
            and (row["lease_id"] not in accounted or row["cleanup_state"] != "quarantined")
            for row in submissions
        ),
    )


def mutate_in_transaction(
    cursor: OwnerCursor,
    request: AcquireOwner | RenewOwner | ActivateOwner,
    *,
    clock: Callable[[], int],
) -> OwnerResult:
    pool_id = request.pool_id if isinstance(request, AcquireOwner) else request.authority.pool_id
    current = read_ownership(cursor, pool_id, lock=True)
    if current is None:
        raise OwnerRejectedError("not_found")
    if isinstance(request, AcquireOwner):
        updated = acquired(current, request, clock())
    elif isinstance(request, RenewOwner):
        updated = renewed(current, request, clock())
    else:
        evidence = activation_facts(cursor, pool_id)
        # Time is obtained after every lock wait, not before entering the txn.
        updated = activated(current, request, clock(), evidence)
    cursor.execute(
        "UPDATE resource_pools SET owner_id = %s, fencing_token = %s, owner_expires_at_us = %s, "
        "owner_state = %s, version = %s WHERE pool_id = %s AND version = %s",
        (
            updated.owner_id,
            updated.fencing_token,
            updated.expires_at_us,
            updated.owner_state,
            updated.version,
            pool_id,
            current.version,
        ),
    )
    if cursor.rowcount != 1:
        raise OwnerRejectedError("reconciliation_required")
    return OwnerResult("committed", pool_id, updated)
