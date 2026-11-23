"""Both production Job writers store canonical ``due_at`` at INSERT time (#1176).

``coordination_jobs.due_at`` is frozen by ``migrations/canonical_source_map.json`` to
``speech_jobs.created_at_us``. Until this slice only the 0064/0065 backfill supplied it,
so every Job created afterwards had ``due_at IS NULL`` and the candidate predicate
``due_at <= now`` — the boundary ``claim_batch`` is built on — could never select it.

The two writers are proven separately, each through the path its deployment uses:

* the admission-less fallback: the real HTTP assembly (``jobs_db``), where
  ``public_jobs_postgres._insert_job_rows(admitted=False)`` writes both
  ``speech_jobs.created_at_us`` and the coordination row;
* the admitted path: ``jobs_sql.create_in_transaction`` is called inside
  ``admission_sql.admit_in_transaction`` with the value that the same transaction will
  write into ``speech_jobs.created_at_us``; the test drives the complete public create
  through ``PostgresPublicJobs`` so the equality is between two committed rows.

The stored value is compared with exact integer-microsecond arithmetic, not with the
product converter, so a writer that copies the wrong column (or rounds through seconds)
fails here. Each Job is then handed to ``claims.candidates`` with ``now`` at/after its
creation instant: prerequisite 2 exists because that page was empty before this change.

A row the pre-switch writer left behind — ``bucket_no`` supplied, ``due_at`` still NULL —
is asserted to stay NULL and to stay out of the candidate page. 0065 only backfills rows
that existed when it ran; a test that filled the column first would hide exactly the
window this slice closes.
"""

from __future__ import annotations

import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import cast

import pytest
from httpx import ASGITransport, AsyncClient
from tests.integration.test_claim_batch_pg import _contract_rows
from tests.integration.test_coordination_foundation import foundation as foundation
from tests.integration.test_production_admission import AdmissionDatabase
from tests.integration.test_production_admission import admission_database as admission_database
from tests.integration.test_production_jobs import database as database
from tests.integration.test_production_runtime_intents import (
    runtime_database as runtime_database,
)
from tests.integration.test_public_job_atomic_admission_pg import _job, _seed, _store
from tests.integration.test_public_jobs_http import (
    PREFIX,
    JobSandbox,
    _app,
    _body,
)
from tests.integration.test_public_jobs_http import jobs_db as jobs_db
from tests.support.portable_pg import connect as portable_pg_connect

from speechnexus_portable_sql.claims import ClaimCandidate, bucket_for, candidates
from speechnexus_portable_sql.contract import ExecutableConnection

pytestmark = pytest.mark.integration
DIGEST = "sha256:" + "a" * 64


def _epoch_us_to_naive_utc(value: int) -> datetime:
    """The canonical conversion, spelled independently of the product helper."""

    return datetime(1970, 1, 1) + timedelta(microseconds=value)


def _candidate_page(
    url: str,
    schema: str,
    *,
    job_id: str,
    now: datetime,
    require_admission: bool = False,
) -> list[ClaimCandidate]:
    connection = cast(ExecutableConnection, portable_pg_connect(url, row_factory=_contract_rows))
    try:
        connection.execute(f'SET search_path TO "{schema}", pg_catalog')
        connection.execute("SET TimeZone TO 'UTC'")
        connection.commit()
        page, has_more = candidates(
            connection,
            bucket_no=Decimal(bucket_for(job_id)),
            now=now,
            require_admission=require_admission,
        )
        assert not has_more
        return page
    finally:
        connection.close()


def _stored_timestamps(db: JobSandbox, job_id: str) -> tuple[int, datetime | None]:
    rows = db.query(
        "SELECT s.created_at_us, c.due_at FROM speech_jobs AS s "
        "JOIN coordination_jobs AS c ON c.job_id = s.job_id WHERE s.job_id = %s",
        (job_id,),
    )
    assert len(rows) == 1, f"no speech_jobs/coordination_jobs pair for {job_id}"
    due_at = rows[0]["due_at"]
    return int(rows[0]["created_at_us"]), due_at


async def test_the_fallback_writer_stores_due_at_from_speech_jobs_created_at_us(
    jobs_db: JobSandbox,
) -> None:
    app = _app(jobs_db)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        created = await client.post(
            PREFIX, json=_body(), headers={"Idempotency-Key": "synthetic-idem-due-at-fallback"}
        )
    assert created.status_code == 202, created.text
    job_id = created.json()["id"]
    created_at_us, due_at = _stored_timestamps(jobs_db, job_id)
    assert due_at is not None, (
        "the admission-less fallback writer left due_at NULL; the Job it just created "
        "can never satisfy the canonical candidate predicate due_at <= now"
    )
    assert due_at.tzinfo is None
    assert due_at == _epoch_us_to_naive_utc(created_at_us), (
        f"fallback writer: due_at={due_at!r} is not speech_jobs.created_at_us="
        f"{created_at_us} converted to naive UTC"
    )

    page = _candidate_page(jobs_db.url, jobs_db.schema, job_id=job_id, now=datetime.now(UTC))
    assert [candidate.job_id for candidate in page] == [job_id], (
        "the Job the fallback writer just committed is not in claims.candidates(...)"
    )


@pytest.mark.parametrize("database", ["postgresql"], indirect=True)
def test_the_admission_writer_stores_due_at_from_speech_jobs_created_at_us(
    admission_database: AdmissionDatabase,
) -> None:
    _seed(admission_database)
    created_at_us = time.time_ns() // 1_000
    job = _job(kind="tts", key="synthetic-idem-due-at-admission")
    job = replace(job, submission=replace(job.submission, submitted_at_us=created_at_us))

    result = _store(admission_database).create(job)
    assert result.outcome == "created" and result.job is not None, result
    db = admission_database.runtime.foundation.database
    rows = db.query(
        "SELECT s.created_at_us, c.due_at FROM speech_jobs AS s "
        "JOIN coordination_jobs AS c ON c.job_id = s.job_id WHERE s.job_id = %s",
        (job.job_id,),
    )
    assert len(rows) == 1, f"no speech_jobs/coordination_jobs pair for {job.job_id}"
    assert int(rows[0]["created_at_us"]) == created_at_us
    due_at = rows[0]["due_at"]
    assert due_at is not None, (
        "the admission writer left due_at NULL; the Job it just admitted can never "
        "satisfy the canonical candidate predicate due_at <= now"
    )
    assert due_at.tzinfo is None
    assert due_at == _epoch_us_to_naive_utc(created_at_us), (
        f"admission writer: due_at={due_at!r} is not speech_jobs.created_at_us="
        f"{created_at_us} converted to naive UTC"
    )

    page = _candidate_page(
        db.url,
        db.schema,
        job_id=job.job_id,
        now=datetime.now(UTC),
        require_admission=True,
    )
    assert [candidate.job_id for candidate in page] == [job.job_id], (
        "the admitted Job is not in claims.candidates(..., require_admission=True)"
    )


def test_a_pre_change_row_keeps_null_due_at_and_stays_out_of_the_candidate_page(
    jobs_db: JobSandbox,
) -> None:
    """The window 0065 cannot cover: created after the migration, before this writer."""

    job_id = "job-due-at-window"
    jobs_db.admin.execute(
        "INSERT INTO speech_jobs "
        "(job_id, workspace_id, kind, model_id, request_digest, created_at_us, updated_at_us) "
        "VALUES (%s, 'workspace-due-at', 'tts', 'model-due-at', %s, 1, 1)",
        (job_id, DIGEST),
    )
    # The post-B1, pre-B2 writer shape: bucket supplied at INSERT, due_at omitted.
    jobs_db.admin.execute(
        "INSERT INTO coordination_jobs "
        "(job_id, operation_id, attempt_id, initial_event_id, initial_event_digest, "
        "request_digest, bucket_no) VALUES (%s, %s, %s, %s, %s, %s, %s)",
        (
            job_id,
            job_id,
            "attempt-due-at",
            f"{job_id}-queued",
            DIGEST,
            DIGEST,
            int(bucket_for(job_id)),
        ),
    )
    rows = jobs_db.query("SELECT due_at FROM coordination_jobs WHERE job_id = %s", (job_id,))
    assert len(rows) == 1
    assert rows[0]["due_at"] is None

    page = _candidate_page(jobs_db.url, jobs_db.schema, job_id=job_id, now=datetime.now(UTC))
    assert page == [], (
        "a row with NULL due_at became a claim candidate; neither this writer nor 0065 "
        "backfills rows written after the migration"
    )
    assert jobs_db.query("SELECT due_at FROM coordination_jobs WHERE job_id = %s", (job_id,)) == [
        {"due_at": None}
    ], "the candidate read silently backfilled due_at"
