"""0064 adds the canonical coordination_jobs columns, backfills them, and drops only those.

The whole point of segment A is that the old epoch columns stay authoritative: this test
proves the new columns can be populated without touching ``claim_expires_at_us`` and that
``downgrade`` removes exactly the four additive columns.

The migration connection runs in ``Asia/Shanghai`` on purpose. ``due_at`` and
``claim_expires_at`` are naive ``TIMESTAMP(6)`` columns; if the backfill forgot
``AT TIME ZONE 'UTC'`` the stored wall clock would silently shift by eight hours.
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

from speechnexus_portable_sql.claims import bucket_for

pytestmark = pytest.mark.integration
ROOT = Path(__file__).resolve().parents[2]
VARIABLE = "SPEECHNEXUS_TEST_DATABASE_URL"
DIGEST = "sha256:" + "a" * 64
MOMENT = datetime(2026, 11, 23, 12, 0, 0, tzinfo=UTC)
MOMENT_US = int(MOMENT.timestamp()) * 1_000_000
NAIVE_MOMENT = datetime(2026, 11, 23, 12, 0, 0)
ADDED_COLUMNS = {"bucket_no", "last_event_seq", "due_at", "claim_expires_at"}


def _columns(admin: psycopg.Connection[Any], schema: str) -> set[str]:
    rows = admin.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = %s AND table_name = 'coordination_jobs'",
        (schema,),
    ).fetchall()
    return {str(row[0]) for row in rows}


def _insert_job(
    admin: psycopg.Connection[Any],
    *,
    job_id: str,
    claim_expires_at_us: int,
    speech_created_at_us: int | None,
) -> None:
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
            "VALUES (%s, 'workspace-0064', 'tts', 'model-0064', %s, %s, %s)",
            (job_id, DIGEST, speech_created_at_us, speech_created_at_us),
        )


def _canonical_rows(
    admin: psycopg.Connection[Any],
) -> dict[str, tuple[Any, ...]]:
    rows = admin.execute(
        "SELECT job_id, bucket_no, due_at, claim_expires_at, last_event_seq "
        "FROM coordination_jobs ORDER BY job_id"
    ).fetchall()
    return {str(row[0]): row for row in rows}


def test_0064_backfills_canonical_columns_and_downgrade_removes_only_them() -> None:
    url = os.environ.get(VARIABLE)
    if not url:
        pytest.fail(
            f"Configure an isolated PostgreSQL DSN in {VARIABLE}; skip is not migration evidence"
        )
    schema = "sn_0064_" + uuid.uuid4().hex[:12]
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

        # Same session timezone as the migration connection; a signed shift would be visible.
        with engine.connect() as timezone_probe:
            assert timezone_probe.execute(text("SHOW TimeZone")).scalar_one() == "Asia/Shanghai"

        config = Config(str(ROOT / "alembic.ini"))
        config.attributes["schema"] = schema
        with engine.connect() as connection:
            config.attributes["connection"] = connection
            command.upgrade(config, "0063_deletion_targets_blocked")

        assert "claim_expires_at_us" in _columns(admin, schema)
        assert ADDED_COLUMNS.isdisjoint(_columns(admin, schema))

        _insert_job(
            admin,
            job_id="job-0064-unclaimed",
            claim_expires_at_us=0,
            speech_created_at_us=MOMENT_US,
        )
        _insert_job(
            admin,
            job_id="job-0064-claimed",
            claim_expires_at_us=MOMENT_US,
            speech_created_at_us=MOMENT_US + 1_000_000,
        )
        # No public speech_jobs row: due_at has no source yet and must stay NULL,
        # not be invented at the epoch.
        _insert_job(
            admin,
            job_id="job-0064-no-public-row",
            claim_expires_at_us=0,
            speech_created_at_us=None,
        )

        with engine.connect() as connection:
            config.attributes["connection"] = connection
            command.upgrade(config, "0064_canonical_jobs_columns")

        rows = _canonical_rows(admin)
        unclaimed = rows["job-0064-unclaimed"]
        claimed = rows["job-0064-claimed"]
        no_public = rows["job-0064-no-public-row"]

        # sha256(job_id) first 8 bytes big-endian mod 256, computed by the one Python
        # definition rather than reimplemented in the test.
        assert unclaimed[1] == int(bucket_for("job-0064-unclaimed"))
        assert claimed[1] == int(bucket_for("job-0064-claimed"))
        assert no_public[1] == int(bucket_for("job-0064-no-public-row"))

        # 0 means "never claimed" in the draft; it must not become 1970-01-01.
        assert unclaimed[3] is None
        assert no_public[3] is None
        # Positive epoch -> naive UTC wall clock, despite the Asia/Shanghai session.
        assert claimed[3] == NAIVE_MOMENT

        # canonical_source_map.json freezes due_at's source at speech_jobs.created_at_us.
        assert unclaimed[2] == NAIVE_MOMENT
        assert claimed[2] == datetime(2026, 11, 23, 12, 0, 1)
        assert no_public[2] is None

        assert unclaimed[4] == claimed[4] == no_public[4] == 0

        with engine.connect() as connection:
            config.attributes["connection"] = connection
            command.downgrade(config, "0063_deletion_targets_blocked")

        remaining = _columns(admin, schema)
        assert ADDED_COLUMNS.isdisjoint(remaining)
        assert "claim_expires_at_us" in remaining
        kept = admin.execute(
            "SELECT job_id, claim_expires_at_us FROM coordination_jobs ORDER BY job_id"
        ).fetchall()
        assert kept == [
            ("job-0064-claimed", MOMENT_US),
            ("job-0064-no-public-row", 0),
            ("job-0064-unclaimed", 0),
        ]
    finally:
        admin.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        admin.close()
        engine.dispose()
