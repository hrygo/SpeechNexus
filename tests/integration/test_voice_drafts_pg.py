"""Real migrated PG voice draft/Job atomicity; only caller-supplied isolated DSNs."""

import json
import os
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from typing import Any, cast

import pytest
from alembic import command
from httpx import ASGITransport, AsyncClient
from speechnexus_contracts import parse as parse_contract
from speechnexus_contracts.voice_drafts_http import validate_response
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
from speechnexus_backend.speech.infrastructure.job_dispatch_postgres import PostgresDispatchQueue
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
        asr_digest = "sha256:" + "e" * 64
        admin.execute(
            "INSERT INTO speech_binding_families (binding_id,latest_revision) "
            "VALUES ('asr-model','1')"
        )
        admin.execute(
            "INSERT INTO speech_binding_commands "
            "(key_digest,binding_id,document_version,intent_json,request_digest) "
            "VALUES (%s,'asr-model',1,'{}',%s)",
            (asr_digest, asr_digest),
        )
        admin.execute(
            "INSERT INTO speech_binding_revisions "
            "(binding_id,revision,revision_id,key_digest,document_version,definition_json,"
            "definition_digest,request_digest,created_at_us) "
            "VALUES ('asr-model','1','asr-model-rev-1',%s,1,%s,%s,%s,1)",
            (
                asr_digest,
                json.dumps({"tasks": ["asr_file"]}),
                asr_digest,
                asr_digest,
            ),
        )
        admin.execute(
            "INSERT INTO speech_publications (kind,object_id,revision,revision_id,published_at_us) "
            "VALUES ('binding','asr-model','1','asr-model-rev-1',1)"
        )
        admin.execute(
            "INSERT INTO speech_publication_heads "
            "(kind,object_id,revision,revision_id,updated_at_us) "
            "VALUES ('binding','asr-model','1','asr-model-rev-1',1)"
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
        # rev2 does not exist; an exact selector must 404 instead of echoing rev1.
        assert (await http.get(read_path, params={"draft_revision": "2"})).status_code == 404
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
        validate_response("getVoiceDraft", 200, {"body": failed.json()})
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


def _seed_preview_artifact(
    db: JobSandbox,
    artifact_id: str = "preview-synthetic",
    *,
    receipt: str = "receipt-synthetic",
    state: str = "ready",
) -> None:
    db.admin.execute(
        "INSERT INTO speech_artifacts "
        "(artifact_id, workspace_id, artifact_kind, artifact_state, media_type, bytes, "
        "sha256, receipt_id, source_operation_id, object_key, storage_version_id, created_at_us) "
        "VALUES (%s, 'workspace-default', 'preview', %s, 'audio/wav', 128, %s, %s, NULL, %s, "
        "'synthetic-version', 10)",
        (
            artifact_id,
            state,
            "sha256:" + "d" * 64,
            receipt,
            "private/artifacts/workspace-default/" + artifact_id,
        ),
    )


def _candidate_result(
    draft_id: str,
    kind: str,
    *,
    receipt: str | None = "receipt-synthetic",
    previews: list[str] | None = None,
) -> str:
    result: dict[str, Any] = {
        "kind": "voice_" + kind,
        "voice_draft_id": draft_id,
        "draft_revision": "1",
        "preview_artifact_ids": ["preview-synthetic"] if previews is None else previews,
    }
    if receipt is not None:
        result["receipt_id"] = receipt
    return json.dumps(result)


@pytest.mark.parametrize("kind", ["design", "clone"])
async def test_committed_ready_preview_advances_to_candidate(
    voice_db: JobSandbox, kind: str
) -> None:
    provider = _provider(voice_db)
    draft, job = provider.create(
        "voice_design" if kind == "design" else "voice_clone",
        _body(kind),
        request_id="request-candidate",
        idempotency_key=None,
    )
    _seed_preview_artifact(voice_db)
    voice_db.jobs.advance(job.job_id, "running")
    # Synthetic worker publication facts qualify the projection, not a real engine.
    voice_db.jobs.advance(
        job.job_id,
        "succeeded",
        result_json=_candidate_result(draft["id"], kind),
    )
    app = create_app(settings=_settings(), jobs=voice_db.jobs, voice_drafts=provider)
    async with AsyncClient(transport=ASGITransport(app), base_url="http://localhost") as http:
        read = await http.get("/v1/nexus/voice-drafts/" + draft["id"])
    assert read.status_code == 200, read.text
    assert read.json()["draft"]["status"] == read.json()["revision"]["status"] == "candidate"
    assert read.json()["revision"]["preview_artifact_ids"] == ["preview-synthetic"]
    validate_response("getVoiceDraft", 200, {"body": read.json()})


@pytest.mark.parametrize(
    ("mode", "receipt", "seed_receipt", "seed_state", "seed_artifact"),
    [
        ("missing-receipt", None, "receipt-synthetic", "ready", True),
        ("missing-artifact", "receipt-synthetic", "receipt-synthetic", "ready", False),
        ("wrong-receipt", "receipt-synthetic", "receipt-other", "ready", True),
        ("not-ready", "receipt-synthetic", "receipt-synthetic", "expired", True),
    ],
)
async def test_candidate_requires_a_matching_receipt_and_ready_artifact(
    voice_db: JobSandbox,
    mode: str,
    receipt: str | None,
    seed_receipt: str,
    seed_state: str,
    seed_artifact: bool,
) -> None:
    provider = _provider(voice_db)
    draft, job = provider.create(
        "voice_design",
        _body("design"),
        request_id="request-candidate-" + mode,
        idempotency_key=None,
    )
    if seed_artifact:
        _seed_preview_artifact(voice_db, receipt=seed_receipt, state=seed_state)
    voice_db.jobs.advance(job.job_id, "running")
    voice_db.jobs.advance(
        job.job_id,
        "succeeded",
        result_json=_candidate_result(draft["id"], "design", receipt=receipt),
    )
    app = create_app(settings=_settings(), jobs=voice_db.jobs, voice_drafts=provider)
    async with AsyncClient(transport=ASGITransport(app), base_url="http://localhost") as http:
        read = await http.get("/v1/nexus/voice-drafts/" + draft["id"])
    assert read.status_code == 503, (mode, read.text)
    assert read.json()["error"]["code"] == "voice_draft_unavailable"
    assert read.json()["error"]["retryable"] is True
    validate_response("getVoiceDraft", 503, {"body": read.json(), "headers": dict(read.headers)})


@pytest.mark.parametrize(("seed_artifact", "expected"), [(True, 200), (False, 503)])
async def test_candidate_validation_is_shared_by_get_and_patch(
    voice_db: JobSandbox, seed_artifact: bool, expected: int
) -> None:
    provider = _provider(voice_db)
    draft, job = provider.create(
        "voice_design",
        _body("design"),
        request_id="request-candidate-patch",
        idempotency_key=None,
    )
    if seed_artifact:
        _seed_preview_artifact(voice_db)
    voice_db.jobs.advance(job.job_id, "running")
    voice_db.jobs.advance(
        job.job_id,
        "succeeded",
        result_json=_candidate_result(draft["id"], "design"),
    )
    app = create_app(settings=_settings(), jobs=voice_db.jobs, voice_drafts=provider)
    path = "/v1/nexus/voice-drafts/" + draft["id"]
    async with AsyncClient(transport=ASGITransport(app), base_url="http://localhost") as http:
        read = await http.get(path)
        patch = await http.patch(
            path,
            json={"expected_metadata_revision": 1, "display_name": "Candidate voice"},
        )
    assert read.status_code == patch.status_code == expected, (read.text, patch.text)
    if expected == 200:
        assert read.json()["draft"]["status"] == "candidate"
        assert patch.json()["status"] == "candidate"
        assert patch.json()["head_revision"] == "1"
        assert read.json()["revision"]["preview_artifact_ids"] == ["preview-synthetic"]
        validate_response("getVoiceDraft", 200, {"body": read.json()})
        validate_response("updateVoiceDraftMetadata", 200, {"body": patch.json()})
    else:
        for body in (read.json(), patch.json()):
            assert body["error"]["code"] == "voice_draft_unavailable"
            assert body["error"]["retryable"] is True
        validate_response(
            "getVoiceDraft", 503, {"body": read.json(), "headers": dict(read.headers)}
        )
        validate_response(
            "updateVoiceDraftMetadata",
            503,
            {"body": patch.json(), "headers": dict(patch.headers)},
        )


@pytest.mark.parametrize(
    ("reference_text", "preview_text"),
    [
        ("中" * 10000, "文" * 2000),
        ("😀" * 10000, "🎤" * 2000),
    ],
)
async def test_utf8_voice_input_at_the_storage_boundary(
    voice_db: JobSandbox, reference_text: str, preview_text: str
) -> None:
    db = voice_db
    app = create_app(settings=_settings(), jobs=db.jobs, voice_drafts=_provider(db))
    body = {
        "model": "voice-model",
        "input": {
            "reference_audio_asset_id": "synthetic-reference",
            "source_use_confirmed": True,
            "reference_text": reference_text,
            "preview_text": preview_text,
        },
    }
    async with AsyncClient(transport=ASGITransport(app), base_url="http://localhost") as http:
        created = await http.post("/v1/nexus/voice-clones", json=body)
        assert created.status_code == 202, created.text
        validate_response(
            "createVoiceClone",
            202,
            {"body": created.json(), "headers": dict(created.headers)},
        )
        read = await http.get("/v1/nexus/voice-drafts/" + created.json()["draft"]["id"])
    assert read.status_code == 200, read.text
    assert read.json()["revision"]["input"]["reference_text"] == reference_text
    assert read.json()["revision"]["input"]["preview_text"] == preview_text
    stored = db.query("SELECT OCTET_LENGTH(input_json) AS n FROM speech_voice_drafts")[0]
    assert 36000 < int(stored["n"]) <= 65536


async def test_mixed_voice_asr_tts_batch_does_not_poison_supported_claims(
    voice_db: JobSandbox,
) -> None:
    db = voice_db
    tts = db.jobs.submit(
        {
            "kind": "tts",
            "model": "tts-quality",
            "input": {"text": "synthetic"},
            "voice": {"id": "voice-vivian", "revision": "2"},
        },
        request_id="request-mixed-tts",
        idempotency_key=None,
    )
    asr = db.jobs.submit(
        {"kind": "asr", "model": "asr-model", "input": {}},
        request_id="request-mixed-asr",
        idempotency_key=None,
    )
    _draft, voice = _provider(db).create(
        "voice_design",
        _body("design"),
        request_id="request-mixed-voice",
        idempotency_key=None,
    )
    queue = PostgresDispatchQueue(db.pool)
    claims = await queue.claim_due("worker-mixed", 30_000, 10)
    assert {claim.job.kind for claim in claims} == {"asr", "tts"}
    assert {claim.job.job_id for claim in claims} == {tts.job_id, asr.job_id}
    assert voice.job_id not in {claim.job.job_id for claim in claims}
    row = db.query("SELECT job_state FROM coordination_jobs WHERE job_id=%s", (voice.job_id,))[0]
    assert row["job_state"] == "pending"
    # The unsupported row stays pending and cannot fail or starve later claims.
    assert await queue.claim_due("worker-mixed-2", 30_000, 10) == []


async def test_http_lost_create_ack_with_key_replays_the_committed_draft(
    voice_db: JobSandbox, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = PostgresPublicJobs(voice_db.pool)
    original_create = adapter.create
    state = {"lost": False}

    def lose_ack(job: NewPublicJob) -> JobCreateResult:
        result = original_create(job)
        if not state["lost"] and result.outcome == "created":
            state["lost"] = True
            return JobCreateResult("unknown", error_code="outcome_unknown")
        return result

    monkeypatch.setattr(adapter, "create", lose_ack)
    provider = VoiceDrafts(PublicJobs(adapter, adapter), PostgresVoiceDrafts(voice_db.pool))
    app = create_app(settings=_settings(), jobs=voice_db.jobs, voice_drafts=provider)
    headers = {"Idempotency-Key": "http-lost-ack-key"}
    async with AsyncClient(transport=ASGITransport(app), base_url="http://localhost") as http:
        lost = await http.post("/v1/nexus/voice-designs", json=_body("design"), headers=headers)
        assert lost.status_code == 503, lost.text
        assert lost.json()["error"]["code"] == "voice_draft_outcome_unknown"
        assert lost.json()["error"]["retryable"] is True
        assert lost.headers["X-Should-Retry"] == "true"
        validate_response(
            "createVoiceDesign", 503, {"body": lost.json(), "headers": dict(lost.headers)}
        )
        replay = await http.post("/v1/nexus/voice-designs", json=_body("design"), headers=headers)
        assert replay.status_code == 202, replay.text
        assert replay.json()["draft"]["id"] == "draft-" + replay.json()["job"]["id"]
        assert voice_db.count("speech_voice_drafts") == voice_db.count("speech_jobs") == 1
        different = await http.post(
            "/v1/nexus/voice-designs",
            json=_body("design"),
            headers={"Idempotency-Key": "http-other-key"},
        )
        assert different.status_code == 202, different.text
        assert different.json()["draft"]["id"] != replay.json()["draft"]["id"]
        assert voice_db.count("speech_voice_drafts") == 2


async def test_http_lost_create_ack_without_key_is_not_retryable(
    voice_db: JobSandbox, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = PostgresPublicJobs(voice_db.pool)
    original_create = adapter.create
    state = {"lost": False}

    def lose_ack(job: NewPublicJob) -> JobCreateResult:
        result = original_create(job)
        if not state["lost"] and result.outcome == "created":
            state["lost"] = True
            return JobCreateResult("unknown", error_code="outcome_unknown")
        return result

    monkeypatch.setattr(adapter, "create", lose_ack)
    provider = VoiceDrafts(PublicJobs(adapter, adapter), PostgresVoiceDrafts(voice_db.pool))
    app = create_app(settings=_settings(), jobs=voice_db.jobs, voice_drafts=provider)
    async with AsyncClient(transport=ASGITransport(app), base_url="http://localhost") as http:
        lost = await http.post("/v1/nexus/voice-designs", json=_body("design"))
        assert lost.status_code == 409, lost.text
        assert lost.json()["error"]["code"] == "voice_draft_outcome_unknown"
        assert lost.json()["error"]["retryable"] is False
        assert lost.headers["X-Should-Retry"] == "false"
        validate_response(
            "createVoiceDesign", 409, {"body": lost.json(), "headers": dict(lost.headers)}
        )
        resubmitted = await http.post("/v1/nexus/voice-designs", json=_body("design"))
        assert resubmitted.status_code == 202, resubmitted.text
        assert voice_db.count("speech_voice_drafts") == 2
        drafts = {
            row["draft_id"] for row in voice_db.query("SELECT draft_id FROM speech_voice_drafts")
        }
        assert resubmitted.json()["draft"]["id"] in drafts


@pytest.mark.parametrize("kind", ["design", "clone"])
@pytest.mark.parametrize("language", ["", "   ", "x" * 36])
async def test_blank_or_overlong_language_is_rejected_before_persistence(
    voice_db: JobSandbox, kind: str, language: str
) -> None:
    db = voice_db
    app = create_app(settings=_settings(), jobs=db.jobs, voice_drafts=_provider(db))
    path = "/v1/nexus/voice-" + ("designs" if kind == "design" else "clones")
    body = _body(kind)
    body["input"]["language"] = language
    async with AsyncClient(transport=ASGITransport(app), base_url="http://localhost") as http:
        response = await http.post(path, json=body)
    assert response.status_code == 400, response.text
    assert response.json()["error"]["code"] == "voice_draft_invalid_request"
    assert db.count("speech_voice_drafts") == 0
    assert db.count("speech_jobs") == 0


@pytest.mark.parametrize("kind", ["design", "clone"])
@pytest.mark.parametrize(
    ("language", "expected"),
    [
        ("auto", "auto"),
        ("zh-CN", "zh-CN"),
        ("EN-US", "en-US"),
        ("en-US", "en-US"),
        ("english", "und"),
        ("x!", "und"),
    ],
)
async def test_language_accepts_any_contract_string(
    voice_db: JobSandbox, kind: str, language: str, expected: str
) -> None:
    db = voice_db
    app = create_app(settings=_settings(), jobs=db.jobs, voice_drafts=_provider(db))
    path = "/v1/nexus/voice-" + ("designs" if kind == "design" else "clones")
    body = _body(kind)
    body["input"]["language"] = language
    async with AsyncClient(transport=ASGITransport(app), base_url="http://localhost") as http:
        created = await http.post(path, json=body)
        assert created.status_code == 202, created.text
        validate_response(
            "createVoiceDesign" if kind == "design" else "createVoiceClone",
            202,
            {"body": created.json(), "headers": dict(created.headers)},
        )
        read = await http.get("/v1/nexus/voice-drafts/" + created.json()["draft"]["id"])
    assert read.status_code == 200, read.text
    assert read.json()["revision"]["input"]["language"] == expected
    assert db.count("speech_voice_drafts") == db.count("speech_jobs") == 1


@pytest.mark.parametrize("kind", ["voice_design", "voice_clone"])
async def test_generic_jobs_endpoint_creates_one_draft_and_replays_it(
    voice_db: JobSandbox, kind: str
) -> None:
    db = voice_db
    app = create_app(settings=_settings(), jobs=db.jobs, voice_drafts=_provider(db))
    payload = {
        "kind": kind,
        "model": "voice-model",
        "input": _body("design" if kind == "voice_design" else "clone")["input"],
    }
    headers = {"Idempotency-Key": "generic-" + kind}
    async with AsyncClient(transport=ASGITransport(app), base_url="http://localhost") as http:
        first = await http.post("/v1/nexus/jobs", json=payload, headers=headers)
        assert first.status_code == 202, first.text
        parse_contract("Job", first.json())
        job_id = first.json()["id"]
        assert db.count("speech_jobs") == 1
        assert db.count("speech_voice_drafts") == 1
        draft = await http.get("/v1/nexus/voice-drafts/draft-" + job_id)
        assert draft.status_code == 200, draft.text
        assert draft.json()["revision"]["kind"] == kind.removeprefix("voice_")
        replay = await http.post("/v1/nexus/jobs", json=payload, headers=headers)
        assert replay.status_code == 202, replay.text
        assert replay.json()["id"] == job_id
        assert db.count("speech_jobs") == db.count("speech_voice_drafts") == 1


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
    validate_response("getVoiceDraft", 200, {"body": read.json()})
