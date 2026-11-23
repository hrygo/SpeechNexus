"""0065 re-runs the 0064 backfill and derives ``last_event_seq`` from the draft events.

Three claims are worth a real database, and none of them can be proven by reading the
migration:

* ``last_event_seq`` is the per-Job **MAX** of ``speech_job_events.event_seq``, not a count
  and not the placeholder ``0`` the 0064 column default left behind. The draft allocates
  ``COALESCE(MAX(event_seq), -1) + 1``, so a Job whose events are ``0, 1, 2`` has head
  ``2`` — a ``COUNT`` implementation would pass a naive ``3`` expectation.
* A row written **after** 0064 ran (draft columns only, canonical columns NULL) is filled
  by 0065. That window is the entire reason this revision exists.
* ``claim_expires_at`` / ``due_at`` are naive ``TIMESTAMP(6)`` holding UTC wall clock even
  though the migration session is ``Asia/Shanghai``. Drop ``AT TIME ZONE 'UTC'`` from the
  backfill and these reads shift by eight hours with no error anywhere.

``bucket_no`` is deliberately **still nullable** after 0065, and that is a finding rather
than an omission. The live Job writer inserts only
``COORDINATION_JOB_INSERT_COLUMNS`` — the six draft columns — so a NOT NULL constraint on
``bucket_no`` makes every Job creation fail with ``23502``, surfaced as ``503
job_unavailable``. The tightening has to land with the write path that supplies
``bucket_for(job_id)`` (#1102's write switch), and the guard below fails as soon as that
write path does supply the column, so the deferral cannot silently outlive its premise.
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.pool import NullPool
from tests.support.portable_pg import connect as portable_pg_connect

from speechnexus_backend.speech.infrastructure.job_writer_columns import (
    COORDINATION_JOB_INSERT_COLUMNS,
)
from speechnexus_portable_sql.claims import bucket_for

pytestmark = pytest.mark.integration
ROOT = Path(__file__).resolve().parents[2]
VARIABLE = "SPEECHNEXUS_TEST_DATABASE_URL"
DIGEST = "sha256:" + "a" * 64
MOMENT = datetime(2026, 11, 23, 12, 0, 0, tzinfo=UTC)
MOMENT_US = int(MOMENT.timestamp()) * 1_000_000
NAIVE_MOMENT = datetime(2026, 11, 23, 12, 0, 0)
PRE_BACKFILL = "0063_deletion_targets_blocked"
PREVIOUS = "0064_canonical_jobs_columns"
REVISION = "0065_canonical_jobs_reconcile"


def _config(schema: str) -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.attributes["schema"] = schema
    return config


def _insert_draft_job(
    admin: psycopg.Connection[Any],
    *,
    job_id: str,
    claim_expires_at_us: int,
    speech_created_at_us: int | None,
) -> None:
    """Insert a row the way the pre-switch runtime still writes it: draft columns only."""

    admin.execute(
        "INSERT INTO coordination_jobs "
        "(job_id, operation_id, attempt_id, initial_event_id, initial_event_digest, "
        "request_digest, claim_expires_at_us) VALUES (%s, %s, %s, %s, %s, %s, %s)",
        (
            job_id,
            f"op-{job_id}",
            f"attempt-{job_id}",
            f"{job_id}-queued",
            DIGEST,
            DIGEST,
            claim_expires_at_us,
        ),
    )
    if speech_created_at_us is not None:
        admin.execute(
            "INSERT INTO speech_jobs "
            "(job_id, workspace_id, kind, model_id, request_digest, created_at_us, updated_at_us) "
            "VALUES (%s, 'workspace-0065', 'tts', 'model-0065', %s, %s, %s)",
            (job_id, DIGEST, speech_created_at_us, speech_created_at_us),
        )


def _insert_event(admin: psycopg.Connection[Any], *, job_id: str, event_seq: int) -> None:
    """One draft lifecycle event; ``state`` and ``occurred_at_us`` are all the draft stores."""

    admin.execute(
        "INSERT INTO speech_job_events (job_id, event_seq, state, occurred_at_us) "
        "VALUES (%s, %s, 'pending', %s)",
        (job_id, event_seq, MOMENT_US),
    )


def _insert_like_the_live_writer(admin: psycopg.Connection[Any], *, job_id: str) -> None:
    """Insert with exactly the columns the production writer is granted and uses.

    ``COORDINATION_JOB_INSERT_COLUMNS`` is the single declaration the grants, the startup
    verifier and both INSERT statements follow, so this is the real producer surface rather
    than a hand-copied column list.
    """

    values = {
        "job_id": job_id,
        "operation_id": f"op-{job_id}",
        "attempt_id": f"attempt-{job_id}",
        "initial_event_id": f"{job_id}-queued",
        "initial_event_digest": DIGEST,
        "request_digest": DIGEST,
    }
    assert set(COORDINATION_JOB_INSERT_COLUMNS) <= set(values), sorted(values)
    columns = ", ".join(COORDINATION_JOB_INSERT_COLUMNS)
    placeholders = ", ".join("%s" for _ in COORDINATION_JOB_INSERT_COLUMNS)
    admin.execute(
        f"INSERT INTO coordination_jobs ({columns}) VALUES ({placeholders})",
        tuple(values[column] for column in COORDINATION_JOB_INSERT_COLUMNS),
    )


def _bucket_no_is_nullable(admin: psycopg.Connection[Any], schema: str) -> str:
    row = admin.execute(
        "SELECT is_nullable FROM information_schema.columns "
        "WHERE table_schema = %s AND table_name = 'coordination_jobs' "
        "AND column_name = 'bucket_no'",
        (schema,),
    ).fetchone()
    assert row is not None, "coordination_jobs.bucket_no disappeared"
    return str(row[0])


def _canonical_row(admin: psycopg.Connection[Any], job_id: str) -> tuple[Any, ...]:
    row = admin.execute(
        "SELECT bucket_no, due_at, claim_expires_at, last_event_seq, claim_expires_at_us "
        "FROM coordination_jobs WHERE job_id = %s",
        (job_id,),
    ).fetchone()
    assert row is not None, f"coordination_jobs has no row for {job_id}"
    return tuple(row)


def test_0065_backfills_the_event_head_and_rows_written_after_0064() -> None:
    url = os.environ.get(VARIABLE) or pytest.fail(
        f"Configure an isolated PostgreSQL DSN in {VARIABLE}; skip is not migration evidence"
    )
    schema = "sn_0065_" + uuid.uuid4().hex[:12]
    engine = create_engine(
        url.replace("postgresql://", "postgresql+psycopg://", 1),
        poolclass=NullPool,
        connect_args={"options": "-c timezone=Asia/Shanghai"},
    )
    admin = portable_pg_connect(url, autocommit=True)
    try:
        admin.execute(f'CREATE SCHEMA "{schema}"')
        admin.execute(f'SET search_path TO "{schema}", pg_catalog')
        admin.execute("SET TimeZone TO 'Asia/Shanghai'")
        assert admin.execute("SHOW TimeZone").fetchone() == ("Asia/Shanghai",)

        # The migration connection runs in the same +08:00 zone on purpose: a backfill that
        # forgot AT TIME ZONE 'UTC' would store 20:00 instead of 12:00 and never complain.
        with engine.connect() as timezone_probe:
            assert timezone_probe.execute(text("SHOW TimeZone")).scalar_one() == "Asia/Shanghai"

        config = _config(schema)
        # Stop before 0064 so these rows exist when 0064's own backfill runs; the schema
        # then has no canonical columns at all, which is the pre-0064 deployment.
        with engine.connect() as connection:
            config.attributes["connection"] = connection
            command.upgrade(config, PRE_BACKFILL)

        # Inserted before 0064 ran: 0064 already filled their canonical columns.
        _insert_draft_job(
            admin,
            job_id="job-0065-three-events",
            claim_expires_at_us=MOMENT_US,
            speech_created_at_us=MOMENT_US,
        )
        for event_seq in (1, 2, 3):
            _insert_event(admin, job_id="job-0065-three-events", event_seq=event_seq)

        # Same event count, zero-based numbering: MAX is 2, so a COUNT-based backfill
        # (which would say 3) is wrong for the sequence the draft actually allocates.
        _insert_draft_job(
            admin,
            job_id="job-0065-zero-based-events",
            claim_expires_at_us=0,
            speech_created_at_us=MOMENT_US,
        )
        for event_seq in (0, 1, 2):
            _insert_event(admin, job_id="job-0065-zero-based-events", event_seq=event_seq)

        _insert_draft_job(
            admin,
            job_id="job-0065-no-events",
            claim_expires_at_us=0,
            speech_created_at_us=MOMENT_US,
        )

        with engine.connect() as connection:
            config.attributes["connection"] = connection
            command.upgrade(config, PREVIOUS)

        # The whole point of step 1: a row created while the schema was already at 0064
        # has NULL canonical columns, and only 0065's re-run of the 0064 backfill fills them.
        _insert_draft_job(
            admin,
            job_id="job-0065-after-0064",
            claim_expires_at_us=MOMENT_US,
            speech_created_at_us=MOMENT_US,
        )
        late = _canonical_row(admin, "job-0065-after-0064")
        assert late[0] is None and late[2] is None and late[3] == 0

        with engine.connect() as connection:
            config.attributes["connection"] = connection
            command.upgrade(config, REVISION)

        three = _canonical_row(admin, "job-0065-three-events")
        zero_based = _canonical_row(admin, "job-0065-zero-based-events")
        no_events = _canonical_row(admin, "job-0065-no-events")
        late = _canonical_row(admin, "job-0065-after-0064")

        # MAX(speech_job_events.event_seq) per Job, 0 without events.
        assert three[3] == 3
        assert zero_based[3] == 2
        assert no_events[3] == 0
        assert late[3] == 0

        # Rows written after 0064 are reconciled by 0065, by the one Python bucket
        # definition rather than a second hash written in the test.
        assert late[0] == int(bucket_for("job-0065-after-0064"))
        assert three[0] == int(bucket_for("job-0065-three-events"))
        # Positive epoch -> naive UTC wall clock; 0 -> NULL, never 1970-01-01.
        assert late[2] == NAIVE_MOMENT
        assert three[2] == NAIVE_MOMENT
        assert no_events[2] is None
        assert late[1] == NAIVE_MOMENT
        assert three[1] == NAIVE_MOMENT
        for moment in (late[1], late[2], three[1], three[2]):
            assert isinstance(moment, datetime)
            assert moment.tzinfo is None

        # Re-running the pair (downgrade then upgrade) must close a second window too:
        # this is the version-stamped re-entry pattern from ``tests/support/migrations``,
        # where the revision runs again on a table that already has the late writes.
        with engine.connect() as connection:
            config.attributes["connection"] = connection
            command.downgrade(config, PREVIOUS)
        _insert_draft_job(
            admin,
            job_id="job-0065-second-window",
            claim_expires_at_us=0,
            speech_created_at_us=MOMENT_US,
        )
        assert _canonical_row(admin, "job-0065-second-window")[0] is None
        with engine.connect() as connection:
            config.attributes["connection"] = connection
            command.upgrade(config, REVISION)
        second = _canonical_row(admin, "job-0065-second-window")
        assert second[0] == int(bucket_for("job-0065-second-window"))
        assert second[2] is None and second[3] == 0
        # The first window's event head survives a second entry, so the backfill is a
        # whole-table recompute rather than an append that would double-count.
        assert _canonical_row(admin, "job-0065-three-events")[3] == 3

        # 0065 leaves bucket_no nullable on purpose: the live writer's six-column INSERT
        # does not supply it, so NOT NULL here would reject every new Job (23502 ->
        # 503 job_unavailable). The deferral is tied to the writer, not to memory: once
        # the write path supplies bucket_no, this assertion goes red and the tightening
        # becomes implementable.
        assert "bucket_no" not in COORDINATION_JOB_INSERT_COLUMNS, (
            "the Job writer now supplies bucket_no; 0065's deferral of NOT NULL is stale"
        )
        assert _bucket_no_is_nullable(admin, schema) == "YES"
        _insert_like_the_live_writer(admin, job_id="job-0065-live-writer")
        live = _canonical_row(admin, "job-0065-live-writer")
        # A Job created after the migration is accepted exactly as the runtime writes it;
        # its canonical columns stay empty until the switch, which is 0064's contract.
        assert live[0] is None and live[3] == 0
    finally:
        admin.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        admin.close()
        engine.dispose()


def test_0065_downgrade_restores_0064_semantics_without_losing_rows() -> None:
    url = os.environ.get(VARIABLE)
    if not url:
        pytest.fail(
            f"Configure an isolated PostgreSQL DSN in {VARIABLE}; skip is not migration evidence"
        )
    schema = "sn_0065_down_" + uuid.uuid4().hex[:12]
    engine = create_engine(
        url.replace("postgresql://", "postgresql+psycopg://", 1),
        poolclass=NullPool,
        connect_args={"options": "-c timezone=Asia/Shanghai"},
    )
    admin = portable_pg_connect(url, autocommit=True)
    try:
        admin.execute(f'CREATE SCHEMA "{schema}"')
        admin.execute(f'SET search_path TO "{schema}", pg_catalog')
        admin.execute("SET TimeZone TO 'Asia/Shanghai'")

        config = _config(schema)
        with engine.connect() as connection:
            config.attributes["connection"] = connection
            command.upgrade(config, PREVIOUS)

        _insert_draft_job(
            admin,
            job_id="job-0065-claimed",
            claim_expires_at_us=MOMENT_US,
            speech_created_at_us=MOMENT_US,
        )
        _insert_draft_job(
            admin,
            job_id="job-0065-unclaimed",
            claim_expires_at_us=0,
            speech_created_at_us=MOMENT_US + 1_000_000,
        )
        for event_seq in (1, 2):
            _insert_event(admin, job_id="job-0065-claimed", event_seq=event_seq)

        with engine.connect() as connection:
            config.attributes["connection"] = connection
            command.upgrade(config, REVISION)
        # Before the downgrade the head is the real event MAX, so zeroing it later is a
        # change rather than a state the downgrade inherited.
        assert _canonical_row(admin, "job-0065-claimed")[3] == 2
        with engine.connect() as connection:
            config.attributes["connection"] = connection
            command.downgrade(config, PREVIOUS)

        # 0064 owns the columns, so they must survive; the head goes back to the placeholder.
        # (0065 never tightened bucket_no, so this asserts 0064 semantics survive the
        # round trip rather than that a constraint was removed.)
        assert _bucket_no_is_nullable(admin, schema) == "YES"
        claimed = _canonical_row(admin, "job-0065-claimed")
        unclaimed = _canonical_row(admin, "job-0065-unclaimed")
        assert claimed[3] == 0 and unclaimed[3] == 0
        # Everything else is still there: bucket assignment, times and the draft epoch.
        assert claimed[0] == int(bucket_for("job-0065-claimed"))
        assert claimed[2] == NAIVE_MOMENT
        assert claimed[1] == NAIVE_MOMENT
        assert claimed[4] == MOMENT_US
        assert unclaimed[2] is None
        assert unclaimed[1] == datetime(2026, 11, 23, 12, 0, 1)

        # The 0064-era insert path still works after the round trip.
        _insert_like_the_live_writer(admin, job_id="job-0065-after-downgrade")
        assert _canonical_row(admin, "job-0065-after-downgrade")[0] is None
    finally:
        admin.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        admin.close()
        engine.dispose()
