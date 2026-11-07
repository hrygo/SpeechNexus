"""Real PG validation lane: admission, stored-command dispatch, report and evidence.

The only fakes are the gateway transport, the staging bucket and the Artifact
blob store. Admission, the claim, the stored-command planner, the terminal
write, LeaseService release, the report Artifact and the public GET all run on
real PostgreSQL rows.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import wave
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from typing import Any, cast

import pytest
from httpx import ASGITransport, AsyncClient
from tests.integration.test_coordination_foundation import foundation as foundation
from tests.integration.test_production_admission import AdmissionDatabase
from tests.integration.test_production_admission import admission_database as admission_database
from tests.integration.test_production_jobs import database as database
from tests.integration.test_production_runtime_intents import runtime_database as runtime_database
from tests.integration.test_public_job_atomic_admission_pg import (
    BINDING,
    DIGEST,
    _seed,
    _store,
)
from tests.unit.backend.test_health import _settings

from speechnexus_backend.app import create_app
from speechnexus_backend.speech.application.job_dispatcher import JobDispatcher
from speechnexus_backend.speech.application.public_jobs import PublicJobs
from speechnexus_backend.speech.application.results import ResultArtifactPublisher
from speechnexus_backend.speech.application.voice_drafts import VoiceDrafts
from speechnexus_backend.speech.application.voice_publications import (
    VoicePublicationNotEligibleError,
    VoicePublications,
)
from speechnexus_backend.speech.domain.artifacts import ArtifactObjectWrite
from speechnexus_backend.speech.domain.job_dispatch import (
    VOICE_VALIDATION_JOB_KINDS,
    DispatchLimits,
    ExecutionSubmission,
    GatewayReply,
    ResultReceipt,
    result_receipt_identity,
)
from speechnexus_backend.speech.infrastructure.artifacts_postgres import PostgresArtifactWriter
from speechnexus_backend.speech.infrastructure.job_dispatch_postgres import (
    PostgresDispatchQueue,
    PostgresDispatchStateStore,
    PostgresLeaseSettler,
)
from speechnexus_backend.speech.infrastructure.public_jobs_postgres import PostgresPublicJobs
from speechnexus_backend.speech.infrastructure.stored_command_planner import (
    StoredCommandPlanner,
)
from speechnexus_backend.speech.infrastructure.voice_capability_evidence_postgres import (
    PostgresVoiceEvidenceRecorder,
)
from speechnexus_backend.speech.infrastructure.voice_draft_schema import voice_draft_grants
from speechnexus_backend.speech.infrastructure.voice_drafts_postgres import PostgresVoiceDrafts
from speechnexus_backend.speech.infrastructure.voice_publication_schema import (
    voice_evidence_recorder_grants,
    voice_publication_grants,
)
from speechnexus_backend.speech.infrastructure.voice_publications_postgres import (
    PostgresVoicePublications,
)
from speechnexus_backend.speech.infrastructure.voice_validation_run_postgres import (
    PostgresValidationRunFacts,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.parametrize("database", ["postgresql"], indirect=True),
]

VALIDATION_WORKSPACE = "workspace-default"
REFERENCE_ASSET = "synthetic-reference"
REFERENCE_SHA = "sha256:" + "b" * 64
REFERENCE_BYTES = 48_000
GENERATION_PREVIEW = "Generation-stage preview sentence."


def _wav_body(*, frames: int = 12_000) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(24_000)
        audio.writeframes(b"\x01\x00" * frames)
    return buffer.getvalue()


WAV_BODY = _wav_body()


@dataclass
class ValidationSandbox:
    jobs: PublicJobs
    jobs_store: PostgresPublicJobs
    drafts: VoiceDrafts
    publications: VoicePublications
    pool: Any = field(repr=False)
    db: Any = field(repr=False)

    def query(self, statement: str, parameters: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        return cast(list[dict[str, Any]], self.db.query(statement, parameters))


@pytest.fixture
def validation_db(admission_database: AdmissionDatabase) -> Iterator[ValidationSandbox]:
    admission = admission_database
    _seed(admission, complete=True)
    db = admission.runtime.foundation.database
    role = admission.runtime.foundation.speech_role
    schema = db.schema
    for grant in (
        *voice_draft_grants(schema, role),
        *voice_publication_grants(schema, role),
        *voice_evidence_recorder_grants(schema, role),
    ):
        db.query(grant)
    definition = {
        "logical_model": BINDING,
        "tasks": ["tts_file", "asr_file", "voice_clone", "voice_validation"],
        "release_tier": "production",
        "execution_recipe": {
            "recipe_revision": "1",
            "capacity_manifest_id": "manifest-test",
            "stage_roles": ["tts_talker"],
        },
        # Deliberately no ``execution_defaults``: validation must use the
        # server-owned measurement budget rather than a borrowed ASR/TTS block.
    }
    db.query(
        "UPDATE speech_binding_revisions SET definition_json=%s "
        "WHERE binding_id=%s AND revision='1'",
        (json.dumps(definition), BINDING),
    )
    db.query(
        "INSERT INTO workspace_quotas (workspace_id, max_attempts, held_attempts, "
        "policy_revision) VALUES (%s, 2, 1, 'policy-test') "
        "ON CONFLICT (workspace_id) DO UPDATE SET policy_revision=EXCLUDED.policy_revision, "
        "max_attempts=EXCLUDED.max_attempts",
        (VALIDATION_WORKSPACE,),
    )
    db.query(
        "INSERT INTO speech_assets (asset_id, workspace_id, purpose, asset_state, upload_phase, "
        "bytes, sha256, request_digest, object_key, media_type, duration_ms, sample_rate_hz, "
        "channels, created_at_us, expires_at_us) VALUES (%s, %s, 'voice_reference', 'ready', "
        "'settled', %s, %s, %s, 'private/synthetic-reference.wav', 'audio/wav', 1000, 16000, 1, "
        "1, 1000000)",
        (REFERENCE_ASSET, VALIDATION_WORKSPACE, REFERENCE_BYTES, REFERENCE_SHA, DIGEST),
    )
    db.query(
        "INSERT INTO speech_asset_objects "
        "(asset_id, object_key, storage_version_id, bytes, sha256, writer_id, verified_at_us) "
        "VALUES (%s, 'private/synthetic-reference.wav', 'version-reference-1', %s, %s, 'test', 1)",
        (REFERENCE_ASSET, REFERENCE_BYTES, REFERENCE_SHA),
    )
    pool = admission.runtime.pool
    assert pool is not None
    store = _store(admission)
    jobs = PublicJobs(store, store)
    drafts = VoiceDrafts(jobs, PostgresVoiceDrafts(pool))
    publications = VoicePublications(PostgresVoicePublications(pool))
    yield ValidationSandbox(jobs, store, drafts, publications, pool, db)


def _create_generation(sandbox: ValidationSandbox) -> tuple[dict[str, Any], Any, str]:
    draft, job = sandbox.drafts.create(
        "voice_clone",
        {
            "model": BINDING,
            "input": {
                "reference_audio_asset_id": REFERENCE_ASSET,
                "source_use_confirmed": True,
                "preview_text": GENERATION_PREVIEW,
                "reference_text": None,
                "language": "en",
                "display_name": "Synthetic clone",
            },
        },
        request_id="request-create-clone",
        idempotency_key=None,
    )
    artifact_id = "artifact-preview-" + job.job_id.removeprefix("job-")[:32]
    receipt_id = result_receipt_identity(job.job_id)
    sandbox.db.query(
        "INSERT INTO speech_artifacts "
        "(artifact_id, workspace_id, artifact_kind, artifact_state, media_type, bytes, sha256, "
        "receipt_id, source_operation_id, object_key, storage_version_id, created_at_us) "
        "VALUES (%s, %s, 'preview', 'ready', 'audio/wav', 1000, %s, %s, %s, %s, "
        "'version-preview-1', 10)",
        (
            artifact_id,
            VALIDATION_WORKSPACE,
            DIGEST,
            receipt_id,
            job.job_id,
            "private/artifacts/" + VALIDATION_WORKSPACE + "/" + artifact_id,
        ),
    )
    sandbox.jobs.advance(job.job_id, "running")
    sandbox.jobs.advance(
        job.job_id,
        "succeeded",
        result_json=json.dumps(
            {
                "kind": "voice_clone",
                "voice_draft_id": draft["id"],
                "draft_revision": draft["head_revision"],
                "preview_artifact_ids": [artifact_id],
                "receipt_id": receipt_id,
            }
        ),
    )
    return draft, job, artifact_id


class _Staging:
    def read(self, reference: Mapping[str, Any], storage_version: str) -> bytes:
        assert storage_version == "version-staging-validation"
        assert reference["byte_length"] == len(WAV_BODY)
        return WAV_BODY


class _Blobs:
    def __init__(self) -> None:
        self.bodies: dict[str, bytes] = {}

    def check_writes_available(self) -> None:
        return None

    def put_verified(self, write: ArtifactObjectWrite) -> str:
        self.bodies[write.artifact_id] = write.body
        return "version-report-1"


class _Gateway:
    def __init__(self) -> None:
        self.submitted: ExecutionSubmission | None = None
        self.receipt_reads = 0

    async def query(self, submit_intent_id: str) -> GatewayReply:
        if self.submitted is None:
            return GatewayReply("not_found")
        return GatewayReply(
            "status",
            {
                "submit_intent_id": submit_intent_id,
                "command_digest": self.submitted.body["command_digest"],
                "execution_state": "succeeded",
            },
        )

    async def submit(self, submission: ExecutionSubmission) -> GatewayReply:
        self.submitted = submission
        return GatewayReply(
            "status",
            {
                "submit_intent_id": submission.submit_intent_id,
                "command_digest": submission.body["command_digest"],
                "execution_state": "succeeded",
            },
        )

    async def result_receipt(self, submit_intent_id: str, *, audio: bool) -> ResultReceipt:
        assert audio is True
        self.receipt_reads += 1
        return ResultReceipt(
            "status",
            {
                "request_digest": DIGEST,
                "reference": {
                    "asset_or_artifact_id": "staging-validation",
                    "workspace_id": VALIDATION_WORKSPACE,
                    "storage_bucket": "synthetic-results",
                    "object_key": "staging/validation.wav",
                    "content_sha256": "sha256:" + hashlib.sha256(WAV_BODY).hexdigest(),
                    "byte_length": len(WAV_BODY),
                },
                "storage_version": "version-staging-validation",
                "media_type": "audio/wav",
            },
        )


def _dispatcher(sandbox: ValidationSandbox, gateway: _Gateway) -> tuple[JobDispatcher, _Blobs]:
    pool = sandbox.pool
    blobs = _Blobs()
    publisher = ResultArtifactPublisher(
        PostgresArtifactWriter(pool),
        blobs,
        _Staging(),
        validation=PostgresValidationRunFacts(pool),
    )
    dispatcher = JobDispatcher(
        PostgresDispatchQueue(
            pool,
            kinds=VOICE_VALIDATION_JOB_KINDS,
            require_admission=True,
        ),
        PostgresDispatchStateStore(pool),
        StoredCommandPlanner(pool),
        gateway,
        publisher,
        limits=DispatchLimits(batch_size=1, lease_ms=30_000, job_timeout_seconds=600),
    )
    dispatcher.settle_capacity_through(PostgresLeaseSettler(pool))
    return dispatcher, blobs


def test_validation_job_is_admitted_dispatched_and_commits_a_report(
    validation_db: ValidationSandbox,
) -> None:
    sandbox = validation_db
    draft, _generation, _preview = _create_generation(sandbox)
    before_quota = sandbox.query(
        "SELECT held_attempts, version FROM workspace_quotas WHERE workspace_id=%s",
        (VALIDATION_WORKSPACE,),
    )[0]
    before_host = sandbox.query(
        "SELECT held_unified_physical_memory_bytes AS held, version FROM host_nodes"
    )[0]

    validation = sandbox.drafts.validate(
        draft["id"],
        {"expected_draft_revision": draft["head_revision"], "validation_set_revision": "1"},
        request_id="request-validation",
        idempotency_key=None,
    )
    admitted = sandbox.query(
        "SELECT a.binding_revision_id, a.manifest_id, a.policy_revision_id, "
        "l.lease_id, l.deadline_at_us, o.request_document "
        "FROM admission_attempts AS a "
        "JOIN admission_leases AS l ON l.operation_id=a.operation_id "
        "AND l.attempt_id=a.attempt_id "
        "JOIN admission_operations AS o ON o.operation_id=a.operation_id "
        "WHERE a.operation_id=%s",
        (validation.job_id,),
    )
    assert len(admitted) == 1
    row = admitted[0]
    assert row["binding_revision_id"] == BINDING
    assert row["manifest_id"] == "manifest-test"
    assert row["policy_revision_id"] == "policy-test"
    # The deadline comes from the server constant, not a binding block: the
    # binding definition above deliberately has no execution_defaults.
    offset = row["deadline_at_us"] - validation.created_at_us
    assert 595_000_000 <= offset <= 605_000_000
    operation = json.loads(row["request_document"])
    command = operation["gateway_request"]["command"]
    assert command["kind"] == "voiceclone"
    assert command["preview_text"] != GENERATION_PREVIEW
    assert command["task_kind"] == "voice_clone"
    assert operation["gateway_request"]["sample_rate_hz"] == 24_000
    assert operation["gateway_request"]["max_output_audio_ms"] == 60_000
    assert operation["gateway_request"]["voice_revision_id"] is None

    gateway = _Gateway()
    dispatcher, blobs = _dispatcher(sandbox, gateway)
    batch = asyncio.run(dispatcher.run_once("worker-validation-1"))
    assert batch.succeeded == 1
    assert gateway.submitted is not None
    assert gateway.submitted.body["command"]["preview_text"] == command["preview_text"]

    job = sandbox.jobs.get(validation.job_id)
    assert job.state == "succeeded"
    result = json.loads(job.result_json or "{}")
    assert result["kind"] == "voice_validation"
    assert result["validation"]["status"] == "passed"
    assert result["validation"]["publication_eligible"] is True
    assert result["validation"]["draft_id"] == draft["id"]
    assert all(
        set(check) == {"name", "required", "status"} for check in result["validation"]["checks"]
    )
    artifact_id = result["validation_report_artifact_id"]
    artifact = sandbox.jobs.artifacts(job)
    assert [(item.artifact_id, item.kind) for item in artifact] == [
        (artifact_id, "validation_report")
    ]
    assert artifact[0].receipt_id == result["receipt_id"]

    report_bytes = blobs.bodies[artifact_id]
    report_document = json.loads(report_bytes)
    probe = report_document["coverage"]["probes"][0]
    assert probe["name"] == "new_text_reuse"
    assert probe["text"] != GENERATION_PREVIEW
    assert probe["generation_preview_text_sha256"] == (
        "sha256:" + hashlib.sha256(GENERATION_PREVIEW.encode()).hexdigest()
    )
    assert report_document["capabilities"] == {
        "languages": ["en"],
        "sample_rates_hz": [24_000],
        "audio_formats": ["wav"],
        "modes": ["complete"],
    }
    assert report_document["material"]["asset_id"] == REFERENCE_ASSET
    assert probe["output_bytes"] == len(WAV_BODY)
    assert probe["audio_duration_ms"] == 500

    app = create_app(
        settings=_settings(), jobs=sandbox.jobs, voice_publications=sandbox.publications
    )

    async def _get() -> Any:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://127.0.0.1"
        ) as client:
            return await client.get("/v1/nexus/jobs/" + validation.job_id)

    response = asyncio.run(_get())
    assert response.status_code == 200, response.text
    wire = response.json()
    assert wire["status"] == "succeeded"
    assert [(item["artifact_id"], item["kind"]) for item in wire["artifacts"]] == [
        (artifact_id, "validation_report")
    ]

    async def _publish() -> Any:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://127.0.0.1"
        ) as client:
            return await client.post(
                f"/v1/nexus/voice-drafts/{draft['id']}/publications",
                json={
                    "expected_draft_revision": draft["head_revision"],
                    "validation_job_id": validation.job_id,
                    "manual_confirmation": True,
                    "display_name": "Synthetic clone",
                },
            )

    publication = asyncio.run(_publish())
    assert publication.status_code == 422, publication.text
    assert publication.json()["error"]["code"] == "voice_publication_not_eligible"
    with pytest.raises(VoicePublicationNotEligibleError, match="evidence_missing"):
        sandbox.publications.publish(
            draft["id"],
            {
                "expected_draft_revision": draft["head_revision"],
                "validation_job_id": validation.job_id,
                "manual_confirmation": True,
                "display_name": "Synthetic clone",
            },
            request_id="request-publish-no-evidence",
            idempotency_key=None,
        )

    # The report is consumable by the existing recorder: its probes survive into
    # the evidence ledger. This is a test fixture, not production auto-recording.
    evidence, created = PostgresVoiceEvidenceRecorder(sandbox.pool).record(
        validation.job_id, report_bytes, "pytest/recorder"
    )
    assert created is True
    assert evidence.capabilities["languages"] == ["en"]
    assert evidence.coverage["probes"] == [probe]

    lease = sandbox.query(
        "SELECT lease_state, cleanup_state FROM admission_leases WHERE operation_id=%s",
        (validation.job_id,),
    )[0]
    assert lease == {"lease_state": "released", "cleanup_state": "complete"}
    after_quota = sandbox.query(
        "SELECT held_attempts, version FROM workspace_quotas WHERE workspace_id=%s",
        (VALIDATION_WORKSPACE,),
    )[0]
    after_host = sandbox.query(
        "SELECT held_unified_physical_memory_bytes AS held, version FROM host_nodes"
    )[0]
    assert after_quota["held_attempts"] == before_quota["held_attempts"]
    assert after_host["held"] == before_host["held"]
