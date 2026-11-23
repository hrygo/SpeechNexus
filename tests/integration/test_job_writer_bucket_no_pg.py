"""Both production Job writers supply ``bucket_for(job_id)`` at INSERT time (#1176).

``coordination_jobs.bucket_no`` is a canonical column (0064) that the live writers used to
leave empty: the admission transaction in ``jobs_sql.create_in_transaction`` and the
admission-less fallback in ``public_jobs_postgres._insert_job_rows`` inserted the six draft
columns only. A value that only the migration backfill supplies goes stale on the next Job,
so the write switch is the premise of every later reader and of the ``NOT NULL`` tightening.

The two writer call sites are proven separately, each under the identity its deployment
provisions:

* the fallback: the real admission-less HTTP assembly from ``test_public_jobs_http``
  (``jobs_db`` — the harness the older Public Job suite uses), so the coordination row
  comes from ``_insert_job_rows(admitted=False)``;
* the admission call site: ``jobs_sql.create_in_transaction`` — the function
  ``admission_sql.admit_in_transaction`` invokes inside its transaction — run on a pool
  authenticated as a role granted exactly ``public_job_admission_grants``, so the stored
  value and that role's column grant are proved together.

The full HTTP admission transaction is not repeated here: its fixture
(``tests.integration.test_production_admission.admission_database``) requires both the
PostgreSQL and MySQL DSNs at setup, and only PostgreSQL is available in this environment.
That benchmark still runs in CI and covers the wrapper; this module pins the INSERT surface
both wrappers share.

The stored value is compared with the single Python definition ``bucket_for(job_id)``, not
with a bucket recomputed by SQL or copied as a constant. Not covered here: a reader of the
column (none exists yet) and the ``NOT NULL`` tightening (a later revision).
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from psycopg.conninfo import make_conninfo
from tests.integration.test_public_jobs_http import (
    PASSWORD,
    PREFIX,
    JobSandbox,
    _app,
    _body,
)
from tests.integration.test_public_jobs_http import jobs_db as jobs_db

from speechnexus_backend.speech.domain.jobs import CreateJob
from speechnexus_backend.speech.infrastructure.jobs_postgres import PostgresJobTransactions
from speechnexus_backend.speech.infrastructure.postgres_pool import speech_pool
from speechnexus_backend.speech.infrastructure.public_job_admission_grants import (
    public_job_admission_grants,
)
from speechnexus_portable_sql.claims import bucket_for

pytestmark = pytest.mark.integration

DIGEST = "sha256:" + "a" * 64


def _stored_bucket(db: JobSandbox, job_id: str) -> int:
    """The bucket on the coordination row the writer just committed, or a failed premise."""

    rows = db.query("SELECT bucket_no FROM coordination_jobs WHERE job_id = %s", (job_id,))
    assert len(rows) == 1, f"coordination_jobs has no row for {job_id}"
    value = rows[0]["bucket_no"]
    assert value is not None, f"{job_id} was committed without bucket_no"
    return int(value)


async def test_the_fallback_writer_stores_bucket_for_the_job(jobs_db: JobSandbox) -> None:
    app = _app(jobs_db)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        created = await client.post(
            PREFIX, json=_body(), headers={"Idempotency-Key": "synthetic-idem-bucket-fallback"}
        )
    assert created.status_code == 202, created.text
    job_id = created.json()["id"]
    assert jobs_db.count("coordination_jobs") == 1
    assert _stored_bucket(jobs_db, job_id) == int(bucket_for(job_id))


def test_the_admission_writer_call_site_stores_bucket_for_the_job(
    jobs_db: JobSandbox,
) -> None:
    suffix = uuid.uuid4().hex[:12]
    role = "sn_jobs_adm_" + suffix
    pool: Any = None
    jobs_db.admin.execute(f"CREATE ROLE \"{role}\" LOGIN PASSWORD '{PASSWORD}'")
    try:
        for grant in public_job_admission_grants(jobs_db.schema, role):
            jobs_db.admin.execute(grant)
        pool = speech_pool(
            make_conninfo(jobs_db.url, user=role, password=PASSWORD),
            schema=jobs_db.schema,
            max_size=2,
            name="bucket-admission-" + suffix,
        )
        pool.open()
        request = CreateJob(
            "job-bucket-admission",
            "operation-bucket-admission",
            "attempt-bucket-admission",
            "event-bucket-admission",
            DIGEST,
            DIGEST,
        )
        result = PostgresJobTransactions(pool, timeout_seconds=3).create_job(request)
        assert result.outcome == "committed", result
    finally:
        if pool is not None:
            pool.close()
        jobs_db.admin.execute(f'DROP OWNED BY "{role}"')
        jobs_db.admin.execute(f'DROP ROLE IF EXISTS "{role}"')
    assert _stored_bucket(jobs_db, "job-bucket-admission") == int(
        bucket_for("job-bucket-admission")
    )
