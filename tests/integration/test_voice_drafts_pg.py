"""Real migrated PG voice draft/Job atomicity; only caller-supplied isolated DSNs."""

import json
import os
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from typing import Any, cast

import pytest
from alembic import command
from httpx import ASGITransport, AsyncClient
from tests.integration.test_public_job_progress_migration_pg import _isolated_schema
from tests.integration.test_public_jobs_http import JobSandbox, _seed_binding, _settings_for
from tests.integration.test_public_jobs_http import jobs_db as jobs_db
from tests.unit.backend.test_health import _settings

from speechnexus_backend.app import create_app
from speechnexus_backend.speech.application.public_jobs import (
    JobOutcomeUnknownError,
    JobRejectedError,
    PublicJobs,
)
from speechnexus_backend.speech.application.voice_drafts import VoiceDrafts
from speechnexus_backend.speech.domain.public_jobs import JobCreateResult, JobState, NewPublicJob
from speechnexus_backend.speech.infrastructure.postgres_pool import speech_pool
from speechnexus_backend.speech.infrastructure.public_jobs_postgres import PostgresPublicJobs
from speechnexus_backend.speech.infrastructure.voice_draft_schema import voice_draft_grants
from speechnexus_backend.speech.infrastructure.voice_drafts_postgres import PostgresVoiceDrafts

pytestmark = pytest.mark.integration


@pytest.fixture
def voice_db() -> Iterator[JobSandbox]:
    url = os.environ.get("SPEECHNEXUS_TEST_DATABASE_URL")
    if not url:
        pytest.fail("Configure an isolated PostgreSQL DSN; skip is not migration evidence")
    # This helper creates a random schema inside try and always DROP CASCADEs
    # it in finally, even when migration or a later assertion fails.
    with _isolated_schema(url) as (config, admin):
        command.upgrade(config, "head")
        schema = str(config.attributes["schema"])
        _seed_binding(admin)
        # New immutable publication rather than changing an existing binding.
        digest = "sha256:" + "b" * 64
        definition = json.dumps({"tasks": ["voice_design", "voice_clone"]})
        admin.execute(
            "INSERT INTO speech_binding_families (binding_id,latest_revision) "
            "VALUES ('voice-model','1')"
        )
        admin.execute(
            "INSERT INTO speech_binding_commands "
            "(key_digest,binding_id,document_version,intent_json,request_digest) "
            "VALUES (%s,'voice-model',1,'{}',%s)",
            (digest, digest),
        )
        admin.execute(
            "INSERT INTO speech_binding_revisions "
            "(binding_id,revision,revision_id,key_digest,document_version,definition_json,"
            "definition_digest,request_digest,created_at_us) "
            "VALUES ('voice-model','1','voice-model-rev-1',%s,1,%s,%s,%s,1)",
            (digest, definition, digest, digest),
        )
        admin.execute(
            "INSERT INTO speech_publications (kind,object_id,revision,revision_id,published_at_us) "
            "VALUES ('binding','voice-model','1','voice-model-rev-1',1)"
        )
        admin.execute(
            "INSERT INTO speech_publication_heads "
            "(kind,object_id,revision,revision_id,updated_at_us) "
            "VALUES ('binding','voice-model','1','voice-model-rev-1',1)"
        )
        admin.execute(
            "INSERT INTO speech_assets (asset_id,workspace_id,purpose,asset_state,upload_phase,"
            "bytes,sha256,request_digest,object_key,media_type,duration_ms,sample_rate_hz,channels,"
            "created_at_us,expires_at_us) VALUES ('synthetic-reference','workspace-default',"
            "'voice_reference','ready','settled',64,%s,%s,'synthetic-reference','audio/wav',"
            "10,16000,1,1,253402300799999999)",
            (digest, digest),
        )
        with speech_pool(url, schema=schema, name="voice-drafts-test") as pool:
            adapter = PostgresPublicJobs(pool)
            yield JobSandbox(url, schema, "", admin, PublicJobs(adapter, adapter), pool)


def _provider(db: JobSandbox) -> VoiceDrafts:
    return VoiceDrafts(db.jobs, PostgresVoiceDrafts(db.pool))


def _body(kind: str) -> dict[str, Any]:
    return {
        "model": "voice-model",
        "input": (
            {"description": "Synthetic voice", "preview_text": "Synthetic preview"}
            if kind == "design"
            else {"reference_audio_asset_id": "synthetic-reference", "source_use_confirmed": True}
        ),
    }


@pytest.mark.parametrize("kind", ["design", "clone"])
async def test_create_replay_read_metadata_and_generation_lifecycle(
    voice_db: JobSandbox, kind: str
) -> None:
    db = voice_db
    app = create_app(settings=_settings(), jobs=db.jobs, voice_drafts=_provider(db))
    async with AsyncClient(transport=ASGITransport(app), base_url="http://localhost") as http:
        path = "/v1/nexus/voice-" + ("designs" if kind == "design" else "clones")
        headers = {"Idempotency-Key": "voice-generation-key"}
        first = await http.post(path, json=_body(kind), headers=headers)
        assert first.status_code == 202, first.text
        body = first.json()
        draft_id, job_id = body["draft"]["id"], body["job"]["id"]
        assert body["draft"]["status"] == "generating"
        assert (await http.post(path, json=_body(kind), headers=headers)).json() == body
        for table in (
            "speech_voice_drafts",
            "speech_jobs",
            "coordination_jobs",
            "coordination_outbox",
        ):
            assert db.count(table) == 1
        read_path = "/v1/nexus/voice-drafts/" + draft_id
        read = (await http.get(read_path)).json()
        assert read["revision"]["generation_job_id"] == job_id
        assert read["revision"]["model_binding"] == {"id": "voice-model", "revision": "1"}
        for forbidden in ("base64", "feature_vector", "object_key", "internal_model_path"):
            assert forbidden not in json.dumps(read)
        patch = {"expected_metadata_revision": 1, "display_name": "Synthetic renamed"}
        patch_headers = {"Idempotency-Key": "voice-metadata-key"}
        updated = await http.patch(read_path, json=patch, headers=patch_headers)
        assert updated.status_code == 200, updated.text
        assert updated.json()["metadata_revision"] == 2
        assert (
            await http.patch(read_path, json=patch, headers=patch_headers)
        ).json() == updated.json()
        assert (await http.patch(read_path, json=patch)).status_code == 409
        assert (
            await http.patch(read_path, json={**patch, "status": "published"})
        ).status_code == 422
        assert (await http.get("/v1/nexus/voice-drafts/missing-draft")).status_code == 404
        assert (await http.get(read_path, params={"draft_revision": "1"})).status_code == 200
        assert (await http.get(read_path, params={"draft_revision": "9"})).status_code == 404
        job = db.jobs.get(job_id)
        db.jobs.advance(job_id, "running")
        db.jobs.advance(job_id, "failed", error_code="synthetic_generation_failure")
        failed = await http.get(read_path)
        assert failed.status_code == 200, failed.text
        assert failed.json()["draft"]["status"] == "failed"
        assert failed.json()["revision"]["failure"] == {
            "code": "synthetic_generation_failure",
            "message": "The generation job failed.",
            "retryable": False,
        }
        assert failed.json()["revision"]["input"] == read["revision"]["input"]
        assert job.submission.execution_revision == 1
        db.query("UPDATE speech_voice_drafts SET status='published' WHERE draft_id=%s", (draft_id,))
        assert (
            await http.patch(read_path, json={**patch, "expected_metadata_revision": 2})
        ).status_code == 409
        assert (
            await http.patch(read_path, json=patch, headers=patch_headers)
        ).json() == updated.json()


def test_concurrent_same_key_commits_one_draft_and_outbox(voice_db: JobSandbox) -> None:
    def submit(_: int) -> tuple[str, str]:
        draft, job = _provider(voice_db).create(
            "voice_design",
            _body("design"),
            request_id="request-concurrent",
            idempotency_key="concurrent-voice-key",
        )
        return draft["id"], job.job_id

    with ThreadPoolExecutor(max_workers=2) as workers:
        results = list(workers.map(submit, range(2)))
    assert results[0] == results[1]
    assert voice_db.count("speech_voice_drafts") == voice_db.count("coordination_outbox") == 1


def test_draft_insert_failure_rolls_back_existing_job_transaction(voice_db: JobSandbox) -> None:
    voice_db.query(
        "ALTER TABLE speech_voice_drafts ADD CONSTRAINT synthetic_rejection CHECK (false)"
    )
    with pytest.raises(JobRejectedError):
        _provider(voice_db).create(
            "voice_design", _body("design"), request_id="request-rollback", idempotency_key=None
        )
    for table in ("speech_voice_drafts", "speech_jobs", "speech_job_events", "coordination_outbox"):
        assert voice_db.count(table) == 0


async def test_production_lifespan_installs_voice_provider(jobs_db: JobSandbox) -> None:
    for grant in voice_draft_grants(jobs_db.schema, jobs_db.role):
        jobs_db.admin.execute(grant)
    app = create_app(settings=_settings_for(jobs_db))
    async with app.router.lifespan_context(app):
        assert app.state.job_runtime.voice_drafts is not None
        async with AsyncClient(transport=ASGITransport(app), base_url="http://localhost") as http:
            response = await http.get("/v1/nexus/voice-drafts/missing-draft")
        assert response.status_code == 404
    assert app.state.job_runtime.voice_drafts is None


def test_lost_create_ack_recovers_original_committed_draft(
    voice_db: JobSandbox, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = PostgresPublicJobs(voice_db.pool)
    original_create = adapter.create

    def lose_ack(job: NewPublicJob) -> JobCreateResult:
        result = original_create(job)
        assert result.outcome == "created"  # Actual PG transaction already committed.
        return JobCreateResult("unknown", error_code="outcome_unknown")

    monkeypatch.setattr(adapter, "create", lose_ack)
    provider = VoiceDrafts(PublicJobs(adapter, adapter), PostgresVoiceDrafts(voice_db.pool))
    with pytest.raises(JobOutcomeUnknownError):
        provider.create(
            "voice_design",
            _body("design"),
            request_id="request-lost-ack",
            idempotency_key="voice-lost-ack-key",
        )
    recovered, job = _provider(voice_db).create(
        "voice_design",
        _body("design"),
        request_id="request-recovered",
        idempotency_key="voice-lost-ack-key",
    )
    assert recovered["id"] == "draft-" + job.job_id
    assert job.submission.request_id == "request-lost-ack"
    assert voice_db.count("speech_voice_drafts") == voice_db.count("coordination_outbox") == 1


@pytest.mark.parametrize("kind", ["design", "clone"])
def test_committed_generation_result_advances_only_matching_candidate(
    voice_db: JobSandbox, kind: str
) -> None:
    provider = _provider(voice_db)
    draft, job = provider.create(
        "voice_design" if kind == "design" else "voice_clone",
        _body(kind),
        request_id="request-candidate",
        idempotency_key=None,
    )
    voice_db.jobs.advance(job.job_id, "running")
    # Synthetic worker publication facts qualify the projection, not a real engine.
    voice_db.jobs.advance(
        job.job_id,
        "succeeded",
        result_json=json.dumps(
            {
                "kind": "voice_" + kind,
                "voice_draft_id": draft["id"],
                "draft_revision": "1",
                "preview_artifact_ids": ["preview-synthetic"],
                "receipt_id": "receipt-synthetic",
            }
        ),
    )
    view = provider.get(draft["id"])
    assert view["draft"]["status"] == view["revision"]["status"] == "candidate"
    assert view["revision"]["preview_artifact_ids"] == ["preview-synthetic"]


@pytest.mark.parametrize("kind", ["design", "clone"])
async def test_creation_rejects_undeclared_language_before_persistence(
    voice_db: JobSandbox, kind: str
) -> None:
    db = voice_db
    app = create_app(settings=_settings(), jobs=db.jobs, voice_drafts=_provider(db))
    path = "/v1/nexus/voice-" + ("designs" if kind == "design" else "clones")
    async with AsyncClient(transport=ASGITransport(app), base_url="http://localhost") as http:
        for language in ("EN-US", "english", "x!"):
            body = _body(kind)
            body["input"]["language"] = language
            response = await http.post(path, json=body)
            assert response.status_code == 400, response.text
            assert response.json()["error"]["code"] == "voice_draft_invalid_request"
        assert db.count("speech_voice_drafts") == 0
        assert db.count("speech_jobs") == 0
        accepted = _body(kind)
        accepted["input"]["language"] = "zh-CN"
        assert (await http.post(path, json=accepted)).status_code == 202


@pytest.mark.parametrize("state", ["cancelled", "expired"])
async def test_cancelled_and_expired_generations_name_their_failure(
    voice_db: JobSandbox, state: str
) -> None:
    db = voice_db
    app = create_app(settings=_settings(), jobs=db.jobs, voice_drafts=_provider(db))
    async with AsyncClient(transport=ASGITransport(app), base_url="http://localhost") as http:
        created = await http.post("/v1/nexus/voice-designs", json=_body("design"))
        assert created.status_code == 202, created.text
        job_id = created.json()["job"]["id"]
        if state == "cancelled":
            db.jobs.advance(job_id, "running")
        db.jobs.advance(job_id, cast(JobState, state))
        read = await http.get("/v1/nexus/voice-drafts/" + created.json()["draft"]["id"])
    assert read.status_code == 200, read.text
    assert read.json()["draft"]["status"] == "failed"
    assert read.json()["revision"]["failure"]["code"] == "voice_generation_" + state
