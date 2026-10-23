"""Explicit Job/outbox statements shared by the PG adapter and behavior oracle.

No connection ownership or commit here: future complete-recipe admission must
call create_in_transaction inside its existing transaction, never a second port.
"""

from __future__ import annotations

from typing import Any, Protocol

from speechnexus_backend.speech.domain.jobs import (
    CreateJob,
    JobFact,
    JobResult,
    replay_result,
    unknown_result,
)


class JobCursor(Protocol):
    def execute(self, query: str, parameters: tuple[object, ...] = ()) -> object: ...

    def fetchall(self) -> list[dict[str, Any]]: ...


def _reconciliation(request: CreateJob) -> JobResult:
    return JobResult(
        "rejected", request.job_id, error_code="reconciliation_required", recovery="reconcile"
    )


def _existing(cursor: JobCursor, request: CreateJob) -> JobResult | None:
    cursor.execute(
        """SELECT job_id, operation_id, attempt_id,
                  initial_event_id AS event_id, initial_event_digest AS event_digest, request_digest
           FROM coordination_jobs
           WHERE job_id = %s OR (operation_id = %s AND attempt_id = %s)
           ORDER BY job_id""",
        (request.job_id, request.operation_id, request.attempt_id),
    )
    rows = cursor.fetchall()
    if not rows:
        return None
    if len(rows) != 1:
        return JobResult("conflict", request.job_id, error_code="idempotency_conflict")
    try:
        original = CreateJob(**rows[0])
    except ValueError, TypeError:
        return _reconciliation(request)
    replay = replay_result(request, original)
    if replay.outcome == "conflict":
        return replay
    cursor.execute(
        "SELECT job_id, event_digest FROM coordination_outbox WHERE event_id = %s",
        (original.event_id,),
    )
    events = cursor.fetchall()
    if events != [{"job_id": original.job_id, "event_digest": original.event_digest}]:
        return _reconciliation(request)
    return replay


def create_in_transaction(cursor: JobCursor, request: CreateJob) -> JobResult:
    # These identity/digest columns are immutable. Unique indexes serialize new
    # creations; no broad UPDATE grant or mutable-claim lock is needed to replay.
    existing = _existing(cursor, request)
    if existing is not None:
        return existing
    cursor.execute(
        """INSERT INTO coordination_jobs
           (job_id, operation_id, attempt_id, initial_event_id,
            initial_event_digest, request_digest)
           VALUES (%s, %s, %s, %s, %s, %s)""",
        (
            request.job_id,
            request.operation_id,
            request.attempt_id,
            request.event_id,
            request.event_digest,
            request.request_digest,
        ),
    )
    cursor.execute(
        "INSERT INTO coordination_outbox (event_id, job_id, event_digest) VALUES (%s, %s, %s)",
        (request.event_id, request.job_id, request.event_digest),
    )
    # The enclosing adapter may return this only after commit confirmation.
    return JobResult("committed", request.job_id, JobFact(request.job_id, request.request_digest))


def recover_in_transaction(
    cursor: JobCursor, request: CreateJob, *, conflict_if_absent: bool = False
) -> JobResult:
    """A missing visible row cannot distinguish rollback from an in-flight commit."""
    existing = _existing(cursor, request)
    if existing is not None:
        return existing
    if conflict_if_absent:
        # Only after the DB has confirmed rollback for an insertion-key conflict.
        return JobResult("conflict", request.job_id, error_code="idempotency_conflict")
    return unknown_result(request)
