"""Verify 0039 upgrades, guards progress facts, and refuses an unsafe downgrade."""

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


def _column(connection: psycopg.Connection[dict[str, Any]], schema: str, name: str) -> bool:
    return bool(
        connection.execute(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_schema = %s AND table_name = 'speech_job_events' "
            "AND column_name = %s",
            (schema, name),
        ).fetchall()
    )


def test_progress_migration_round_trip_and_guarded_facts() -> None:
    # The shard owns the server and its database; only the configured DSN is
    # authoritative, so never assume a local database name.
    url = os.environ.get(VARIABLE)
    if not url:
        pytest.fail("Configure an isolated PostgreSQL DSN; skip is not migration evidence")
    schema = "sn_progress_mig_" + uuid.uuid4().hex[:12]
    engine = create_engine(
        url.replace("postgresql://", "postgresql+psycopg://", 1), poolclass=NullPool
    )
    admin = psycopg.connect(url, autocommit=True, row_factory=dict_row)
    try:
        admin.execute(f'CREATE SCHEMA "{schema}"')
        with engine.connect() as connection:
            config = _config(schema, connection)
            command.upgrade(config, "0038_voice_catalogue_reader")
            columns = psycopg.connect(url, autocommit=True, row_factory=dict_row)
            columns.execute(f'SET search_path TO "{schema}", pg_catalog')
            assert not _column(columns, schema, "kind")

            command.upgrade(config, "0039_public_job_progress")
            for name in ("kind", "progress_unit", "progress_completed", "progress_total"):
                assert _column(columns, schema, name)

            # A downgrade with no progress fact is allowed.
            command.downgrade(config, "0038_voice_catalogue_reader")
            assert not _column(columns, schema, "kind")
            command.upgrade(config, "0039_public_job_progress")

            columns.execute(
                "INSERT INTO speech_jobs (job_id, workspace_id, kind, model_id, "
                "request_digest, state, status_version, created_at_us, updated_at_us) "
                "VALUES ('job-progress-migrate', 'workspace-default', 'tts', 'tts-model', "
                "%s, 'running', 2, 1, 1)",
                ("sha256:" + "e" * 64,),
            )
            columns.execute(
                "INSERT INTO speech_job_submissions "
                "(job_id, request_id, client_reference, effective_configuration_json, "
                "execution_revision, progress_unit, progress_total, submitted_at_us) "
                "VALUES ('job-progress-migrate', 'req-progress-migrate', "
                "'synthetic-progress', '{}', 1, 'audio_ms', NULL, 1)"
            )
            columns.execute(
                "INSERT INTO speech_job_events "
                "(job_id, event_seq, state, occurred_at_us) "
                "VALUES ('job-progress-migrate', 0, 'pending', 1)"
            )
            columns.execute(
                "INSERT INTO speech_job_events "
                "(job_id, event_seq, state, occurred_at_us, kind, progress_unit, "
                "progress_completed) VALUES "
                "('job-progress-migrate', 1, 'running', 2, 'progress', 'audio_ms', 10)"
            )
            with pytest.raises(psycopg.errors.CheckViolation) as shape_failure:
                columns.execute(
                    "INSERT INTO speech_job_events "
                    "(job_id, event_seq, state, occurred_at_us, kind, progress_unit) VALUES "
                    "('job-progress-migrate', 2, 'running', 3, 'progress', 'audio_ms')"
                )
            assert (
                shape_failure.value.diag.constraint_name == "speech_job_events_progress_shape_check"
            )
            with pytest.raises(psycopg.errors.CheckViolation) as duplicate_failure:
                columns.execute(
                    "INSERT INTO speech_job_events "
                    "(job_id, event_seq, state, occurred_at_us, kind, progress_unit, "
                    "progress_completed) VALUES "
                    "('job-progress-migrate', 2, 'running', 3, 'progress', 'audio_ms', 10)"
                )
            assert "must strictly advance" in str(duplicate_failure.value)
            with pytest.raises(psycopg.errors.CheckViolation) as unit_failure:
                columns.execute(
                    "INSERT INTO speech_job_events "
                    "(job_id, event_seq, state, occurred_at_us, kind, progress_unit, "
                    "progress_completed) VALUES "
                    "('job-progress-migrate', 2, 'running', 3, 'progress', 'items', 20)"
                )
            assert "frozen submission" in str(unit_failure.value)
            with pytest.raises(psycopg.errors.CheckViolation) as sequence_failure:
                columns.execute(
                    "INSERT INTO speech_job_events "
                    "(job_id, event_seq, state, occurred_at_us, kind, progress_unit, "
                    "progress_completed) VALUES "
                    "('job-progress-migrate', 3, 'running', 4, 'progress', 'audio_ms', 30)"
                )
            assert "sequence must follow" in str(sequence_failure.value)
            # The older shape cannot represent a committed progress fact.
            with pytest.raises(RuntimeError) as failure:
                command.downgrade(config, "0038_voice_catalogue_reader")
            assert "progress facts" in str(failure.value)
            columns.execute("DELETE FROM speech_job_events WHERE job_id = 'job-progress-migrate'")
            columns.execute(
                "DELETE FROM speech_job_submissions WHERE job_id = 'job-progress-migrate'"
            )
            columns.execute("DELETE FROM speech_jobs WHERE job_id = 'job-progress-migrate'")
            command.downgrade(config, "0038_voice_catalogue_reader")
            command.upgrade(config, "head")
    finally:
        admin.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        admin.close()
        engine.dispose()
