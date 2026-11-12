"""The publication journal accepts its new action only through 0051."""

from __future__ import annotations

from typing import Any

import psycopg
import pytest
from alembic import command
from tests.integration.test_public_job_progress_migration_pg import _isolated_schema, _url

pytestmark = pytest.mark.integration

CONSTRAINT = "speech_publication_lifecycle_events_action_check"
EVENT = "speech_publication_lifecycle_events"
DIGEST = "sha256:" + "a" * 64


def _definition(query: psycopg.Connection[dict[str, Any]]) -> str:
    row = query.execute(
        "SELECT pg_get_constraintdef(oid) AS definition FROM pg_constraint "
        "WHERE conrelid='speech_publication_lifecycle_events'::regclass AND conname=%s",
        (CONSTRAINT,),
    ).fetchone()
    assert row is not None
    return str(row["definition"])


def _seed_publication(query: psycopg.Connection[dict[str, Any]]) -> None:
    query.execute(
        "INSERT INTO speech_publications "
        "(kind, object_id, revision, revision_id, published_at_us) "
        "VALUES ('binding','tts-quality','3','binding-rev-3',1700000000000003) "
        "ON CONFLICT (kind, object_id, revision) DO NOTHING"
    )


def _publish_event(query: psycopg.Connection[dict[str, Any]], action: str) -> None:
    query.execute(
        f"INSERT INTO {EVENT} "
        "(event_id, kind, action, binding_id, revision, revision_id, result_status, "
        " request_id, actor_type, occurred_at_us, policy_revision, intent_digest, "
        " change_summary_json) "
        "VALUES ('event-1','binding',%s,'tts-quality','3','binding-rev-3','published',"
        " 'request-1','anonymous',1700000000000003,1,%s,'{}'::jsonb)",
        (action, DIGEST),
    )


def test_0051_widens_the_legacy_check_and_refuses_to_erase_publication_events() -> None:
    with _isolated_schema(_url()) as (config, query):
        # Fresh schemas already carry the widened CHECK through LIFECYCLE_DDL;
        # the 0051 downgrade restores the legacy shape so the upgrade below
        # exercises exactly the statement a pre-existing database needs.
        command.upgrade(config, "head")
        assert "model_binding.publish" in _definition(query)
        _seed_publication(query)
        command.downgrade(config, "0050_profile_retirements")
        assert "model_binding.publish" not in _definition(query)
        with pytest.raises(psycopg.errors.CheckViolation):
            _publish_event(query, "model_binding.publish")

        command.upgrade(config, "head")
        assert "model_binding.publish" in _definition(query)
        _publish_event(query, "model_binding.publish")

        with pytest.raises(RuntimeError, match="require preservation"):
            command.downgrade(config, "0050_profile_retirements")
        assert query.execute("SELECT version_num FROM alembic_version").fetchone() == {
            "version_num": "0051_binding_publication_events"
        }

        query.execute(f"DELETE FROM {EVENT}")
        command.downgrade(config, "0050_profile_retirements")
        assert "model_binding.publish" not in _definition(query)
