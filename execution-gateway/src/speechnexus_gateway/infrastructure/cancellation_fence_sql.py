"""Read the backend-owned cancellation fence inside the submit transaction.

The fence row is keyed by the execution operation identity. For a public Job the
backend uses the Job identity as that operation identity, so the gateway can find
the durable acceptance fact without a second database or a new request field.

Taking the canonical Job row lock (``coordination_jobs``, ``FOR UPDATE``) before
reading the fence orders concatenated submit and acceptance transactions: the
acceptance writer holds that same row while it writes the fence, so either the
submit commits before acceptance, or the submit waits and then observes the
committed fence and refuses to create an execution. The lock is the portable
replacement for the transaction-scoped advisory key this module used to take; the
gateway holds only the row-lock allowance (``UPDATE (claim_version)``), never a
write.
"""

from __future__ import annotations

from speechnexus_contracts.cancellation_fence import JOB_CANCELLATION_FENCE_TABLE

from speechnexus_gateway.infrastructure.submit_sql import SubmitCursor


def cancellation_fence_present(cursor: SubmitCursor, operation_id: str) -> bool:
    """Lock the Job row and read the fence; a missing row means no accepted cancellation."""
    cursor.execute(
        "SELECT job_id FROM coordination_jobs WHERE job_id = ? FOR UPDATE", (operation_id,)
    )
    cursor.execute(
        f"SELECT job_id FROM {JOB_CANCELLATION_FENCE_TABLE} WHERE job_id = ?",
        (operation_id,),
    )
    return bool(cursor.fetchall())


__all__ = ["cancellation_fence_present"]
