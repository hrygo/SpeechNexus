"""Verify 0044 admits voice_preview, round-trips cleanly and refuses lossy downgrade."""

from __future__ import annotations

import os

import psycopg
import pytest
from alembic import command
from tests.integration.test_public_job_progress_migration_pg import _isolated_schema
from tests.integration.test_voice_draft_revisions_migration_pg import _kind_check, _seed_draft

pytestmark = pytest.mark.integration


def _url() -> str:
    url = os.environ.get("SPEECHNEXUS_TEST_DATABASE_URL")
    if not url:
        pytest.fail("Configure an isolated PostgreSQL DSN; skip is not migration evidence")
    return url


def _insert_preview(query: object, job_id: str = "job-preview-1") -> None:
    query.execute(  # type: ignore[attr-defined]
        "INSERT INTO speech_jobs (job_id, workspace_id, kind, model_id, request_digest, "
        "state, status_version, created_at_us, updated_at_us) "
        "VALUES (%s, 'workspace-default', 'voice_preview', 'voice-model', "
        "%s, 'pending', 1, 300, 300)",
        (job_id, "sha256:" + "d" * 64),
    )


def test_0044_admits_voice_preview_and_round_trips() -> None:
    with _isolated_schema(_url()) as (config, query):
        command.upgrade(config, "0043_voice_draft_revisions")
        _seed_draft(query)
        assert "voice_preview" not in _kind_check(query)
        # The five-kind CHECK still refuses the new kind before 0044.
        with pytest.raises(psycopg.errors.CheckViolation) as refused:
            _insert_preview(query)
        assert "speech_jobs_kind_check" in str(refused.value)

        command.upgrade(config, "head")
        assert "voice_preview" in _kind_check(query)
        _insert_preview(query)

        # Downgrade once clears the kind; a second upgrade restores it. No
        # preview row exists at this point, so the narrow CHECK is accepted.
        query.execute("DELETE FROM speech_jobs WHERE job_id='job-preview-1'")
        command.downgrade(config, "0043_voice_draft_revisions")
        assert "voice_preview" not in _kind_check(query)
        command.upgrade(config, "head")
        assert "voice_preview" in _kind_check(query)


def test_0044_downgrade_refuses_to_drop_preview_history() -> None:
    with _isolated_schema(_url()) as (config, query):
        command.upgrade(config, "head")
        _insert_preview(query, "job-preview-history")
        with pytest.raises(RuntimeError) as failure:
            command.downgrade(config, "0043_voice_draft_revisions")
        assert "voice_preview" in str(failure.value)
        # The refused downgrade leaves 0044 applied and the row reachable.
        assert "voice_preview" in _kind_check(query)
        retained = query.execute(
            "SELECT count(*) AS n FROM speech_jobs WHERE kind='voice_preview'"
        ).fetchone()
        assert retained is not None
        assert retained["n"] == 1
