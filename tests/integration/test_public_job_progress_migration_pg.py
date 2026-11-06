"""Verify 0039 upgrades over existing facts, guards progress, and refuses an unsafe downgrade."""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
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
PROGRESS_COLUMNS = ("progress_unit", "progress_completed", "progress_total")
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


@contextmanager
def _isolated_schema(
    url: str,
) -> Iterator[tuple[Config, psycopg.Connection[dict[str, Any]]]]:
    """Own one throwaway schema so a failing assertion cannot leak state."""
    # The shard owns the server and its database; only the configured DSN is
    # authoritative, so never assume a local database name.
    schema = "sn_progress_mig_" + uuid.uuid4().hex[:12]
    engine = create_engine(
        url.replace("postgresql://", "postgresql+psycopg://", 1), poolclass=NullPool
    )
    admin = psycopg.connect(url, autocommit=True, row_factory=dict_row)
    query = psycopg.connect(url, autocommit=True, row_factory=dict_row)
    try:
        admin.execute(f'CREATE SCHEMA "{schema}"')
        query.execute(f'SET search_path TO "{schema}", pg_catalog')
        with engine.connect() as connection:
            yield _config(schema, connection), query
    finally:
        query.close()
        admin.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        admin.close()
        engine.dispose()


def _url() -> str:
    url = os.environ.get(VARIABLE)
    if not url:
        pytest.fail("Configure an isolated PostgreSQL DSN; skip is not migration evidence")
    return url


def _seed_pre_0039_facts(query: psycopg.Connection[dict[str, Any]]) -> list[tuple[Any, ...]]:
    """Commit lifecycle facts in the shape that exists before 0039."""
    digest = "sha256:" + "f" * 64
    query.execute(
        "INSERT INTO speech_jobs (job_id, workspace_id, kind, model_id, "
        "request_digest, state, status_version, created_at_us, updated_at_us) "
        "VALUES ('job-backfill-pending', 'workspace-default', 'tts', 'tts-model', "
        "%s, 'pending', 1, 100, 100)",
        (digest,),
    )
    query.execute(
        "INSERT INTO speech_jobs (job_id, workspace_id, kind, model_id, "
        "request_digest, state, status_version, error_code, created_at_us, updated_at_us) "
        "VALUES ('job-backfill-failed', 'workspace-default', 'asr', 'asr-model', "
        "%s, 'failed', 3, 'engine_failure', 200, 400)",
        ("sha256:" + "0" * 64,),
    )
    query.execute(
        "INSERT INTO speech_job_events (job_id, event_seq, state, occurred_at_us) VALUES "
        "('job-backfill-pending', 0, 'pending', 100), "
        "('job-backfill-failed', 0, 'pending', 200), "
        "('job-backfill-failed', 1, 'running', 300), "
        "('job-backfill-failed', 2, 'failed', 400)"
    )
    rows = query.execute(
        "SELECT job_id, event_seq, state, occurred_at_us FROM speech_job_events "
        "ORDER BY job_id, event_seq"
    ).fetchall()
    return [(row["job_id"], row["event_seq"], row["state"], row["occurred_at_us"]) for row in rows]


def _current_schema(connection: psycopg.Connection[dict[str, Any]]) -> str:
    row = connection.execute("SELECT current_schema() AS name").fetchone()
    assert row is not None
    return str(row["name"])


def _progress_facts(connection: psycopg.Connection[dict[str, Any]]) -> int:
    row = connection.execute(
        "SELECT count(*) AS n FROM speech_job_events WHERE kind = 'progress'"
    ).fetchone()
    assert row is not None
    return int(row["n"])


def test_upgrade_backfills_existing_event_rows_as_lifecycle() -> None:
    with _isolated_schema(_url()) as (config, query):
        command.upgrade(config, "0038_voice_catalogue_reader")
        schema = _current_schema(query)
        assert not _column(query, schema, "kind")
        before = _seed_pre_0039_facts(query)

        # Adding NOT NULL with a default rewrites every existing row; the
        # upgrade must succeed with facts already committed, not only on an
        # empty table.
        command.upgrade(config, "0039_public_job_progress")

        rows = query.execute(
            "SELECT job_id, event_seq, state, occurred_at_us, kind, progress_unit, "
            "progress_completed, progress_total FROM speech_job_events "
            "ORDER BY job_id, event_seq"
        ).fetchall()
        assert [
            (row["job_id"], row["event_seq"], row["state"], row["occurred_at_us"]) for row in rows
        ] == before
        assert [row["kind"] for row in rows] == ["lifecycle"] * len(before)
        assert all(row[column] is None for row in rows for column in PROGRESS_COLUMNS)
        assert _progress_facts(query) == 0
        kind_column = query.execute(
            "SELECT is_nullable, column_default FROM information_schema.columns "
            "WHERE table_schema = current_schema() AND table_name = 'speech_job_events' "
            "AND column_name = 'kind'"
        ).fetchone()
        assert kind_column is not None
        assert kind_column["is_nullable"] == "NO"
        assert kind_column["column_default"] == "'lifecycle'::character varying"


def test_progress_migration_round_trip_and_guarded_facts() -> None:
    with _isolated_schema(_url()) as (config, query):
        command.upgrade(config, "0038_voice_catalogue_reader")
        schema = _current_schema(query)
        assert not _column(query, schema, "kind")

        command.upgrade(config, "0039_public_job_progress")
        for name in ("kind", *PROGRESS_COLUMNS):
            assert _column(query, schema, name)

        # A downgrade with no progress fact is allowed.
        command.downgrade(config, "0038_voice_catalogue_reader")
        assert not _column(query, schema, "kind")
        command.upgrade(config, "0039_public_job_progress")

        query.execute(
            "INSERT INTO speech_jobs (job_id, workspace_id, kind, model_id, "
            "request_digest, state, status_version, created_at_us, updated_at_us) "
            "VALUES ('job-progress-migrate', 'workspace-default', 'tts', 'tts-model', "
            "%s, 'running', 2, 1, 1)",
            ("sha256:" + "e" * 64,),
        )
        query.execute(
            "INSERT INTO speech_job_submissions "
            "(job_id, request_id, client_reference, effective_configuration_json, "
            "execution_revision, progress_unit, progress_total, submitted_at_us) "
            "VALUES ('job-progress-migrate', 'req-progress-migrate', "
            "'synthetic-progress', '{}', 1, 'audio_ms', NULL, 1)"
        )
        query.execute(
            "INSERT INTO speech_job_events "
            "(job_id, event_seq, state, occurred_at_us) "
            "VALUES ('job-progress-migrate', 0, 'pending', 1)"
        )
        query.execute(
            "INSERT INTO speech_job_events "
            "(job_id, event_seq, state, occurred_at_us, kind, progress_unit, "
            "progress_completed) VALUES "
            "('job-progress-migrate', 1, 'running', 2, 'progress', 'audio_ms', 10)"
        )
        with pytest.raises(psycopg.errors.CheckViolation) as shape_failure:
            query.execute(
                "INSERT INTO speech_job_events "
                "(job_id, event_seq, state, occurred_at_us, kind, progress_unit) VALUES "
                "('job-progress-migrate', 2, 'running', 3, 'progress', 'audio_ms')"
            )
        assert shape_failure.value.diag.constraint_name == "speech_job_events_progress_shape_check"
        with pytest.raises(psycopg.errors.CheckViolation) as duplicate_failure:
            query.execute(
                "INSERT INTO speech_job_events "
                "(job_id, event_seq, state, occurred_at_us, kind, progress_unit, "
                "progress_completed) VALUES "
                "('job-progress-migrate', 2, 'running', 3, 'progress', 'audio_ms', 10)"
            )
        assert "must strictly advance" in str(duplicate_failure.value)
        with pytest.raises(psycopg.errors.CheckViolation) as unit_failure:
            query.execute(
                "INSERT INTO speech_job_events "
                "(job_id, event_seq, state, occurred_at_us, kind, progress_unit, "
                "progress_completed) VALUES "
                "('job-progress-migrate', 2, 'running', 3, 'progress', 'items', 20)"
            )
        assert "frozen submission" in str(unit_failure.value)
        with pytest.raises(psycopg.errors.CheckViolation) as sequence_failure:
            query.execute(
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
        query.execute("DELETE FROM speech_job_events WHERE job_id = 'job-progress-migrate'")
        query.execute("DELETE FROM speech_job_submissions WHERE job_id = 'job-progress-migrate'")
        query.execute("DELETE FROM speech_jobs WHERE job_id = 'job-progress-migrate'")
        command.downgrade(config, "0038_voice_catalogue_reader")
        command.upgrade(config, "head")
