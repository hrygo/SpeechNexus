"""Public pinned Jobs use the production app and an isolated PostgreSQL writer."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from psycopg.conninfo import make_conninfo
from pydantic import AnyHttpUrl, SecretStr
from tests.integration.test_public_job_binding_pin_pg import _head, _payload, _seed
from tests.integration.test_public_jobs_http import PASSWORD, PREFIX, JobSandbox
from tests.integration.test_public_jobs_http import jobs_db as jobs_db

from speechnexus_backend.app import create_app
from speechnexus_backend.settings import Settings

pytestmark = pytest.mark.integration


def _isolated_settings(writer: str, schema: str, monkeypatch: pytest.MonkeyPatch) -> Settings:
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
    )


def test_isolated_settings_ignore_environment_and_dotenv(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    writer = "postgresql://synthetic@127.0.0.1/synthetic"
    expected = _isolated_settings(writer, "sn_isolation_test", monkeypatch)
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
    configured = _isolated_settings(writer, "sn_isolation_test", monkeypatch)

    assert configured == expected
    assert configured.process_role == "api"
    assert configured.job_writer_database_url is not None
    assert configured.job_writer_database_url.get_secret_value() == writer
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


async def test_pinned_job_replays_original_after_binding_head_changes(
    jobs_db: JobSandbox, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed(jobs_db)
    writer = make_conninfo(jobs_db.url, user=jobs_db.role, password=PASSWORD)
    app = create_app(settings=_isolated_settings(writer, jobs_db.schema, monkeypatch))
    payload = _payload("2")
    headers = {"Idempotency-Key": "synthetic-pinned-job-http"}
    tables = (
        "speech_jobs",
        "speech_job_events",
        "speech_job_submissions",
        "admission_operations",
        "coordination_jobs",
        "coordination_outbox",
    )
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
        assert {table: jobs_db.count(table) for table in tables} == dict.fromkeys(tables, 1)
        submission = jobs_db.query(
            "SELECT effective_configuration_json FROM speech_job_submissions WHERE job_id=%s",
            (job_id,),
        )
        assert len(submission) == 1
        assert (
            json.loads(submission[0]["effective_configuration_json"])
            == original["effective_configuration"]
        )
        command = jobs_db.query("SELECT request_document FROM admission_operations")
        assert len(command) == 1
        assert json.loads(command[0]["request_document"])["model_binding"] == {
            "id": "tts-quality",
            "revision": "2",
        }

        # Change only this test's synthetic head; replay must recover the committed Job.
        _head(jobs_db, "1")
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
        assert {table: jobs_db.count(table) for table in tables} == dict.fromkeys(tables, 1)
