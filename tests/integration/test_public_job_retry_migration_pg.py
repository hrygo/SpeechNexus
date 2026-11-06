"""Verify 0037 upgrades, refuses an unsafe downgrade, and reverses cleanly."""

from __future__ import annotations

import os
import uuid
from pathlib import Path
from typing import Any

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from psycopg.rows import dict_row
from sqlalchemy import create_engine
from sqlalchemy.pool import NullPool

ROOT = Path(__file__).resolve().parents[2]
VARIABLE = "SPEECHNEXUS_TEST_DATABASE_URL"
pytestmark = pytest.mark.integration


def _config(schema: str, connection: Any) -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.attributes["schema"] = schema
    config.attributes["connection"] = connection
    return config


def test_retry_migration_round_trip_and_safe_downgrade() -> None:
    # The shard owns the server and its database; only the configured DSN is
    # authoritative, so never assume a local database name.
    url = os.environ.get(VARIABLE)
    if not url:
        pytest.fail("Configure an isolated PostgreSQL DSN; skip is not migration evidence")
    schema = "sn_retry_mig_" + uuid.uuid4().hex[:12]
    engine = create_engine(
        url.replace("postgresql://", "postgresql+psycopg://", 1), poolclass=NullPool
    )
    admin = psycopg.connect(url, autocommit=True, row_factory=dict_row)
    try:
        admin.execute(f'CREATE SCHEMA "{schema}"')
        with engine.connect() as connection:
            config = _config(schema, connection)
            command.upgrade(config, "0036_abort_dispatch_attempts")
            columns = psycopg.connect(url, autocommit=True, row_factory=dict_row)
            columns.execute(f'SET search_path TO "{schema}", pg_catalog')
            assert (
                columns.execute(
                    "SELECT 1 FROM information_schema.columns "
                    "WHERE table_schema = %s AND table_name = 'speech_jobs' "
                    "AND column_name = 'retry_of_job_id'",
                    (schema,),
                ).fetchall()
                == []
            )
            command.upgrade(config, "0037_public_job_retries")
            assert (
                columns.execute(
                    "SELECT 1 FROM information_schema.columns "
                    "WHERE table_schema = %s AND table_name = 'speech_jobs' "
                    "AND column_name = 'retry_of_job_id'",
                    (schema,),
                ).fetchall()
                != []
            )
            # A downgrade with no lineage is allowed.
            command.downgrade(config, "0036_abort_dispatch_attempts")
            assert (
                columns.execute(
                    "SELECT 1 FROM information_schema.columns "
                    "WHERE table_schema = %s AND table_name = 'speech_jobs' "
                    "AND column_name = 'retry_of_job_id'",
                    (schema,),
                ).fetchall()
                == []
            )
            command.upgrade(config, "0037_public_job_retries")
            # Seed a lineage pair and prove the downgrade refuses to drop it.
            for job_id, parent in (
                ("job-migrate-parent", None),
                ("job-migrate-child", "job-migrate-parent"),
            ):
                columns.execute(
                    "INSERT INTO speech_jobs (job_id, workspace_id, kind, model_id, "
                    "request_digest, state, status_version, created_at_us, updated_at_us, "
                    "retry_of_job_id) VALUES (%s, 'workspace-default', 'tts', 'm', %s, "
                    "'pending', 1, 1, 1, %s)",
                    (job_id, "sha256:" + "d" * 64, parent),
                )
            with pytest.raises(RuntimeError) as failure:
                command.downgrade(config, "0036_abort_dispatch_attempts")
            assert "replacement lineage" in str(failure.value)
            columns.execute("DELETE FROM speech_jobs WHERE job_id LIKE 'job-migrate-%'")
            command.downgrade(config, "0036_abort_dispatch_attempts")
            command.upgrade(config, "head")
    finally:
        admin.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        admin.close()
        engine.dispose()
