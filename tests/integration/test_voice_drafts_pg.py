"""Real migrated PG voice draft/Job atomicity; only caller-supplied isolated DSNs."""

import json
import os
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
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
    VoiceDraftNotCandidateError,
    VoiceDraftStaleRevisionError,
)
from speechnexus_backend.speech.application.voice_drafts import (
    VoiceDraftNotFoundError,
    VoiceDrafts,
)
from speechnexus_backend.speech.domain.public_jobs import (
    JobCreateResult,
    JobState,
    NewPublicJob,
    PublicJobUnavailableError,
)
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
        definition = json.dumps(
            {
                "logical_model": "voice-model",
                "tasks": ["voice_design", "voice_clone", "voice_validation"],
            }
        )
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
    revision: str = "1",
) -> str:
    result: dict[str, Any] = {
        "kind": "voice_" + kind,
        "voice_draft_id": draft_id,
        "draft_revision": revision,
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
    before = voice_db.query(
        "SELECT display_name, metadata_revision FROM speech_voice_drafts WHERE draft_id=%s",
        (draft["id"],),
    )[0]
    assert before == {"display_name": "Voice design", "metadata_revision": 1}
    headers = {"Idempotency-Key": "candidate-patch-key"}
    async with AsyncClient(transport=ASGITransport(app), base_url="http://localhost") as http:
        read = await http.get(path)
        patch = await http.patch(
            path,
            json={"expected_metadata_revision": 1, "display_name": "Candidate voice"},
            headers=headers,
        )
        replay = await http.patch(
            path,
            json={"expected_metadata_revision": 1, "display_name": "Candidate voice"},
            headers=headers,
        )
    assert read.status_code == patch.status_code == replay.status_code == expected, (
        read.text,
        patch.text,
        replay.text,
    )
    after = voice_db.query(
        "SELECT display_name, metadata_revision FROM speech_voice_drafts WHERE draft_id=%s",
        (draft["id"],),
    )[0]
    if expected == 200:
        assert read.json()["draft"]["status"] == "candidate"
        assert patch.json()["status"] == "candidate"
        assert patch.json()["head_revision"] == "1"
        assert read.json()["revision"]["preview_artifact_ids"] == ["preview-synthetic"]
        assert after == {"display_name": "Candidate voice", "metadata_revision": 2}
        assert voice_db.count("speech_voice_metadata_commands") == 1
        assert replay.json() == patch.json()
        validate_response("getVoiceDraft", 200, {"body": read.json()})
        validate_response("updateVoiceDraftMetadata", 200, {"body": patch.json()})
    else:
        for body in (read.json(), patch.json(), replay.json()):
            assert body["error"]["code"] == "voice_draft_unavailable"
            assert body["error"]["retryable"] is True
        assert after == before
        assert voice_db.count("speech_voice_metadata_commands") == 0
        validate_response(
            "getVoiceDraft", 503, {"body": read.json(), "headers": dict(read.headers)}
        )
        validate_response(
            "updateVoiceDraftMetadata",
            503,
            {"body": patch.json(), "headers": dict(patch.headers)},
        )


async def test_keyed_patch_receipt_does_not_copy_large_input(voice_db: JobSandbox) -> None:
    db = voice_db
    app = create_app(settings=_settings(), jobs=db.jobs, voice_drafts=_provider(db))
    body = {
        "model": "voice-model",
        "input": {
            "reference_audio_asset_id": "synthetic-reference",
            "source_use_confirmed": True,
            "reference_text": "声" * 10000,
            "preview_text": "音" * 2000,
        },
    }
    async with AsyncClient(transport=ASGITransport(app), base_url="http://localhost") as http:
        created = await http.post("/v1/nexus/voice-clones", json=body)
        assert created.status_code == 202, created.text
        draft_id = created.json()["draft"]["id"]
        input_bytes = db.query(
            "SELECT OCTET_LENGTH(input_json) AS n FROM speech_voice_drafts WHERE draft_id=%s",
            (draft_id,),
        )[0]["n"]
        assert input_bytes > 36_000, input_bytes
        headers = {"Idempotency-Key": "large-input-patch-key"}
        patch = await http.patch(
            "/v1/nexus/voice-drafts/" + draft_id,
            json={"expected_metadata_revision": 1, "display_name": "Large input voice"},
            headers=headers,
        )
        assert patch.status_code == 200, patch.text
        validate_response("updateVoiceDraftMetadata", 200, {"body": patch.json()})
        assert patch.json()["metadata_revision"] == 2
        receipt = db.query(
            "SELECT OCTET_LENGTH(result_json) AS n FROM speech_voice_metadata_commands "
            "WHERE draft_id=%s",
            (draft_id,),
        )[0]["n"]
        assert 2 <= receipt <= 8192, receipt
        replay = await http.patch(
            "/v1/nexus/voice-drafts/" + draft_id,
            json={"expected_metadata_revision": 1, "display_name": "Large input voice"},
            headers=headers,
        )
        assert replay.status_code == 200, replay.text
        assert replay.json() == patch.json()
    assert db.count("speech_voice_drafts") == 1
    assert db.count("speech_voice_metadata_commands") == 1
    row = db.query(
        "SELECT display_name, metadata_revision FROM speech_voice_drafts WHERE draft_id=%s",
        (draft_id,),
    )[0]
    assert row == {"display_name": "Large input voice", "metadata_revision": 2}


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


def _candidate_draft(db: JobSandbox, kind: str = "design") -> tuple[VoiceDrafts, dict[str, Any]]:
    provider = _provider(db)
    draft, job = provider.create(
        "voice_design" if kind == "design" else "voice_clone",
        _body(kind),
        request_id="request-candidate-" + kind,
        idempotency_key=None,
    )
    _seed_preview_artifact(db)
    db.jobs.advance(job.job_id, "running")
    db.jobs.advance(
        job.job_id,
        "succeeded",
        result_json=_candidate_result(draft["id"], kind),
    )
    return provider, draft


def _generation_body(draft: dict[str, Any], kind: str = "design") -> dict[str, Any]:
    return {
        "expected_draft_revision": draft["head_revision"],
        "model": "voice-model",
        "input": _body(kind)["input"],
    }


def test_generate_appends_one_revision_and_replays_after_head_advance(
    voice_db: JobSandbox,
) -> None:
    provider, draft = _candidate_draft(voice_db)
    first, first_job = provider.generate(
        draft["id"],
        _generation_body(draft),
        request_id="request-generate-1",
        idempotency_key="generate-key-1",
    )
    assert first["revision"]["revision"] == "2"
    assert first["draft"]["head_revision"] == "2"
    assert first_job.kind == "voice_design"
    assert voice_db.count("speech_voice_draft_revisions") == 2
    assert voice_db.count("speech_jobs") == 2
    assert voice_db.count("coordination_outbox") == 2

    # Make revision 2 a candidate, then append revision 3 through the same
    # draft head; the first key must still replay revision 2 and Job 2.
    _seed_preview_artifact(voice_db, "preview-revision-2", receipt="receipt-revision-2")
    voice_db.jobs.advance(first_job.job_id, "running")
    voice_db.jobs.advance(
        first_job.job_id,
        "succeeded",
        result_json=_candidate_result(
            draft["id"],
            "design",
            receipt="receipt-revision-2",
            previews=["preview-revision-2"],
            revision="2",
        ),
    )
    second, _second_job = provider.generate(
        first["draft"]["id"],
        _generation_body(first["draft"]),
        request_id="request-generate-2",
        idempotency_key="generate-key-2",
    )
    assert second["revision"]["revision"] == "3"
    assert second["draft"]["head_revision"] == "3"

    replay, replay_job = provider.generate(
        draft["id"],
        _generation_body(draft),
        request_id="request-generate-replay",
        idempotency_key="generate-key-1",
    )
    assert replay_job.job_id == first_job.job_id
    assert replay["revision"]["revision"] == "2"
    assert replay["draft"]["head_revision"] == "3"
    assert voice_db.count("speech_voice_draft_revisions") == 3
    assert voice_db.count("speech_jobs") == 3

    with pytest.raises(VoiceDraftStaleRevisionError):
        provider.generate(
            draft["id"],
            _generation_body(draft),
            request_id="request-generate-stale",
            idempotency_key=None,
        )
    with pytest.raises(VoiceDraftNotFoundError):
        provider.generate(
            "draft-missing",
            _generation_body(draft),
            request_id="request-generate-missing",
            idempotency_key=None,
        )


def _validation_document(draft_id: str, revision: str = "1") -> dict[str, Any]:
    return {
        "resource_type": "voice_validation",
        "id": "validation-1",
        "object": "nexus.voice_validation",
        "draft_id": draft_id,
        "draft_revision": revision,
        "validation_set_revision": "1",
        "model_binding": {"id": "voice-model", "revision": "1"},
        "status": "passed",
        "checks": [
            {"name": "intelligibility", "required": True, "status": "passed"},
            {"name": "reusable_tts", "required": True, "status": "passed"},
        ],
        "publication_eligible": True,
        "created_at": "2026-10-24T12:00:00Z",
    }


def _seed_validation_report(
    db: JobSandbox, artifact_id: str, receipt: str, *, state: str = "ready"
) -> None:
    db.admin.execute(
        "INSERT INTO speech_artifacts "
        "(artifact_id, workspace_id, artifact_kind, artifact_state, media_type, bytes, "
        "sha256, receipt_id, source_operation_id, object_key, storage_version_id, created_at_us) "
        "VALUES (%s, 'workspace-default', 'validation_report', %s, 'application/json', "
        "128, %s, %s, NULL, %s, 'synthetic-version', 10)",
        (
            artifact_id,
            state,
            "sha256:" + "f" * 64,
            receipt,
            "private/artifacts/workspace-default/" + artifact_id,
        ),
    )


def test_validate_targets_a_candidate_revision_and_replays(
    voice_db: JobSandbox,
) -> None:
    provider, draft = _candidate_draft(voice_db)
    body = {
        "expected_draft_revision": "1",
        "validation_set_revision": "1",
    }
    first = provider.validate(
        draft["id"], body, request_id="request-validate-1", idempotency_key="validate-key-1"
    )
    assert first.kind == "voice_validation"
    assert first.model_id == "voice-model"
    replay = provider.validate(
        draft["id"],
        body,
        request_id="request-validate-replay",
        idempotency_key="validate-key-1",
    )
    assert replay.job_id == first.job_id
    assert voice_db.count("speech_jobs") == 2

    # A pending next head is not a candidate, and a later head makes revision 1
    # stale; both refusals happen before a second Job is written.
    second, _second_job = provider.generate(
        draft["id"],
        _generation_body(draft),
        request_id="request-validate-next",
        idempotency_key="validate-next-key",
    )
    with pytest.raises(VoiceDraftStaleRevisionError):
        provider.validate(
            draft["id"], body, request_id="request-validate-stale", idempotency_key=None
        )
    with pytest.raises(VoiceDraftNotCandidateError):
        provider.validate(
            second["draft"]["id"],
            {
                "expected_draft_revision": second["draft"]["head_revision"],
                "validation_set_revision": "1",
            },
            request_id="request-validate-pending",
            idempotency_key=None,
        )
    assert voice_db.count("speech_jobs") == 3

    # Readback authority is the committed report Artifact, not the result text.
    _seed_validation_report(voice_db, "validation-report-1", "validation-receipt-1")
    voice_db.jobs.advance(first.job_id, "running")
    voice_db.jobs.advance(
        first.job_id,
        "succeeded",
        result_json=json.dumps(
            {
                "kind": "voice_validation",
                "validation": _validation_document(draft["id"]),
                "validation_report_artifact_id": "validation-report-1",
                "receipt_id": "validation-receipt-1",
            }
        ),
    )
    succeeded = voice_db.jobs.get(first.job_id)
    assert succeeded is not None
    facts = voice_db.jobs.artifacts(succeeded)
    assert [fact.artifact_id for fact in facts] == ["validation-report-1"]
    page = voice_db.jobs.artifacts_page([succeeded])
    assert [fact.artifact_id for fact in page[0]] == ["validation-report-1"]


async def test_http_generate_and_validate_expose_revision_contract(
    voice_db: JobSandbox,
) -> None:
    provider, draft = _candidate_draft(voice_db)
    app = create_app(settings=_settings(), jobs=voice_db.jobs, voice_drafts=provider)
    generation = {
        "expected_draft_revision": "1",
        "model": "voice-model",
        "input": _body("design")["input"],
    }
    async with AsyncClient(transport=ASGITransport(app), base_url="http://localhost") as http:
        created = await http.post(
            "/v1/nexus/voice-drafts/" + draft["id"] + "/generations",
            json=generation,
            headers={"Idempotency-Key": "http-generate-key"},
        )
        assert created.status_code == 202, created.text
        validate_response(
            "generateVoiceDraftRevision",
            202,
            {"body": created.json(), "headers": dict(created.headers)},
        )
        assert created.json()["revision"]["revision"] == "2"
        assert created.json()["job"]["kind"] == "voice_design"
        replay = await http.post(
            "/v1/nexus/voice-drafts/" + draft["id"] + "/generations",
            json=generation,
            headers={"Idempotency-Key": "http-generate-key"},
        )
        assert replay.status_code == 202, replay.text
        assert replay.json()["job"]["id"] == created.json()["job"]["id"]
        assert voice_db.count("speech_voice_draft_revisions") == 2

        stale = await http.post(
            "/v1/nexus/voice-drafts/" + draft["id"] + "/generations",
            json={**generation, "expected_draft_revision": "1"},
        )
        assert stale.status_code == 409, stale.text
        validate_response(
            "generateVoiceDraftRevision",
            409,
            {"body": stale.json(), "headers": dict(stale.headers)},
        )

        missing = await http.post(
            "/v1/nexus/voice-drafts/draft-missing/generations",
            json=generation,
        )
        assert missing.status_code == 404, missing.text
        unknown_revision = await http.post(
            "/v1/nexus/voice-drafts/" + draft["id"] + "/generations",
            json={**generation, "expected_draft_revision": "9"},
        )
        assert unknown_revision.status_code == 409, unknown_revision.text
        unknown_validation = await http.post(
            "/v1/nexus/voice-drafts/" + draft["id"] + "/validations",
            json={"expected_draft_revision": "9", "validation_set_revision": "1"},
        )
        assert unknown_validation.status_code == 409, unknown_validation.text

        _seed_preview_artifact(
            voice_db, "preview-http-revision-2", receipt="receipt-http-revision-2"
        )
        generated = voice_db.jobs.get(created.json()["job"]["id"])
        assert generated is not None
        voice_db.jobs.advance(generated.job_id, "running")
        voice_db.jobs.advance(
            generated.job_id,
            "succeeded",
            result_json=_candidate_result(
                draft["id"],
                "design",
                receipt="receipt-http-revision-2",
                previews=["preview-http-revision-2"],
                revision="2",
            ),
        )
        validation = {"expected_draft_revision": "2", "validation_set_revision": "1"}
        accepted = await http.post(
            "/v1/nexus/voice-drafts/" + draft["id"] + "/validations",
            json=validation,
            headers={"Idempotency-Key": "http-validation-key"},
        )
        assert accepted.status_code == 202, accepted.text
        validate_response(
            "validateVoiceDraftRevision",
            202,
            {"body": accepted.json(), "headers": dict(accepted.headers)},
        )
        assert accepted.json()["kind"] == "voice_validation"
        validation_replay = await http.post(
            "/v1/nexus/voice-drafts/" + draft["id"] + "/validations",
            json=validation,
            headers={"Idempotency-Key": "http-validation-key"},
        )
        assert validation_replay.status_code == 202
        assert validation_replay.json()["id"] == accepted.json()["id"]

        stale_validation = await http.post(
            "/v1/nexus/voice-drafts/" + draft["id"] + "/validations",
            json={"expected_draft_revision": "1", "validation_set_revision": "1"},
        )
        assert stale_validation.status_code == 409, stale_validation.text

        pending, _ = provider.create(
            "voice_design",
            _body("design"),
            request_id="request-http-pending",
            idempotency_key=None,
        )
        not_candidate = await http.post(
            "/v1/nexus/voice-drafts/" + pending["id"] + "/validations",
            json={"expected_draft_revision": "1", "validation_set_revision": "1"},
        )
        assert not_candidate.status_code == 422, not_candidate.text
        assert not_candidate.json()["error"]["code"] == "voice_draft_not_candidate"


@pytest.mark.parametrize(
    ("artifact_receipt", "state"),
    [("other-receipt", "ready"), ("validation-receipt-negative", "expired")],
)
def test_validation_report_readback_requires_matching_ready_artifact(
    voice_db: JobSandbox, artifact_receipt: str, state: str
) -> None:
    provider, draft = _candidate_draft(voice_db)
    job = provider.validate(
        draft["id"],
        {"expected_draft_revision": "1", "validation_set_revision": "1"},
        request_id="request-validation-negative",
        idempotency_key=None,
    )
    _seed_validation_report(voice_db, "validation-report-negative", artifact_receipt, state=state)
    voice_db.jobs.advance(job.job_id, "running")
    voice_db.jobs.advance(
        job.job_id,
        "succeeded",
        result_json=json.dumps(
            {
                "kind": "voice_validation",
                "validation": _validation_document(draft["id"]),
                "validation_report_artifact_id": "validation-report-negative",
                "receipt_id": "validation-receipt-negative",
            }
        ),
    )
    succeeded = voice_db.jobs.get(job.job_id)
    assert succeeded is not None
    with pytest.raises(PublicJobUnavailableError):
        voice_db.jobs.artifacts(succeeded)


def test_generate_store_replay_survives_a_missed_precheck(
    voice_db: JobSandbox, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider, draft = _candidate_draft(voice_db)
    body = _generation_body(draft)
    first, first_job = provider.generate(
        draft["id"], body, request_id="request-race-1", idempotency_key="generate-race-key"
    )
    assert first["revision"]["revision"] == "2"
    # Simulate the application's first lookup missing a concurrent commit. The
    # adapter must still decide replay before the draft lock, or this becomes stale.
    monkeypatch.setattr(PublicJobs, "_find_original", lambda *args, **kwargs: None)
    replay, replay_job = provider.generate(
        draft["id"], body, request_id="request-race-2", idempotency_key="generate-race-key"
    )
    assert replay_job.job_id == first_job.job_id
    assert replay["revision"]["revision"] == "2"
    assert voice_db.count("speech_voice_draft_revisions") == 2


def test_revision_insert_failure_rolls_back_job_outbox_and_head(
    voice_db: JobSandbox, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider, draft = _candidate_draft(voice_db)
    monkeypatch.setattr(
        "speechnexus_backend.speech.infrastructure.public_jobs_postgres._next_revision",
        lambda _current: "0",
    )
    with pytest.raises(JobRejectedError):
        provider.generate(
            draft["id"],
            _generation_body(draft),
            request_id="request-revision-rollback",
            idempotency_key=None,
        )
    assert voice_db.count("speech_jobs") == 1
    assert voice_db.count("speech_voice_draft_revisions") == 1
    assert voice_db.count("coordination_outbox") == 1
    read = provider.get(draft["id"])
    assert read["draft"]["head_revision"] == "1"


async def test_generic_validation_job_derives_binding_and_maps_refusals(
    voice_db: JobSandbox,
) -> None:
    provider, draft = _candidate_draft(voice_db)
    app = create_app(settings=_settings(), jobs=voice_db.jobs, voice_drafts=provider)
    body = {
        "kind": "voice_validation",
        "input": {
            "voice_draft_id": draft["id"],
            "draft_revision": "1",
            "validation_set_revision": "1",
        },
    }
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://localhost") as http:
        accepted = await http.post(
            "/v1/nexus/jobs", json=body, headers={"Idempotency-Key": "generic-validation-key"}
        )
        assert accepted.status_code == 202, accepted.text
        parse_contract("Job", accepted.json())
        assert accepted.json()["kind"] == "voice_validation"
        assert accepted.json()["effective_configuration"]["models"]["voice"] == {
            "id": "voice-model",
            "revision": "1",
        }
        replay = await http.post(
            "/v1/nexus/jobs", json=body, headers={"Idempotency-Key": "generic-validation-key"}
        )
        assert replay.status_code == 202, replay.text
        assert replay.json()["id"] == accepted.json()["id"]
        assert voice_db.count("speech_jobs") == 2

        missing = await http.post(
            "/v1/nexus/jobs",
            json={
                "kind": "voice_validation",
                "input": {
                    "voice_draft_id": draft["id"],
                    "draft_revision": "9",
                    "validation_set_revision": "1",
                },
            },
        )
        assert missing.status_code == 404, missing.text
        parse_contract("ErrorResponse", missing.json())

        next_revision, _next_job = provider.generate(
            draft["id"],
            _generation_body(draft),
            request_id="request-generic-next",
            idempotency_key="generic-next-key",
        )
        stale = await http.post("/v1/nexus/jobs", json=body)
        assert stale.status_code == 409, stale.text
        parse_contract("ErrorResponse", stale.json())
        pending = await http.post(
            "/v1/nexus/jobs",
            json={
                "kind": "voice_validation",
                "input": {
                    "voice_draft_id": draft["id"],
                    "draft_revision": next_revision["draft"]["head_revision"],
                    "validation_set_revision": "1",
                },
            },
        )
        assert pending.status_code == 422, pending.text
        parse_contract("ErrorResponse", pending.json())


async def test_malformed_validation_result_reads_as_unavailable(
    voice_db: JobSandbox,
) -> None:
    provider, draft = _candidate_draft(voice_db)
    job = provider.validate(
        draft["id"],
        {"expected_draft_revision": "1", "validation_set_revision": "1"},
        request_id="request-malformed-validation",
        idempotency_key=None,
    )
    _seed_validation_report(voice_db, "validation-report-malformed", "validation-receipt-malformed")
    voice_db.jobs.advance(job.job_id, "running")
    voice_db.jobs.advance(
        job.job_id,
        "succeeded",
        result_json=json.dumps(
            {
                "kind": "voice_validation",
                "validation_report_artifact_id": "validation-report-malformed",
                "receipt_id": "validation-receipt-malformed",
            }
        ),
    )
    succeeded = voice_db.jobs.get(job.job_id)
    assert succeeded is not None
    with pytest.raises(PublicJobUnavailableError):
        voice_db.jobs.artifacts(succeeded)
    app = create_app(settings=_settings(), jobs=voice_db.jobs, voice_drafts=provider)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://localhost") as http:
        response = await http.get("/v1/nexus/jobs/" + job.job_id)
        assert response.status_code == 503, response.text
        parse_contract("ErrorResponse", response.json())


def test_stale_revision_store_guard_rejects_a_missed_precheck(
    voice_db: JobSandbox, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider, draft = _candidate_draft(voice_db)
    store = voice_db.jobs._store
    original = store.create_revision
    captured: dict[str, NewPublicJob] = {}

    def capture(job: NewPublicJob, **kwargs: Any) -> Any:
        captured.setdefault("job", job)
        return original(job, **kwargs)

    monkeypatch.setattr(store, "create_revision", capture)
    provider.generate(
        draft["id"],
        _generation_body(draft),
        request_id="request-store-cas-1",
        idempotency_key=None,
    )
    monkeypatch.setattr(store, "create_revision", original)
    probe = replace(captured["job"], job_id="job-stale-revision-probe")
    result = original(probe, draft_id=draft["id"], expected_revision="1")
    assert result.outcome == "stale", result
    assert voice_db.count("speech_voice_draft_revisions") == 2


def test_validation_target_is_relocked_before_insert(
    voice_db: JobSandbox, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider, draft = _candidate_draft(voice_db)
    store = voice_db.jobs._store
    original = store.create
    captured: dict[str, NewPublicJob] = {}

    def capture(job: NewPublicJob) -> Any:
        captured.setdefault("job", job)
        return original(job)

    monkeypatch.setattr(store, "create", capture)
    provider.validate(
        draft["id"],
        {"expected_draft_revision": "1", "validation_set_revision": "1"},
        request_id="request-store-validation-1",
        idempotency_key=None,
    )
    monkeypatch.setattr(store, "create", original)
    provider.generate(
        draft["id"],
        _generation_body(draft),
        request_id="request-store-validation-next",
        idempotency_key=None,
    )
    probe = replace(captured["job"], job_id="job-stale-validation-probe")
    result = original(probe)
    assert result.outcome == "rejected"
    assert result.error_code == "stale"
    assert voice_db.count("speech_jobs") == 3
