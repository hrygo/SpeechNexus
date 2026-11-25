"""The publication journal accepts its new action only through 0051."""

from __future__ import annotations

from typing import Any

import psycopg
import pytest
from alembic import command
from tests.integration.test_public_job_progress_migration_pg import _isolated_schema, _url

pytestmark = pytest.mark.integration

REVISION = "0051_binding_publication_events"
_PREDECESSOR = "0050_profile_retirements"
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
        # The round trip belongs to 0051↔0050, the revision that rewrites the journal's
        # CHECK. Starting from head is refused by 0070's one-way step before 0051's own
        # downgrade could run; a schema built at 0051 exercises exactly the ALTER a
        # pre-existing database needs.
        command.upgrade(config, REVISION)
        assert "model_binding.publish" in _definition(query)
        _seed_publication(query)

        # 0051's downgrade restores the legacy shape a pre-existing database had.
        command.downgrade(config, _PREDECESSOR)
        assert "model_binding.publish" not in _definition(query)
        with pytest.raises(psycopg.errors.CheckViolation):
            _publish_event(query, "model_binding.publish")

        command.upgrade(config, REVISION)
        assert "model_binding.publish" in _definition(query)
        _publish_event(query, "model_binding.publish")

        # 0051's own refusal: a recorded publication is evidence, and the legacy CHECK
        # cannot hold its action.
        with pytest.raises(RuntimeError, match="require preservation"):
            command.downgrade(config, _PREDECESSOR)
        assert query.execute("SELECT version_num FROM alembic_version").fetchone() == {
            "version_num": REVISION
        }
        assert "model_binding.publish" in _definition(query)

        query.execute(f"DELETE FROM {EVENT}")
        command.downgrade(config, _PREDECESSOR)
        assert "model_binding.publish" not in _definition(query)
