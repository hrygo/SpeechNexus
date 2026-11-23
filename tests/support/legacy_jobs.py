"""Seed a Job directly for migration tests that stop below 0064.

``0064_canonical_jobs_columns`` adds ``bucket_no`` and the production writer now
always supplies it (``bucket_for(job_id)``). That writer is only supported
against schema >= 0064: teaching it to detect the column and pick an INSERT shape
would keep two supported schemas alive in production, so the deployment contract
is "migrations first, then the build".

A migration test that migrates a schema only to an older revision therefore
cannot seed through the writer. Its subject is the migration (the downgrade
guard, the preserved history, the ACL), not the writer, so it seeds the rows the
revision can actually hold with the pre-canonical INSERT shape itself: the six
``coordination_jobs`` columns that existed at 0001 and the outbox row the writer
always wrote. The columns that 0064/0065 add are backfilled by those migrations
from these same rows, which is what keeps the upgrade assertions meaningful.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from speechnexus_backend.speech.domain.jobs import CreateJob

Execute = Callable[[str, tuple[object, ...]], Any]

#: The writer's INSERT before 0064: the six columns ``0001_jobs`` created.
_LEGACY_JOB_INSERT = (
    "INSERT INTO coordination_jobs (job_id, operation_id, attempt_id, initial_event_id, "
    "initial_event_digest, request_digest) VALUES (%s, %s, %s, %s, %s, %s)"
)
_LEGACY_OUTBOX_INSERT = (
    "INSERT INTO coordination_outbox (event_id, job_id, event_digest) VALUES (%s, %s, %s)"
)


def seed_legacy_job(execute: Execute, request: CreateJob) -> None:
    """Insert the Job and outbox rows a pre-0064 writer would have inserted.

    ``execute`` takes one statement plus parameters, matching
    ``JobDatabase.query`` and ``psycopg.Cursor.execute``. No idempotency branch
    is reproduced: the migration tests seed once into an empty schema.
    """
    execute(
        _LEGACY_JOB_INSERT,
        (
            request.job_id,
            request.operation_id,
            request.attempt_id,
            request.event_id,
            request.event_digest,
            request.request_digest,
        ),
    )
    execute(_LEGACY_OUTBOX_INSERT, (request.event_id, request.job_id, request.event_digest))
