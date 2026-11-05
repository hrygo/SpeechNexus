"""Read the backend-owned cancellation fence inside the submit transaction.

The fence row is keyed by the execution operation identity. For a public Job the
backend uses the Job identity as that operation identity, so the gateway can
find the durable acceptance fact without a second database or a new request
field. Taking the same transaction-scoped advisory lock first orders concatenated submit and
acceptance transactions: either the submit commits before acceptance, or the
submit observes the committed fence and refuses to create an execution.
"""

from __future__ import annotations

from speechnexus_contracts.cancellation_fence import (
    JOB_CANCELLATION_FENCE_TABLE,
    job_cancellation_fence_lock_key,
)

from speechnexus_gateway.infrastructure.submit_sql import SubmitCursor


def cancellation_fence_present(cursor: SubmitCursor, operation_id: str) -> bool:
    """Lock and read the fence; a missing row means no accepted cancellation."""
    cursor.execute(
        "SELECT pg_advisory_xact_lock(%s)", (job_cancellation_fence_lock_key(operation_id),)
    )
    cursor.execute(
        f"SELECT job_id FROM {JOB_CANCELLATION_FENCE_TABLE} WHERE job_id = %s",
        (operation_id,),
    )
    return bool(cursor.fetchall())


__all__ = ["cancellation_fence_present"]
