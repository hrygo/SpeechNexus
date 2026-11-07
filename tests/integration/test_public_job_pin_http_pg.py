"""Public pinned Jobs use the production app and isolated PostgreSQL roles."""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from psycopg.conninfo import make_conninfo
from pydantic import AnyHttpUrl, SecretStr
from tests.integration import test_production_admission as _admission_module
from tests.integration.test_coordination_foundation import (  # noqa: F401 -- pytest fixture
    foundation,
)
from tests.integration.test_production_admission import AdmissionDatabase
from tests.integration.test_production_jobs import database  # noqa: F401 -- pytest fixture
from tests.integration.test_production_runtime_intents import (  # noqa: F401 -- pytest fixture
    runtime_database,
)
from tests.integration.test_public_jobs_http import PASSWORD, PREFIX, _body

from speechnexus_backend.app import create_app
from speechnexus_backend.settings import Settings
from speechnexus_backend.speech.domain.publications import Publication
from speechnexus_backend.speech.infrastructure.job_cancellation_fence_schema import (
    cancellation_reconciliation_grants,
)
from speechnexus_backend.speech.infrastructure.job_schema import (
    job_dispatch_grants,
    job_runtime_grants,
)
from speechnexus_backend.speech.infrastructure.public_job_admission_grants import (
    public_job_admission_grants,
)
from speechnexus_backend.speech.infrastructure.public_job_schema import public_job_grants
from speechnexus_backend.speech.infrastructure.public_job_submission_schema import (
    public_job_submission_grants,
)
from speechnexus_backend.speech.infrastructure.publications_sql import append_publication
from speechnexus_backend.speech.infrastructure.voice_draft_schema import voice_draft_grants

admission_database = _admission_module.admission_database

pytestmark = pytest.mark.integration

DIGEST = "sha256:" + "d" * 64
WORKSPACE = "workspace-test"
TTS = {
    "language": "en",
    "text_commit_mode": "explicit",
    "audio_delivery": "complete",
    "speed": 1.0,
    "instructions": None,
    "pronunciation_revision": None,
    "seed": 7,
    "planner": {"max_chars": 400, "max_wait_ms": 1000},
    "sample_rate_hz": 24_000,
    "max_output_audio_ms": 120_000,
    "timeout_seconds": 60,
}


def _seed(db: Any) -> None:
    definition = {
        "binding_id": "tts-quality",
        "logical_model": "narrator-tts",
        "tasks": ["tts_file"],
        "release_tier": "production",
        "execution_recipe": {
            "recipe_revision": "1",
            "capacity_manifest_id": "manifest-test",
            "stage_roles": ["tts_talker"],
        },
        "execution_defaults": {"tts": TTS},
    }
    db.query(
        "INSERT INTO speech_workspaces "
        "(workspace_id, display_name, workspace_state, created_at_us) "
        "VALUES (%s, 'test', 'active', 1) ON CONFLICT DO NOTHING",
        (WORKSPACE,),
    )
    db.query(
        "INSERT INTO speech_binding_families (binding_id, latest_revision) "
        "VALUES ('tts-quality', '2')"
    )
    db.query(
        "INSERT INTO speech_binding_commands "
        "(key_digest, binding_id, document_version, intent_json, request_digest) "
        "VALUES (%s, 'tts-quality', 1, '{}', %s)",
        (DIGEST, DIGEST),
    )
    db.query(
        "INSERT INTO speech_binding_revisions "
        "(binding_id, revision, revision_id, key_digest, document_version, definition_json, "
        "definition_digest, request_digest, created_at_us) "
        "VALUES ('tts-quality', '2', 'binding-test', %s, 1, %s, %s, %s, 2)",
        (DIGEST, json.dumps(definition), DIGEST, DIGEST),
    )
    db.query(
        "INSERT INTO speech_publications "
        "(kind, object_id, revision, revision_id, published_at_us) "
        "VALUES ('binding', 'tts-quality', '1', 'binding-rev-1', 1)"
    )
    catalog = db.query("SELECT revision FROM speech_catalog_state")[0]["revision"]
    with db.admin.transaction(), db.admin.cursor() as cursor:
        assert append_publication(
            cursor,
            Publication("binding", "tts-quality", 2, "binding-test", 2),
            expected_current_revision=None,
            expected_catalog_revision=int(catalog),
        )
        assert append_publication(
            cursor,
            Publication("voice", "voice-vivian", 2, "voice-rev-2", 2),
            expected_current_revision=None,
            expected_catalog_revision=int(catalog) + 1,
        )


def _head(db: Any, revision: str | None) -> None:
    if revision is None:
        db.query("DELETE FROM speech_publication_heads WHERE kind='binding'")
    else:
        db.query(
            "UPDATE speech_publication_heads SET revision=%s, revision_id=%s "
            "WHERE kind='binding' AND object_id='tts-quality'",
            (revision, "binding-test" if revision == "2" else "binding-rev-" + revision),
        )


def _payload(revision: str = "2") -> dict[str, Any]:
    body = _body(model="narrator-tts", model_binding={"id": "tts-quality", "revision": revision})
    body["workspace_id"] = WORKSPACE
    return body


def _roles(db: Any) -> tuple[str, str, str, str]:
    writer = "pin_writer_" + uuid.uuid4().hex[:10]
    admission = "pin_admission_" + uuid.uuid4().hex[:10]
    db.query(f"CREATE ROLE \"{writer}\" LOGIN PASSWORD '{PASSWORD}'")
    db.query(f"CREATE ROLE \"{admission}\" LOGIN PASSWORD '{PASSWORD}'")
    for grant in (
        *job_runtime_grants(db.schema, writer),
        *job_dispatch_grants(db.schema, writer),
        *public_job_grants(db.schema, writer),
        *voice_draft_grants(db.schema, writer),
        *public_job_submission_grants(db.schema, writer),
        *cancellation_reconciliation_grants(db.schema, writer),
        f'GRANT SELECT ON "{db.schema}".speech_workspaces, '
        f'"{db.schema}".speech_publications, "{db.schema}".speech_publication_heads, '
        f'"{db.schema}".speech_catalog_state, "{db.schema}".speech_publication_retirements, '
        f'"{db.schema}".speech_binding_revisions, "{db.schema}".speech_assets, '
        f'"{db.schema}".speech_pronunciation_revisions TO "{writer}"',
    ):
        db.query(grant)
    for grant in public_job_admission_grants(db.schema, admission):
        db.query(grant)
    return writer, admission, writer, admission


def _settings_for(
    writer: str, admission: str, schema: str, monkeypatch: pytest.MonkeyPatch
) -> Settings:
    # A settings fixture must not inherit runtime endpoints from the host or .env.
    for name in tuple(os.environ):
        if name.upper().startswith("SPEECHNEXUS_"):
            monkeypatch.delenv(name)
    return Settings(
        _env_file=None,
        environment="test",
        service_name="speechnexus-api-test",
        process_role="api",
        database_url=SecretStr("postgresql://unused@127.0.0.1/unused"),
        object_storage_endpoint=AnyHttpUrl("http://127.0.0.1:9000"),
        object_storage_healthcheck_url=AnyHttpUrl("http://127.0.0.1:9000/health"),
        object_storage_bucket="test",
        database_schema=schema,
        job_writer_database_url=SecretStr(writer),
        job_admission_database_url=SecretStr(admission),
    )


def test_isolated_settings_ignore_environment_and_dotenv(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    writer = "postgresql://synthetic@127.0.0.1/synthetic"
    admission = "postgresql://admission@127.0.0.1/synthetic"
    expected = _settings_for(writer, admission, "sn_isolation_test", monkeypatch)
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text(
        "SPEECHNEXUS_PROCESS_ROLE=job-worker\n"
        "SPEECHNEXUS_BINDING_LIFECYCLE_DATABASE_URL=postgresql://unused@127.0.0.1/unused\n"
        "SPEECHNEXUS_EXECUTION_GATEWAY_BASE_URL=http://127.0.0.1:33333\n"
        "SPEECHNEXUS_EXECUTION_GATEWAY_COMPONENT_CREDENTIAL=synthetic\n"
    )
    monkeypatch.setenv("SPEECHNEXUS_PROCESS_ROLE", "scheduler")
    monkeypatch.setenv("SPEECHNEXUS_JOB_WRITER_DATABASE_URL", "postgresql://unused/other")
    monkeypatch.setenv("speechnexus_profile_writer_database_url", "postgresql://unused/other")
    # Check the actual source precedence before demonstrating the isolated setup.
    unisolated = Settings(
        database_url=SecretStr("postgresql://unused@127.0.0.1/unused"),
        object_storage_endpoint=AnyHttpUrl("http://127.0.0.1:9000"),
        object_storage_healthcheck_url=AnyHttpUrl("http://127.0.0.1:9000/health"),
        object_storage_bucket="test",
        job_writer_database_url=SecretStr(writer),
    )
    assert unisolated.process_role == "scheduler"  # environment overrides .env
    assert unisolated.job_writer_database_url is not None
    assert unisolated.job_writer_database_url.get_secret_value() == writer  # init overrides env
    assert unisolated.profile_writer_database_url is not None  # case-insensitive env prefix
    assert unisolated.binding_lifecycle_database_url is not None  # loaded from .env
    assert unisolated.execution_gateway_base_url is not None
    configured = _settings_for(writer, admission, "sn_isolation_test", monkeypatch)

    assert configured == expected
    assert configured.process_role == "api"
    assert configured.job_writer_database_url is not None
    assert configured.job_writer_database_url.get_secret_value() == writer
    assert configured.job_admission_database_url is not None
    assert configured.job_admission_database_url.get_secret_value() == admission
    assert configured.profile_writer_database_url is None
    assert configured.binding_lifecycle_database_url is None
    assert configured.execution_gateway_base_url is None

    # App construction does not open its lifespan or connect to the synthetic DSN.
    app = create_app(settings=configured)
    assert app.state.settings is configured
    assert app.state.job_runtime is not None
    for name in (
        "profile_runtime",
        "pronunciation_runtime",
        "catalogue_runtime",
        "binding_runtime",
        "binding_defaults_runtime",
        "binding_admin_runtime",
        "artifact_runtime",
        "speech",
        "transcriptions",
    ):
        assert getattr(app.state, name) is None, name


@pytest.mark.parametrize("database", ["postgresql"], indirect=True)
async def test_pinned_job_replays_original_after_binding_head_changes(
    admission_database: AdmissionDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = admission_database.runtime.foundation.database
    _seed(db)
    writer, admission, writer_role, admission_role = _roles(db)
    try:
        app = create_app(
            settings=_settings_for(
                make_conninfo(db.url, user=writer, password=PASSWORD),
                make_conninfo(db.url, user=admission, password=PASSWORD),
                db.schema,
                monkeypatch,
            )
        )
        payload = _payload("2")
        headers = {"Idempotency-Key": "synthetic-pinned-job-http"}
        tables = (
            "speech_jobs",
            "speech_job_events",
            "speech_job_submissions",
            "speech_job_intents",
            "admission_operations",
            "coordination_jobs",
            "coordination_outbox",
        )
        before = {table: db.query(f"SELECT count(*) AS n FROM {table}")[0]["n"] for table in tables}
        async with (
            app.router.lifespan_context(app),
            AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
        ):
            created = await client.post(PREFIX, json=payload, headers=headers)
            assert created.status_code == 202, created.text
            original = created.json()
            job_id = original["id"]
            assert original["object"] == "nexus.job" and original["status"] == "queued"
            assert original["effective_configuration"]["models"]["tts"] == {
                "id": "tts-quality",
                "revision": "2",
            }
            assert created.headers["Location"] == PREFIX + "/" + job_id
            assert {
                table: db.query(f"SELECT count(*) AS n FROM {table}")[0]["n"] for table in tables
            } == {table: before[table] + 1 for table in tables}
            submission = db.query(
                "SELECT effective_configuration_json FROM speech_job_submissions WHERE job_id=%s",
                (job_id,),
            )
            assert len(submission) == 1
            assert (
                json.loads(submission[0]["effective_configuration_json"])
                == original["effective_configuration"]
            )
            command = db.query("SELECT request_document FROM admission_operations")
            assert len(command) == 1
            assert json.loads(command[0]["request_document"])["model_binding"] == {
                "id": "tts-quality",
                "revision": "2",
            }

            # Change only this test's synthetic head; replay must recover the committed Job.
            _head(db, "1")
            replay = await client.post(PREFIX, json=payload, headers=headers)
            assert replay.status_code == 202, replay.text
            assert replay.json() == original
            assert replay.headers["Location"] == created.headers["Location"]

            stale = await client.post(
                PREFIX, json=payload, headers={"Idempotency-Key": "synthetic-pinned-job-new"}
            )
            assert stale.status_code == 422, stale.text
            assert stale.json()["error"]["code"] == "model_binding_not_current"
            conflict = await client.post(PREFIX, json=_payload("1"), headers=headers)
            assert conflict.status_code == 409, conflict.text
            assert conflict.json()["error"]["code"] == "idempotency_conflict"

            read = await client.get(PREFIX + "/" + job_id)
            assert read.status_code == 200, read.text
            assert read.json() == original
            assert {
                table: db.query(f"SELECT count(*) AS n FROM {table}")[0]["n"] for table in tables
            } == {table: before[table] + 1 for table in tables}
    finally:
        for role in (writer_role, admission_role):
            db.query(f'DROP OWNED BY "{role}"')
            db.query(f'DROP ROLE IF EXISTS "{role}"')
