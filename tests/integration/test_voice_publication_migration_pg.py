"""0047 upgrade shape, idempotent rerun, and refusal to discard measured evidence."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from alembic import command
from psycopg import Connection
from tests.integration.test_public_job_progress_migration_pg import (
    _isolated_schema,
    _url,
)
from tests.support.migrations import head_revision

pytestmark = pytest.mark.integration

_EVIDENCE = "speech_voice_capability_evidence"
_REVISIONS = "speech_voice_revisions"
_INTENTS = "speech_voice_publication_intents"
_VIEW = "speech_published_voice_revisions"
_DRAFT_VIEW = "speech_voice_draft_publications"


def _one(
    query: Connection[dict[str, Any]], statement: str, parameters: tuple[Any, ...] = ()
) -> dict[str, Any]:
    row = query.execute(statement, parameters).fetchone()
    assert row is not None
    return row


def _seed_measured_evidence(query: Connection[dict[str, Any]]) -> None:
    digest = "sha256:" + "a" * 64
    for job_id in ("job-evidence-generation", "job-evidence-validation"):
        query.execute(
            "INSERT INTO speech_jobs (job_id, workspace_id, kind, model_id, "
            "request_digest, state, status_version, created_at_us, updated_at_us) "
            "VALUES (%s, 'workspace-default', 'voice_clone', 'voice-model', "
            "%s, 'pending', 1, 100, 100)",
            (job_id, digest),
        )
    query.execute(
        "INSERT INTO speech_voice_drafts "
        "(draft_id, generation_job_id, kind, status, head_revision, metadata_revision, "
        "display_name, input_json, binding_json, created_at_us, updated_at_us) "
        "VALUES ('draft-evidence', 'job-evidence-generation', 'clone', 'validating', "
        "'1', 1, 'Synthetic', '{\"reference_audio_asset_id\": \"asset-1\"}', "
        '\'{"id": "voice-model", "revision": "1"}\', 100, 100)'
    )
    query.execute(
        "INSERT INTO speech_voice_draft_revisions "
        "(draft_id, revision, kind, generation_job_id, input_json, binding_json, created_at_us) "
        "VALUES ('draft-evidence', '1', 'clone', 'job-evidence-generation', "
        '\'{"reference_audio_asset_id": "asset-1"}\', '
        '\'{"id": "voice-model", "revision": "1"}\', 100)'
    )
    query.execute(
        f"INSERT INTO {_EVIDENCE} "
        "(evidence_id, draft_id, draft_revision, validation_job_id, validation_id, "
        "binding_revision_id, report_artifact_id, report_sha256, report_receipt_id, "
        "evidence_kind, measured_by, measured_at_us, capabilities, coverage, material, "
        "evidence_digest) VALUES "
        "('vce-job-evidence-validation', 'draft-evidence', '1', 'job-evidence-validation', "
        "'validation-1', 'voice-model-rev-1', 'report-1', %s, 'receipt-1', "
        "'validation_run', 'migration-test', 100, '{}'::jsonb, '{}'::jsonb, NULL, %s)",
        (digest, digest),
    )


def _seed_custom_publication(query: Connection[dict[str, Any]]) -> None:
    _seed_measured_evidence(query)
    query.execute(
        "INSERT INTO speech_publications (kind,object_id,revision,revision_id,published_at_us) "
        "VALUES ('voice','voice-custom','1','voice-custom-1',100)"
    )
    query.execute(
        f"INSERT INTO {_REVISIONS} "
        "(kind, voice_id, revision, revision_id, source_type, document, material, "
        "evidence_id, draft_id, draft_revision, published_at_us) "
        "VALUES ('voice','voice-custom','1','voice-custom-1','cloned','{}'::jsonb,NULL,"
        "'vce-job-evidence-validation','draft-evidence','1',100)"
    )


def test_downgrade_refuses_a_custom_publication() -> None:
    with _isolated_schema(_url()) as (config, query):
        command.upgrade(config, "head")
        _seed_custom_publication(query)
        with pytest.raises(RuntimeError, match="require preservation"):
            command.downgrade(config, "0046_public_job_intents")
        assert (
            _one(query, "SELECT version_num FROM alembic_version")["version_num"] == head_revision()
        )


def test_upgrade_creates_ledger_view_and_reruns_idempotently() -> None:
    with _isolated_schema(_url()) as (config, query):
        command.upgrade(config, "head")
        command.upgrade(config, "head")
        assert _one(query, f"SELECT to_regclass('{_EVIDENCE}') AS t")["t"]
        assert _one(query, f"SELECT to_regclass('{_REVISIONS}') AS t")["t"]
        assert _one(query, f"SELECT to_regclass('{_INTENTS}') AS t")["t"]
        unique = _one(
            query,
            "SELECT indexdef FROM pg_indexes WHERE schemaname=current_schema() AND indexname=%s",
            (_REVISIONS + "_draft_revision",),
        )
        assert unique is not None and "UNIQUE" in unique["indexdef"]
        view = _one(query, "SELECT pg_get_viewdef(%s::regclass) AS definition", (_VIEW,))[
            "definition"
        ]
        assert _REVISIONS in view and "speech_builtin_voice_qualifications" in view
        revision = _one(query, "SELECT version_num FROM alembic_version")["version_num"]
        assert revision == head_revision()


def test_downgrade_refuses_to_discard_evidence_then_restores_legacy_view() -> None:
    with _isolated_schema(_url()) as (config, query):
        command.upgrade(config, "head")
        _seed_measured_evidence(query)
        with pytest.raises(RuntimeError, match="require preservation"):
            command.downgrade(config, "0046_public_job_intents")
        assert (
            _one(query, "SELECT version_num FROM alembic_version")["version_num"] == head_revision()
        )
        # A schema with no custom publication or measurement may still roll back.
        query.execute(f"DELETE FROM {_EVIDENCE}")
        reader = "sn_view_" + uuid.uuid4().hex[:10]
        query.execute(f'CREATE ROLE "{reader}"')
        query.execute(f'GRANT SELECT ON {_VIEW} TO "{reader}"')
        command.downgrade(config, "0046_public_job_intents")
        assert (
            _one(query, "SELECT version_num FROM alembic_version")["version_num"]
            == "0046_public_job_intents"
        )
        assert _one(query, f"SELECT to_regclass('{_EVIDENCE}') AS t")["t"] is None
        view = _one(query, "SELECT pg_get_viewdef(%s::regclass) AS definition", (_VIEW,))[
            "definition"
        ]
        assert "speech_builtin_voice_qualifications" in view and _REVISIONS not in view
        assert (
            _one(
                query,
                "SELECT pg_catalog.has_table_privilege(%s, %s, 'SELECT') AS ok",
                (reader, _VIEW),
            )["ok"]
            is True
        )
        query.execute(f'DROP OWNED BY "{reader}"')
        query.execute(f'DROP ROLE "{reader}"')


def test_published_draft_projection_view_upgrades_and_drops_cleanly() -> None:
    """0053 only adds a projection: the facts behind it survive its downgrade."""
    with _isolated_schema(_url()) as (config, query):
        command.upgrade(config, "0052_quarantine_releases")
        assert _one(query, f"SELECT to_regclass('{_DRAFT_VIEW}') AS t")["t"] is None
        command.upgrade(config, "head")
        command.upgrade(config, "head")
        options = _one(
            query,
            "SELECT reloptions FROM pg_class WHERE relname=%s",
            (_DRAFT_VIEW,),
        )["reloptions"]
        assert set(options or ()) == {"security_barrier=true", "security_invoker=false"}
        columns = [
            row["column_name"]
            for row in query.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema=current_schema() AND table_name=%s "
                "ORDER BY ordinal_position",
                (_DRAFT_VIEW,),
            ).fetchall()
        ]
        assert columns == [
            "draft_id",
            "revision",
            "voice_id",
            "voice_revision",
            "validation_job_id",
        ]
        # A draft that was never published has no row; one publication yields one.
        assert _one(query, f"SELECT count(*) AS n FROM {_DRAFT_VIEW}")["n"] == 0
        _seed_custom_publication(query)
        assert _one(query, f"SELECT * FROM {_DRAFT_VIEW}") == {
            "draft_id": "draft-evidence",
            "revision": "1",
            "voice_id": "voice-custom",
            "voice_revision": "1",
            "validation_job_id": "job-evidence-validation",
        }
        command.downgrade(config, "0052_quarantine_releases")
        assert _one(query, f"SELECT to_regclass('{_DRAFT_VIEW}') AS t")["t"] is None
        assert _one(query, f"SELECT count(*) AS n FROM {_REVISIONS}")["n"] == 1
        command.upgrade(config, "head")
        assert _one(query, f"SELECT count(*) AS n FROM {_DRAFT_VIEW}")["n"] == 1
