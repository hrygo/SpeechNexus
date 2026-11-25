"""Seed a Job directly for migration tests that stop below 0064, and its public-only twin.

Two different "legacy" shapes live here, and they are not the same thing:

``seed_legacy_job`` is the **pre-0064 coordination shape**: a ``coordination_jobs`` row
with the six columns ``0001_jobs`` created, plus its outbox row. It *has* a canonical
parent; what it lacks are the columns ``0064``/``0065`` add (``bucket_no``, ``due_at``,
``claim_expires_at``).

``seed_public_only_job`` is the shape with **no** ``coordination_jobs`` row at all: only
the public rows the Job API projects. That is the shape whose canonical event write has
no foreign-key parent, so every test about "what does the dispatcher do without a
coordination identity" has to start from it rather than from ``seed_legacy_job``.

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

#: The public half only: the columns the Job API writer fills, and the creation event it
#: always appends at ``event_seq`` 0. No ``coordination_jobs``/``coordination_outbox`` row.
_PUBLIC_ONLY_JOB_INSERT = (
    "INSERT INTO speech_jobs (job_id, workspace_id, kind, model_id, request_digest, "
    "idempotency_key_digest, state, status_version, created_at_us, updated_at_us) "
    "VALUES (%s, %s, %s, %s, %s, %s, 'pending', 1, %s, %s)"
)
_PUBLIC_ONLY_CREATION_EVENT = (
    "INSERT INTO speech_job_events (job_id, event_seq, state, occurred_at_us) "
    "VALUES (%s, 0, 'pending', %s)"
)


def seed_public_only_job(
    execute: Execute,
    *,
    job_id: str,
    workspace_id: str,
    kind: str,
    model_id: str,
    request_digest: str,
    created_at_us: int,
) -> None:
    """Insert a pending Job with public rows only — no coordination identity.

    This is the shape a canonical event write cannot reference: ``job_events.job_id``
    points at ``coordination_jobs(job_id)``, so a started fact for this Job has no
    parent to hang from. It is seeded, never written by product code — both creation
    writers (``public_jobs_postgres._insert_job_rows`` and
    ``jobs_sql.create_in_transaction``) insert the coordination row in the same
    transaction, which is exactly what makes this shape a *test* input.
    """
    execute(
        _PUBLIC_ONLY_JOB_INSERT,
        (
            job_id,
            workspace_id,
            kind,
            model_id,
            request_digest,
            request_digest,
            created_at_us,
            created_at_us,
        ),
    )
    execute(_PUBLIC_ONLY_CREATION_EVENT, (job_id, created_at_us))


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
