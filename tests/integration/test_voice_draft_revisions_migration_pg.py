"""Verify 0043 backfills revision one, admits validation, and downgrades cleanly."""

from __future__ import annotations

import os

import pytest
from alembic import command
from sqlalchemy.exc import IntegrityError
from tests.integration.test_public_job_progress_migration_pg import _isolated_schema

pytestmark = pytest.mark.integration


def _url() -> str:
    url = os.environ.get("SPEECHNEXUS_TEST_DATABASE_URL")
    if not url:
        pytest.fail("Configure an isolated PostgreSQL DSN; skip is not migration evidence")
    return url


def _kind_check(query: object) -> str:
    row = query.execute(  # type: ignore[attr-defined]
        "SELECT pg_get_constraintdef(c.oid) AS definition "
        "FROM pg_constraint AS c "
        "JOIN pg_class AS t ON t.oid = c.conrelid "
        "JOIN pg_namespace AS n ON n.oid = t.relnamespace "
        "WHERE c.conname='speech_jobs_kind_check' "
        "AND n.nspname = current_schema() AND t.relname = 'speech_jobs'"
    ).fetchone()
    assert row is not None
    return str(row["definition"])


def _seed_draft(query: object) -> None:
    query.execute(  # type: ignore[attr-defined]
        "INSERT INTO speech_jobs (job_id, workspace_id, kind, model_id, request_digest, "
        "state, status_version, created_at_us, updated_at_us) "
        "VALUES ('job-draft-1', 'workspace-default', 'voice_design', 'voice-model', "
        "%s, 'pending', 1, 100, 100)",
        ("sha256:" + "a" * 64,),
    )
    query.execute(  # type: ignore[attr-defined]
        "INSERT INTO speech_voice_drafts (draft_id, generation_job_id, kind, status, "
        "head_revision, metadata_revision, display_name, input_json, binding_json, "
        "created_at_us, updated_at_us) VALUES ('draft-job-1', 'job-draft-1', 'design', "
        "'generating', '1', 1, 'Synthetic voice', %s, %s, 100, 100)",
        ('{"description":"x"}', '{"id":"voice-model","revision":"1"}'),
    )


def test_0043_backfills_revision_one_and_admits_validation() -> None:
    with _isolated_schema(_url()) as (config, query):
        command.upgrade(config, "0042_drop_superseded_job_index")
        _seed_draft(query)
        command.upgrade(config, "head")
        revision = query.execute(
            "SELECT draft_id, revision, kind, generation_job_id, input_json, binding_json, "
            "created_at_us FROM speech_voice_draft_revisions"
        ).fetchone()
        assert revision is not None
        assert (
            revision["draft_id"],
            revision["revision"],
            revision["kind"],
            revision["generation_job_id"],
            revision["created_at_us"],
        ) == ("draft-job-1", "1", "design", "job-draft-1", 100)
        assert "voice_validation" in _kind_check(query)
        query.execute(
            "INSERT INTO speech_jobs (job_id, workspace_id, kind, model_id, request_digest, "
            "state, status_version, created_at_us, updated_at_us) "
            "VALUES ('job-validation-1', 'workspace-default', 'voice_validation', "
            "'voice-model', %s, 'pending', 1, 200, 200)",
            ("sha256:" + "b" * 64,),
        )
        query.execute("DELETE FROM speech_jobs WHERE job_id='job-validation-1'")
        command.downgrade(config, "0042_drop_superseded_job_index")
        dropped = query.execute(
            "SELECT to_regclass('speech_voice_draft_revisions') AS name"
        ).fetchone()
        assert dropped is not None
        assert dropped["name"] is None
        assert "voice_validation" not in _kind_check(query)


def test_0043_downgrade_refuses_to_drop_revisions_with_validation_history() -> None:
    with _isolated_schema(_url()) as (config, query):
        command.upgrade(config, "head")
        query.execute(
            "INSERT INTO speech_jobs (job_id, workspace_id, kind, model_id, request_digest, "
            "state, status_version, created_at_us, updated_at_us) "
            "VALUES ('job-validation-history', 'workspace-default', 'voice_validation', "
            "'voice-model', %s, 'pending', 1, 200, 200)",
            ("sha256:" + "c" * 64,),
        )
        with pytest.raises(IntegrityError) as failure:
            command.downgrade(config, "0042_drop_superseded_job_index")
        assert "speech_jobs_kind_check" in str(failure.value)
        retained = query.execute(
            "SELECT to_regclass('speech_voice_draft_revisions') AS name"
        ).fetchone()
        assert retained is not None
        assert retained["name"] == "speech_voice_draft_revisions"
        assert "voice_validation" in _kind_check(query)
