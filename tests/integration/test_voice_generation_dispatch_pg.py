"""Real PostgreSQL voice generation lane: claim, result, publication and timeouts.

The engine adapter itself is a later slice; every test below drives the same
dispatcher, claim lease, state store and result publisher the worker uses, and
only replaces the gateway transport and object bytes.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Iterator, Mapping
from typing import Any

import httpx
import pytest
from alembic import command
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr
from tests.integration.test_public_job_progress_migration_pg import _isolated_schema
from tests.integration.test_public_jobs_http import JobSandbox, _seed_binding
from tests.unit.backend.test_health import _settings

from speechnexus_backend.app import create_app
from speechnexus_backend.speech.application.job_dispatcher import JobDispatcher
from speechnexus_backend.speech.application.public_jobs import PublicJobs
from speechnexus_backend.speech.application.results import ResultArtifactPublisher
from speechnexus_backend.speech.application.voice_drafts import VoiceDrafts
from speechnexus_backend.speech.domain.artifacts import ArtifactObjectWrite
from speechnexus_backend.speech.domain.assets import hash_bytes
from speechnexus_backend.speech.domain.job_dispatch import (
    VOICE_GENERATION_JOB_KINDS,
    DispatchClaimLostError,
    DispatchJob,
    DispatchLimits,
    ExecutionSubmission,
    result_artifact_identity,
    terminal_result,
)
from speechnexus_backend.speech.infrastructure.artifacts_postgres import PostgresArtifactWriter
from speechnexus_backend.speech.infrastructure.execution_gateway_adapter import (
    ExecutionGatewayAdapter,
)
from speechnexus_backend.speech.infrastructure.gateway_execution_client import (
    GatewayExecutionClient,
)
from speechnexus_backend.speech.infrastructure.job_cancellation_fence_schema import (
    NOT_SUBMITTED_TABLE,
)
from speechnexus_backend.speech.infrastructure.job_cancellation_reconciliation_postgres import (
    PostgresCancellationReconciliationStore,
)
from speechnexus_backend.speech.infrastructure.job_dispatch_postgres import (
    PostgresDispatchQueue,
    PostgresDispatchStateStore,
)
from speechnexus_backend.speech.infrastructure.postgres_pool import speech_pool
from speechnexus_backend.speech.infrastructure.public_jobs_postgres import PostgresPublicJobs
from speechnexus_backend.speech.infrastructure.voice_drafts_postgres import PostgresVoiceDrafts

pytestmark = pytest.mark.integration
VARIABLE = "SPEECHNEXUS_TEST_DATABASE_URL"
DIGEST = "sha256:" + "b" * 64
CLOCK_US = 1_700_000_000_000_000
PREVIEW_BODY = b"synthetic-voice-preview-bytes"


def _voice_model(admin: Any) -> None:
    """Seed the published voice binding and the clone reference Asset."""
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
        (DIGEST, DIGEST),
    )
    admin.execute(
        "INSERT INTO speech_binding_revisions "
        "(binding_id,revision,revision_id,key_digest,document_version,definition_json,"
        "definition_digest,request_digest,created_at_us) "
        "VALUES ('voice-model','1','voice-model-rev-1',%s,1,%s,%s,%s,1)",
        (DIGEST, definition, DIGEST, DIGEST),
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
        (DIGEST, DIGEST),
    )


@pytest.fixture
def voice_dispatch_db() -> Iterator[JobSandbox]:
    url = os.environ.get(VARIABLE)
    if not url:
        pytest.fail("Configure an isolated PostgreSQL DSN; skip is not dispatch evidence")
    with _isolated_schema(url) as (config, admin):
        command.upgrade(config, "head")
        schema = str(config.attributes["schema"])
        _seed_binding(admin)
        _voice_model(admin)
        with speech_pool(url, schema=schema, name="voice-dispatch-test") as pool:
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


class _Planner:
    async def plan(self, job: DispatchJob, submit_intent_id: str) -> ExecutionSubmission:
        return ExecutionSubmission(
            submit_intent_id,
            {
                "submit_intent_id": submit_intent_id,
                "operation_id": job.job_id,
                "command_digest": DIGEST,
                "command": {"schema_version": "1.0.0", "kind": job.kind},
            },
        )


class _Blobs:
    def __init__(self) -> None:
        self.writes = 0

    def check_writes_available(self) -> None:
        return None

    def put_verified(self, write: ArtifactObjectWrite) -> str:
        del write
        self.writes += 1
        return "version-1"


class _Staging:
    def __init__(self) -> None:
        self.reads = 0

    def read(self, reference: Mapping[str, Any], storage_version: str) -> bytes:
        assert storage_version == "version-staging-1"
        assert reference["byte_length"] == len(PREVIEW_BODY)
        self.reads += 1
        return PREVIEW_BODY


def _receipt() -> dict[str, Any]:
    return {
        "request_digest": DIGEST,
        "reference": {
            "asset_or_artifact_id": "artifact-staging-voice",
            "workspace_id": "workspace-default",
            "storage_bucket": "synthetic-results",
            "object_key": "staging/voice-preview",
            "content_sha256": hash_bytes(PREVIEW_BODY),
            "byte_length": len(PREVIEW_BODY),
        },
        "storage_version": "version-staging-1",
        "media_type": "audio/wav",
    }


class _Gateway:
    """Minimal execution-gateway transport; the voice engine is a later slice."""

    def __init__(
        self,
        *,
        states: tuple[str, ...] = ("succeeded",),
        submit_rejection: tuple[int, str] | None = None,
        engine_error: str | None = None,
    ) -> None:
        self._states, self._submit_rejection, self._engine_error = (
            states,
            submit_rejection,
            engine_error,
        )
        self.submit_calls = 0
        self.submit_id: str | None = None
        self._query_index = 0

    def _status(self, state: str) -> dict[str, Any]:
        status: dict[str, Any] = {
            "submit_intent_id": self.submit_id,
            "execution_state": state,
        }
        if state in ("failed", "aborted") and self._engine_error is not None:
            status["error_code"] = self._engine_error
        return status

    def _next_state(self) -> str:
        index = min(self._query_index, len(self._states) - 1)
        self._query_index += 1
        return self._states[index]

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "POST" and path == "/internal/v1/executions/submit":
            self.submit_calls += 1
            if self._submit_rejection is not None:
                status, code = self._submit_rejection
                return httpx.Response(
                    status,
                    json={
                        "error": {
                            "code": code,
                            "message": "synthetic rejection",
                            "retryable": False,
                        }
                    },
                )
            self.submit_id = json.loads(request.content)["submit_intent_id"]
            return httpx.Response(200, json=self._status("executing"))
        if request.method == "GET" and path.startswith("/internal/v1/executions/"):
            if self.submit_calls == 0 or path.endswith("/audio-staging-result"):
                if path.endswith("/audio-staging-result") and self.submit_calls > 0:
                    return httpx.Response(
                        200,
                        json={
                            "submit_intent_id": self.submit_id,
                            "request_digest": DIGEST,
                            "staging_result": _receipt(),
                        },
                    )
                return httpx.Response(
                    404, json={"error": {"code": "control_execution_not_found", "message": "none"}}
                )
            if path == "/internal/v1/executions/" + str(self.submit_id):
                return httpx.Response(200, json=self._status(self._next_state()))
        return httpx.Response(404, json={"error": {"code": "unknown_path", "message": path}})


def _dispatcher(
    db: JobSandbox,
    gateway: _Gateway,
    *,
    states: PostgresDispatchStateStore | None = None,
    limits: DispatchLimits | None = None,
    now_us: Any = None,
) -> JobDispatcher:
    client = ExecutionGatewayAdapter(
        GatewayExecutionClient(
            base_url="http://127.0.0.1:9999",
            credential=SecretStr("synthetic-voice-token"),
            timeout_seconds=1,
            max_retries=0,
            backoff_seconds=0,
            _transport=httpx.MockTransport(gateway.handler),
        )
    )
    return JobDispatcher(
        PostgresDispatchQueue(db.pool, kinds=VOICE_GENERATION_JOB_KINDS),
        states or PostgresDispatchStateStore(db.pool),
        _Planner(),
        client,
        ResultArtifactPublisher(
            PostgresArtifactWriter(db.pool),
            _Blobs(),
            _Staging(),
            now_us=lambda: CLOCK_US,
        ),
        limits=limits or DispatchLimits(poll_seconds=0.01, job_timeout_seconds=60),
        now_us=now_us,
    )


def _voice_draft(db: JobSandbox, kind: str = "design") -> tuple[dict[str, Any], Any]:
    return _provider(db).create(
        "voice_design" if kind == "design" else "voice_clone",
        _body(kind),
        request_id="request-dispatch-" + kind,
        idempotency_key=None,
    )


async def _draft_read(db: JobSandbox, draft_id: str) -> Any:
    app = create_app(settings=_settings(), jobs=db.jobs, voice_drafts=_provider(db))
    async with AsyncClient(transport=ASGITransport(app), base_url="http://localhost") as http:
        return await http.get("/v1/nexus/voice-drafts/" + draft_id)


async def _settle_creation(db: JobSandbox, kind: str = "design") -> dict[str, Any]:
    draft, _job = _voice_draft(db, kind)
    gateway = _Gateway()
    batch = await _dispatcher(db, gateway).run_once("worker-voice-creation")
    assert batch.succeeded == 1, batch
    read = await _draft_read(db, draft["id"])
    assert read.status_code == 200, read.text
    assert read.json()["draft"]["status"] == "candidate"
    return draft


def _job_row(db: JobSandbox, job_id: str) -> dict[str, Any]:
    rows = db.query("SELECT * FROM speech_jobs WHERE job_id=%s", (job_id,))
    assert len(rows) == 1
    return rows[0]


@pytest.mark.parametrize("kind", ["design", "clone"])
async def test_creation_dispatch_publishes_the_draft_preview(
    voice_dispatch_db: JobSandbox, kind: str
) -> None:
    db = voice_dispatch_db
    draft, created = _voice_draft(db, kind)
    reference: Any = "voice_" + kind

    gateway = _Gateway()
    batch = await _dispatcher(db, gateway).run_once("worker-voice-creation")

    assert batch.succeeded == 1 and gateway.submit_calls == 1
    # The public draft projection is the first consumer of the stored result.
    read = await _draft_read(db, draft["id"])
    assert read.status_code == 200, read.text
    stored = _job_row(db, created.job_id)
    assert stored["state"] == "succeeded"
    result = json.loads(stored["result_json"])
    assert result["kind"] == reference
    assert result["voice_draft_id"] == draft["id"]
    assert result["draft_revision"] == "1"
    assert result["preview_artifact_ids"] == [result_artifact_identity(created.job_id, reference)]
    assert result["receipt_id"].startswith("receipt-")
    publication = db.query(
        "SELECT artifact_kind, artifact_state, media_type, receipt_id FROM speech_artifacts "
        "WHERE artifact_id=%s",
        (result["preview_artifact_ids"][0],),
    )
    assert publication == [
        {
            "artifact_kind": "preview",
            "artifact_state": "ready",
            "media_type": "audio/wav",
            "receipt_id": result["receipt_id"],
        }
    ]
    assert read.json()["revision"]["preview_artifact_ids"] == result["preview_artifact_ids"]

    public_job = db.jobs.get(created.job_id)
    assert [fact.kind for fact in db.jobs.artifacts(public_job)] == ["preview"]


async def test_generation_dispatch_names_the_appended_revision(
    voice_dispatch_db: JobSandbox,
) -> None:
    db = voice_dispatch_db
    draft = await _settle_creation(db)
    generated, jobs = _provider(db).generate(
        draft["id"],
        {
            "expected_draft_revision": draft["head_revision"],
            "model": "voice-model",
            "input": _body("design")["input"],
        },
        request_id="request-dispatch-generation",
        idempotency_key=None,
    )
    assert generated["revision"]["revision"] == "2"
    # The request targeted head revision 1; the appended revision is 2, and the
    # draft projection compares the success result with the appended revision.
    assert draft["head_revision"] == "1"

    gateway = _Gateway()
    batch = await _dispatcher(db, gateway).run_once("worker-voice-generation")

    assert batch.succeeded == 1
    result = json.loads(_job_row(db, jobs.job_id)["result_json"])
    assert result["voice_draft_id"] == draft["id"]
    assert result["draft_revision"] == "2"
    read = await _draft_read(db, draft["id"])
    assert read.status_code == 200, read.text
    assert read.json()["revision"]["revision"] == "2"
    assert read.json()["revision"]["status"] == "candidate"


async def test_engine_submit_rejection_fails_the_job_with_its_code(
    voice_dispatch_db: JobSandbox,
) -> None:
    db = voice_dispatch_db
    draft, created = _voice_draft(db)

    batch = await _dispatcher(
        db, _Gateway(submit_rejection=(422, "policy_or_resource_disabled"))
    ).run_once("worker-voice-rejection")

    assert batch.failed == 1 and batch.unknown == 0
    stored = _job_row(db, created.job_id)
    assert stored["state"] == "failed"
    assert stored["error_code"] == "policy_or_resource_disabled"
    read = await _draft_read(db, draft["id"])
    assert read.status_code == 200, read.text
    assert read.json()["revision"]["status"] == "failed"
    assert read.json()["revision"]["failure"]["code"] == "policy_or_resource_disabled"


async def test_engine_reported_timeout_is_projected_failed(
    voice_dispatch_db: JobSandbox,
) -> None:
    db = voice_dispatch_db
    draft, created = _voice_draft(db)

    batch = await _dispatcher(
        db, _Gateway(states=("failed",), engine_error="voice_generation_timeout")
    ).run_once("worker-voice-engine-timeout")

    assert batch.failed == 1 and batch.unknown == 0
    assert _job_row(db, created.job_id)["error_code"] == "voice_generation_timeout"
    read = await _draft_read(db, draft["id"])
    assert read.status_code == 200, read.text
    assert read.json()["revision"]["failure"]["code"] == "voice_generation_timeout"


async def test_dispatcher_timeout_leaves_the_job_running_and_unknown(
    voice_dispatch_db: JobSandbox,
) -> None:
    db = voice_dispatch_db
    draft, created = _voice_draft(db)
    gateway = _Gateway()

    batch = await _dispatcher(
        db,
        gateway,
        now_us=lambda: time.time_ns() // 1_000 + 7_200_000_000,
    ).run_once("worker-voice-timeout")

    assert batch.unknown == 1 and batch.failed == 0
    assert gateway.submit_calls == 0
    stored = _job_row(db, created.job_id)
    assert stored["state"] == "running"
    assert stored["error_code"] is None
    pending = db.query("SELECT job_state FROM coordination_jobs WHERE job_id=%s", (created.job_id,))
    assert pending == [{"job_state": "pending"}]
    read = await _draft_read(db, draft["id"])
    assert read.status_code == 200, read.text
    assert read.json()["revision"]["status"] == "generating"


class _CancelBeforeSubmitStore(PostgresDispatchStateStore):
    """Accept a cancellation after mark_running, before any gateway submit."""

    def __init__(self, pool: Any, *, admin: Any) -> None:
        super().__init__(pool)
        self._admin, self._cancelled = admin, False

    async def reload(self, job_id: str) -> DispatchJob:
        job = await super().reload(job_id)
        if not self._cancelled and job.state == "running":
            self._cancelled = True
            self._admin.execute(
                "UPDATE speech_jobs SET state='cancelling', "
                "status_version=status_version+1 WHERE job_id=%s AND state='running'",
                (job_id,),
            )
            job = await super().reload(job_id)
        return job


async def test_cancel_before_submit_records_no_execution(
    voice_dispatch_db: JobSandbox,
) -> None:
    db = voice_dispatch_db
    draft, created = _voice_draft(db)
    db.admin.execute(
        "INSERT INTO speech_job_cancellation_fences "
        "(job_id, accepted_at_us, request_id) VALUES (%s, 1, 'request-cancel')",
        (created.job_id,),
    )
    gateway = _Gateway()
    states = _CancelBeforeSubmitStore(db.pool, admin=db.admin)

    batch = await _dispatcher(db, gateway, states=states).run_once("worker-voice-cancel")

    assert batch.lease_lost == 1 and batch.failed == 0
    assert gateway.submit_calls == 0
    observed = db.query(
        f"SELECT job_id, observer FROM {NOT_SUBMITTED_TABLE} WHERE job_id=%s",
        (created.job_id,),
    )
    assert observed == [{"job_id": created.job_id, "observer": "worker-voice-cancel"}]

    # Settlement reads the real reconciliation schedule: the durable
    # no-execution fact is what lets a later pass close cancelling without
    # pretending an engine stop was observed.
    reconciliation = PostgresCancellationReconciliationStore(db.pool)
    now = time.time_ns() // 1_000 + 1_000_000
    candidates = reconciliation.claim_due(
        now_us=now, limit=8, lease_until_us=now + 30_000_000, accepted_before_us=now
    )
    assert [candidate.job_id for candidate in candidates] == [created.job_id]
    assert candidates[0].not_submitted_at_us is not None
    current = db.jobs.get(created.job_id)
    assert current.state == "cancelling"
    settled = db.jobs.settle_cancelled(created.job_id, expected_version=current.status_version)
    assert settled is not None and settled.state == "cancelled"
    reconciliation.record_settled(created.job_id, now_us=now + 1)

    assert _job_row(db, created.job_id)["state"] == "cancelled"
    read = await _draft_read(db, draft["id"])
    assert read.status_code == 200, read.text
    revision = read.json()["revision"]
    assert revision["status"] == "failed"
    assert revision["failure"]["code"] == "voice_generation_cancelled"
    assert revision["preview_artifact_ids"] == []
    assert db.query("SELECT count(*) AS n FROM speech_artifacts") == [{"n": 0}]


async def test_default_and_voice_lanes_do_not_cross_claim(
    voice_dispatch_db: JobSandbox,
) -> None:
    db = voice_dispatch_db
    tts = db.jobs.submit(
        {
            "kind": "tts",
            "model": "tts-quality",
            "input": {"text": "synthetic"},
            "voice": {"id": "voice-vivian", "revision": "2"},
        },
        request_id="request-lane-tts",
        idempotency_key=None,
    )
    _draft, created = _voice_draft(db)

    default_claims = await PostgresDispatchQueue(db.pool).claim_due("worker-default", 30_000, 10)
    voice_claims = await PostgresDispatchQueue(db.pool, kinds=VOICE_GENERATION_JOB_KINDS).claim_due(
        "worker-voice", 30_000, 10
    )

    assert [claim.job.kind for claim in default_claims] == ["tts"]
    assert [claim.job.job_id for claim in default_claims] == [tts.job_id]
    assert [claim.job.kind for claim in voice_claims] == ["voice_design"]
    assert [claim.job.job_id for claim in voice_claims] == [created.job_id]
    pending = db.query(
        "SELECT job_id, job_state FROM coordination_jobs WHERE job_id=%s",
        (created.job_id,),
    )
    assert pending == [{"job_id": created.job_id, "job_state": "pending"}]


async def test_lease_takeover_on_a_voice_job(voice_dispatch_db: JobSandbox) -> None:
    db = voice_dispatch_db
    _draft, created = _voice_draft(db)
    queue = PostgresDispatchQueue(db.pool, kinds=VOICE_GENERATION_JOB_KINDS)
    states = PostgresDispatchStateStore(db.pool)

    first_claims = await queue.claim_due("worker-voice-a", 30_000, 1)
    assert len(first_claims) == 1
    first = first_claims[0]
    await states.mark_running(first.job)

    db.query(
        "UPDATE coordination_jobs SET claim_expires_at_us = 0 "
        "WHERE job_id = %s AND claim_version = %s",
        (created.job_id, first.claim_version),
    )
    second_claims = await queue.claim_due("worker-voice-b", 30_000, 1)
    assert len(second_claims) == 1
    second = second_claims[0]
    assert second.claim_version == 2 and second.job.state == "running"

    assert await states.renew(first, 30_000) is None
    renewed = await states.renew(second, 30_000)
    assert renewed is not None
    result_json = terminal_result(second.job, "preview-synthetic", "receipt-synthetic")
    terminal = await states.finish(renewed, "succeeded", result_json=result_json, error_code=None)
    assert terminal.state == "succeeded"

    with pytest.raises(DispatchClaimLostError):
        await states.finish(first, "succeeded", result_json=result_json, error_code=None)


async def test_repeated_voice_publication_is_one_row_and_one_version(
    voice_dispatch_db: JobSandbox,
) -> None:
    db = voice_dispatch_db
    _draft, _created = _voice_draft(db)
    claims = await PostgresDispatchQueue(db.pool, kinds=VOICE_GENERATION_JOB_KINDS).claim_due(
        "worker-voice-publish", 30_000, 1
    )
    assert len(claims) == 1
    job = claims[0].job
    blobs, staging = _Blobs(), _Staging()
    publisher = ResultArtifactPublisher(
        PostgresArtifactWriter(db.pool), blobs, staging, now_us=lambda: CLOCK_US
    )

    first = await publisher.publish(
        job,
        ExecutionSubmission("submit-synthetic", {"submit_intent_id": "submit-synthetic"}),
        _receipt(),
    )
    second = await publisher.publish(
        job,
        ExecutionSubmission("submit-synthetic", {"submit_intent_id": "submit-synthetic"}),
        _receipt(),
    )

    assert first == second
    assert blobs.writes == 1 and staging.reads == 1
    rows = db.query(
        "SELECT artifact_kind, artifact_state, receipt_id FROM speech_artifacts "
        "WHERE artifact_id=%s",
        (first.artifact_id,),
    )
    assert rows == [
        {
            "artifact_kind": "preview",
            "artifact_state": "ready",
            "receipt_id": first.receipt_id,
        }
    ]


async def test_preview_and_validation_jobs_are_not_claimed(
    voice_dispatch_db: JobSandbox,
) -> None:
    db = voice_dispatch_db
    draft = await _settle_creation(db)
    provider = _provider(db)
    revision = draft["head_revision"]
    preview = provider.preview(
        draft["id"],
        {"expected_draft_revision": revision, "text": "synthetic preview"},
        request_id="request-lane-preview",
        idempotency_key=None,
    )
    validation = provider.validate(
        draft["id"],
        {"expected_draft_revision": revision, "validation_set_revision": "1"},
        request_id="request-lane-validation",
        idempotency_key=None,
    )
    assert preview.kind == "voice_preview" and validation.kind == "voice_validation"

    assert await PostgresDispatchQueue(db.pool).claim_due("worker-default", 30_000, 10) == []
    assert (
        await PostgresDispatchQueue(db.pool, kinds=VOICE_GENERATION_JOB_KINDS).claim_due(
            "worker-voice", 30_000, 10
        )
        == []
    )
    pending = db.query(
        "SELECT job_id, job_state FROM coordination_jobs WHERE job_id IN (%s, %s) ORDER BY job_id",
        (preview.job_id, validation.job_id),
    )
    assert pending == [
        {"job_id": min(preview.job_id, validation.job_id), "job_state": "pending"},
        {"job_id": max(preview.job_id, validation.job_id), "job_state": "pending"},
    ]
