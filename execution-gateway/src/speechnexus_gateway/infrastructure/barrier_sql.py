"""Persist an already enforced ingress minimum using ordinary rows and short transactions."""

from collections.abc import Callable
from datetime import datetime

from speechnexus_gateway.domain.fencing import (
    BarrierConflictError,
    BarrierSnapshot,
    IngressFence,
    verify_barrier_authority,
)
from speechnexus_gateway.domain.submission import (
    InvalidExecutionRequestError,
    StaleFencingTokenError,
    SubmissionStoreError,
)
from speechnexus_gateway.infrastructure.drain_sql import drain_inventory_from_rows
from speechnexus_gateway.infrastructure.submit_sql import SubmitCursor, to_unix_micros


def barrier_inventory(
    cursor: SubmitCursor, gateway_id: str, pool_id: str
) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    """Fresh inventory of this gateway's unfinished intents for one pool.

    Plain reads only: every controlled writer takes the pool row first, so joining a lease
    ``FOR UPDATE`` after the barrier row would reverse that order.
    """
    cursor.execute(
        "SELECT s.submit_intent_id, s.execution_state FROM submit_intents s "
        "JOIN admission_leases l ON l.lease_id = s.lease_id "
        "WHERE l.gateway_id = %s AND l.pool_id = %s "
        "AND s.execution_state IN ('submitting', 'executing', 'stopping', 'unknown_quarantined') "
        "ORDER BY s.submit_intent_id",
        (gateway_id, pool_id),
    )
    inventory = drain_inventory_from_rows(cursor.fetchall())
    return (
        inventory.submitting_intent_ids,
        inventory.active_intent_ids,
        inventory.quarantined_intent_ids,
    )


def read_barrier(
    cursor: SubmitCursor, gateway_id: str, pool_id: str, *, clock: Callable[[], datetime]
) -> BarrierSnapshot | None:
    """Read the recorded barrier fact without locking it or accepting a new minimum.

    Inventory and observation time come from this read: only the enforced minimum and its
    proof digest are the immutable fact, so a replay can return the original proof after the
    owner that sealed it was replaced.
    """
    cursor.execute(
        "SELECT min_fencing_token, sealed_evidence_digest, version FROM gateway_fencing_barriers "
        "WHERE gateway_id = %s AND pool_id = %s",
        (gateway_id, pool_id),
    )
    rows = cursor.fetchall()
    if not rows:
        return None
    submitting, executing, quarantined = barrier_inventory(cursor, gateway_id, pool_id)
    return BarrierSnapshot(
        gateway_id=gateway_id,
        pool_id=pool_id,
        min_fencing_token=rows[0]["min_fencing_token"],
        evidence_digest=rows[0]["sealed_evidence_digest"],
        version=int(rows[0]["version"]),
        observed_at_us=to_unix_micros(clock()),
        submitting_intent_ids=submitting,
        executing_intent_ids=executing,
        quarantined_intent_ids=quarantined,
    )


def seal_barrier_in_transaction(
    cursor: SubmitCursor,
    fence: IngressFence,
    *,
    clock: Callable[[], datetime],
) -> BarrierSnapshot:
    request = fence.request
    cursor.execute(
        "SELECT owner_id, fencing_token, owner_expires_at_us FROM resource_pools "
        "WHERE pool_id = %s FOR UPDATE",
        (request.pool_id,),
    )
    pools = cursor.fetchall()
    if len(pools) != 1:
        raise InvalidExecutionRequestError("barrier references an unknown pool")
    cursor.execute(
        "SELECT min_fencing_token, sealed_evidence_digest, version FROM gateway_fencing_barriers "
        "WHERE gateway_id = %s AND pool_id = %s FOR UPDATE",
        (fence.gateway_id, request.pool_id),
    )
    barriers = cursor.fetchall()
    now_us = to_unix_micros(clock())
    verify_barrier_authority(
        fence,
        owner_id=pools[0]["owner_id"],
        fencing_token=pools[0]["fencing_token"],
        expires_at_us=pools[0]["owner_expires_at_us"],
        now_us=now_us,
    )
    cursor.execute(
        "SELECT DISTINCT host_node_id FROM engine_instances WHERE gateway_id = %s "
        "AND pool_id = %s AND instance_state <> 'stopped'",
        (fence.gateway_id, request.pool_id),
    )
    if any(row["host_node_id"] != fence.host_node_id for row in cursor.fetchall()):
        raise InvalidExecutionRequestError("gateway engine ingress belongs to another node")
    digest = fence.evidence_digest
    if barriers:
        previous = barriers[0]
        if previous["min_fencing_token"] > request.min_accepted_fencing_token:
            raise StaleFencingTokenError("durable ingress minimum cannot decrease")
        version = int(previous["version"])
        if previous["min_fencing_token"] == request.min_accepted_fencing_token:
            if previous["sealed_evidence_digest"] != digest:
                raise BarrierConflictError("barrier minimum already has another proof")
        else:
            if version >= 2**63 - 1:
                raise SubmissionStoreError("barrier version is exhausted")
            cursor.execute(
                "UPDATE gateway_fencing_barriers SET min_fencing_token = %s, "
                "sealed_evidence_digest = %s, version = version + 1 "
                "WHERE gateway_id = %s AND pool_id = %s AND version = %s",
                (
                    request.min_accepted_fencing_token,
                    digest,
                    fence.gateway_id,
                    request.pool_id,
                    version,
                ),
            )
            if cursor.rowcount != 1:
                raise SubmissionStoreError("barrier changed while holding the pool lock")
            version += 1
    else:
        cursor.execute(
            "INSERT INTO gateway_fencing_barriers "
            "(gateway_id, pool_id, min_fencing_token, sealed_evidence_digest) "
            "VALUES (%s, %s, %s, %s)",
            (fence.gateway_id, request.pool_id, request.min_accepted_fencing_token, digest),
        )
        version = 1
    submitting, executing, quarantined = barrier_inventory(
        cursor, fence.gateway_id, request.pool_id
    )
    return BarrierSnapshot(
        gateway_id=fence.gateway_id,
        pool_id=request.pool_id,
        min_fencing_token=request.min_accepted_fencing_token,
        evidence_digest=digest,
        version=version,
        observed_at_us=now_us,
        submitting_intent_ids=submitting,
        executing_intent_ids=executing,
        quarantined_intent_ids=quarantined,
    )
