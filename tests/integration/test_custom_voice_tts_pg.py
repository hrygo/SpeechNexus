"""A published clone voice is assembled as custom Base material for a real TTS Job."""

from __future__ import annotations

import hashlib
import io
import json
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any

import pytest
from tests.fake.file_voice_clone_configuration import reference_wav
from tests.integration.test_coordination_foundation import foundation as foundation
from tests.integration.test_production_admission import AdmissionDatabase
from tests.integration.test_production_admission import admission_database as admission_database
from tests.integration.test_production_jobs import database as database
from tests.integration.test_production_runtime_intents import runtime_database as runtime_database
from tests.integration.test_public_job_atomic_admission_pg import (
    BINDING,
    WORKSPACE,
    _seed,
    _store,
)

from speechnexus_backend.speech.application.voice_publications import PublicationRequest
from speechnexus_backend.speech.application.voice_validation_report import (
    ValidationRunFacts,
    WaveFacts,
    build_validation_report,
    build_validation_result,
    validation_outcome,
)
from speechnexus_backend.speech.domain.public_jobs import (
    JobSubmission,
    NewPublicJob,
    canonical_digest,
    idempotency_digest,
)
from speechnexus_backend.speech.domain.voice_capability_evidence import VoiceMaterial
from speechnexus_backend.speech.infrastructure.voice_capability_evidence_postgres import (
    PostgresVoiceEvidenceRecorder,
)
from speechnexus_backend.speech.infrastructure.voice_publication_schema import (
    voice_evidence_recorder_grants,
    voice_publication_grants,
)
from speechnexus_backend.speech.infrastructure.voice_publications_postgres import (
    PostgresVoicePublications,
)
from speechnexus_gateway.application.execution_inputs import FileExecutionInputs
from speechnexus_gateway.domain.execution_inputs import (
    ExecutionInputLimits,
    ExecutionObjectReference,
    VerifiedExecutionObject,
)
from speechnexus_gateway.domain.execution_results import ExecutionAudioStagingResult
from speechnexus_gateway.domain.submission import SubmissionIdentity, SubmitRequest
from speechnexus_gateway.domain.tts import TtsAudio
from speechnexus_gateway.domain.tts_results import PreparedTtsResult
from speechnexus_gateway.domain.voice_clone import (
    VoiceCloneExecutionTarget,
    VoiceClonePolicy,
    VoiceCloneRequest,
)
from speechnexus_gateway.infrastructure.local_tts_encoder import LocalTtsEncoder
from speechnexus_gateway.interfaces.file_tts_command import CommandFileCustomTts

pytestmark = [
    pytest.mark.integration,
    pytest.mark.parametrize("database", ["postgresql"], indirect=True),
]

REFERENCE_TEXT = "Synthetic warmup reference."
REFERENCE_PAYLOAD = reference_wav()
REFERENCE_SHA = "sha256:" + hashlib.sha256(REFERENCE_PAYLOAD).hexdigest()
PCM = b"\x01\x00" * 2400
PROBE_TEXT = "A validation-only sentence."
NOW_US = 1_800_000_000_000_000


def _digest(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _grants(admission: AdmissionDatabase) -> None:
    db = admission.runtime.foundation.database
    role = admission.runtime.foundation.speech_role
    for grant in (
        *voice_evidence_recorder_grants(db.schema, role),
        *voice_publication_grants(db.schema, role),
    ):
        db.query(grant)


def _material() -> VoiceMaterial:
    return VoiceMaterial(
        asset_id="asset-custom-pg",
        sha256=REFERENCE_SHA,
        bytes=len(REFERENCE_PAYLOAD),
        media_type="audio/wav",
    )


def _publication(admission: AdmissionDatabase) -> dict[str, Any]:
    """Publish a clone revision through the real recorder/ledger path."""
    _grants(admission)
    db = admission.runtime.foundation.database
    pool = admission.runtime.pool
    assert pool is not None
    asset = _material()
    db.query(
        "INSERT INTO speech_assets "
        "(asset_id, workspace_id, purpose, asset_state, upload_phase, bytes, sha256, "
        "request_digest, object_key, media_type, duration_ms, sample_rate_hz, channels, "
        "created_at_us, expires_at_us) "
        "VALUES (%s,%s,'voice_reference','ready','settled',%s,%s,%s,%s,'audio/wav',1500,16000,1,"
        "%s,%s)",
        (
            asset.asset_id,
            WORKSPACE,
            asset.bytes,
            asset.sha256,
            "sha256:" + "c" * 64,
            "private/references/" + asset.asset_id,
            NOW_US - 1000,
            NOW_US + 86_400_000_000,
        ),
    )
    db.query(
        "INSERT INTO speech_asset_objects "
        "(asset_id, object_key, storage_version_id, bytes, sha256, writer_id, verified_at_us) "
        "VALUES (%s,%s,'version-1',%s,%s,'pytest',%s)",
        (
            asset.asset_id,
            "private/references/" + asset.asset_id,
            asset.bytes,
            asset.sha256,
            NOW_US - 1000,
        ),
    )
    generation_job = "job-generation-pg"
    db.query(
        "INSERT INTO speech_jobs "
        "(job_id, workspace_id, kind, model_id, request_digest, state, status_version, "
        "result_json, created_at_us, updated_at_us) "
        "VALUES (%s,%s,'voice_clone','voice-model',%s,'succeeded',1,'{\"kind\":\"voice_clone\"}',"
        "%s,%s)",
        (generation_job, WORKSPACE, "sha256:" + "d" * 64, NOW_US - 1000, NOW_US - 1000),
    )
    draft_id = "draft-custom-pg"
    draft_input = {
        "reference_audio_asset_id": asset.asset_id,
        "reference_text": REFERENCE_TEXT,
        "language": "en",
        "source_use_confirmed": True,
    }
    binding_json = {"id": BINDING, "revision": "1"}
    db.query(
        "INSERT INTO speech_voice_drafts "
        "(draft_id, generation_job_id, kind, status, head_revision, metadata_revision, "
        "display_name, input_json, binding_json, created_at_us, updated_at_us) "
        "VALUES (%s,%s,'clone','validated','1',1,'Synthetic clone',%s,%s,%s,%s)",
        (
            draft_id,
            generation_job,
            json.dumps(draft_input),
            json.dumps(binding_json),
            NOW_US - 1000,
            NOW_US - 1000,
        ),
    )
    db.query(
        "INSERT INTO speech_voice_draft_revisions "
        "(draft_id, revision, kind, generation_job_id, binding_json, input_json, created_at_us) "
        "VALUES (%s,'1','clone',%s,%s,%s,%s)",
        (
            draft_id,
            generation_job,
            json.dumps(binding_json),
            json.dumps(draft_input),
            NOW_US - 1000,
        ),
    )
    facts = ValidationRunFacts(
        draft_id=draft_id,
        draft_revision="1",
        validation_set_revision="1",
        binding_id=BINDING,
        binding_revision="1",
        generation_preview_text="Generation preview text.",
        material=asset,
        reference_text=REFERENCE_TEXT,
    )
    report = build_validation_report(
        facts,
        probe_text=PROBE_TEXT,
        measured=WaveFacts(
            sample_rate_hz=24000,
            channels=1,
            duration_ms=500,
            bytes=24044,
            sha256="sha256:" + "e" * 64,
            frames=12000,
            rms=2000,
            peak=12000,
        ),
    )
    checks, eligible = validation_outcome(report)
    assert eligible
    validation_job = "job-validation-pg"
    artifact_id = "report-validation-pg"
    receipt = "receipt-validation-pg"
    result = build_validation_result(
        facts,
        job_id=validation_job,
        artifact_id=artifact_id,
        receipt_id=receipt,
        created_at_us=NOW_US - 1000,
        checks=checks,
        publication_eligible=True,
    )
    db.query(
        "INSERT INTO speech_jobs "
        "(job_id, workspace_id, kind, model_id, request_digest, state, status_version, "
        "result_json, created_at_us, updated_at_us) "
        "VALUES (%s,%s,'voice_validation','voice-model',%s,'succeeded',1,%s,%s,%s)",
        (
            validation_job,
            WORKSPACE,
            "sha256:" + "f" * 64,
            result,
            NOW_US - 1000,
            NOW_US - 1000,
        ),
    )
    db.query(
        "INSERT INTO speech_artifacts "
        "(artifact_id, workspace_id, artifact_kind, artifact_state, media_type, bytes, sha256, "
        "receipt_id, source_operation_id, object_key, storage_version_id, created_at_us) "
        "VALUES (%s,%s,'validation_report','ready','application/json',%s,%s,%s,NULL,%s,"
        "'version-1',%s)",
        (
            artifact_id,
            WORKSPACE,
            len(report),
            _digest(report),
            receipt,
            "private/artifacts/" + artifact_id,
            NOW_US - 1000,
        ),
    )
    recorder = PostgresVoiceEvidenceRecorder(pool)
    evidence, created = recorder.record(validation_job, report, "pytest/recorder")
    assert created and evidence.material is not None
    publisher = PostgresVoicePublications(pool)
    body, published = publisher.publish(
        draft_id,
        PublicationRequest(
            expected_draft_revision="1",
            validation_job_id=validation_job,
            manual_confirmation=True,
            display_name="Synthetic clone",
        ),
        request_digest="sha256:" + "1" * 64,
        key_digest=None,
    )
    assert published is True
    assert body["validation_job_id"] == validation_job
    return body


def _custom_tts_job(voice_id: str, revision: str) -> NewPublicJob:
    job_id = "job-custom-tts-pg"
    intent: dict[str, Any] = {
        "workspace_id": WORKSPACE,
        "kind": "tts",
        "model": BINDING,
        "input": {"text": "Custom narration."},
        "output": {"audio_format": "mp3", "save_artifacts": True},
        "constraints": {"allow_degradation": False, "require_exact_parameters": False},
        "voice": {"id": voice_id, "revision": int(revision)},
    }
    return NewPublicJob(
        job_id=job_id,
        workspace_id=WORKSPACE,
        kind="tts",
        model_id=BINDING,
        request_digest=canonical_digest(intent),
        request_document=json.dumps(intent),
        initial_event_digest=canonical_digest({"job_id": job_id, "event_seq": 0}),
        idempotency_key_digest=idempotency_digest("custom-voice-pg"),
        submission=JobSubmission(
            request_id="request-" + job_id,
            client_reference=None,
            effective_configuration_json=json.dumps(
                {
                    "models": {
                        "tts": {"id": BINDING, "revision": "1"},
                        "voice": {"id": voice_id, "revision": int(revision)},
                    }
                }
            ),
            execution_revision=1,
            progress_unit="text_codepoints",
            progress_total=18,
            submitted_at_us=NOW_US,
        ),
    )


class Objects:
    def __init__(self) -> None:
        self.reads: list[ExecutionObjectReference] = []

    @asynccontextmanager
    async def open_verified(
        self,
        reference: ExecutionObjectReference,
        *,
        limits: ExecutionInputLimits,
        cancelled: Callable[[], bool],
    ) -> AsyncIterator[VerifiedExecutionObject]:
        assert not cancelled()
        self.reads.append(reference)
        with io.BytesIO(REFERENCE_PAYLOAD) as stream:
            yield VerifiedExecutionObject(reference, stream, "version-1")


class Synthesizer:
    def __init__(self, target: VoiceCloneExecutionTarget) -> None:
        self.binding_revision_id = target.binding_revision_id
        self.target = target
        self.calls: list[tuple[VoiceCloneRequest, VoiceClonePolicy]] = []

    def validate_execution_target(self, target: VoiceCloneExecutionTarget) -> None:
        if target != self.target:
            raise ValueError("unexpected target")

    async def synthesize(
        self,
        request: VoiceCloneRequest,
        policy: VoiceClonePolicy,
        *,
        cancelled: Callable[[], bool],
    ) -> TtsAudio:
        assert not cancelled()
        self.calls.append((request, policy))
        return TtsAudio(
            request.request_id, "audio/wav", PCM, policy.sample_rate_hz, 1, policy.digest
        )

    async def warmup(self) -> None:
        raise AssertionError("no warmup")


class Store:
    def __init__(self) -> None:
        self.results: list[PreparedTtsResult] = []

    async def stage(
        self, result: PreparedTtsResult, *, cancelled: Callable[[], bool]
    ) -> ExecutionAudioStagingResult:
        assert not cancelled()
        self.results.append(result)
        return result.receipt("sn-custom", "version-1")


def _submit_request(gateway: dict[str, Any]) -> SubmitRequest:
    identity = SubmissionIdentity(
        **{
            key: gateway[key]
            for key in (
                "submit_intent_id",
                "lease_id",
                "operation_id",
                "attempt_id",
                "stage_id",
                "workspace_id",
                "pool_id",
                "host_node_id",
                "gateway_id",
                "instance_id",
                "instance_generation",
                "binding_revision_id",
                "policy_revision_id",
                "voice_revision_id",
                "fencing_token",
                "command_digest",
                "max_output_audio_ms",
                "sample_rate_hz",
            )
        },
        deadline_at=datetime.fromisoformat(gateway["deadline_at"].replace("Z", "+00:00")),
    )
    return SubmitRequest(
        identity=identity,
        command=gateway["command"],
        output_stream_id="stream-custom-tts",
    )


async def test_published_clone_is_assembled_and_executes_on_the_base_lane(
    admission_database: AdmissionDatabase,
) -> None:
    _seed(admission_database)
    publication = _publication(admission_database)
    voice_id = publication["voice_id"]
    revision = publication["voice_revision"]
    db = admission_database.runtime.foundation.database

    result = _store(admission_database).create(_custom_tts_job(voice_id, revision))
    assert result.outcome == "created" and result.job is not None, result.error_code
    document = json.loads(
        db.query("SELECT request_document FROM admission_operations")[0]["request_document"]
    )
    gateway = document["gateway_request"]
    command = gateway["command"]
    assert command["resources"]["voice_kind"] == "custom"
    assert command["resources"]["voice"] == {"id": voice_id, "revision": revision}
    assert command["resources"]["voice_material"] == {
        "asset_or_artifact_id": "asset-custom-pg",
        "workspace_id": WORKSPACE,
        "storage_bucket": "transcription-inputs",
        "object_key": "private/references/asset-custom-pg",
        "content_sha256": REFERENCE_SHA,
        "byte_length": len(REFERENCE_PAYLOAD),
    }
    assert command["parameters"]["reference_text"] == REFERENCE_TEXT
    assert command["parameters"]["instructions"] is None
    assert command["parameters"]["speed"] == 1

    # The fake consumer is the Base clone lane, whose approved pool is
    # ``voice-creation``. Production must schedule this binding's ready Base
    # instance in that pool; the admission fixture seeds a TTS pool instead.
    gateway["pool_id"] = "voice-creation"
    target = VoiceCloneExecutionTarget(
        gateway["host_node_id"],
        gateway["gateway_id"],
        gateway["pool_id"],
        gateway["instance_id"],
        gateway["instance_generation"],
        gateway["binding_revision_id"],
    )
    policy = VoiceClonePolicy(gateway["policy_revision_id"], max_audio_ms=120_000)
    engine = Synthesizer(target)
    objects = Objects()
    runner = CommandFileCustomTts(
        inputs=FileExecutionInputs(objects),
        synthesizer=engine,
        encoder=LocalTtsEncoder(),
        binding_policy=policy,
        input_limits=ExecutionInputLimits(8 * 1024 * 1024, 1024 * 1024, 5),
        max_payload_bytes=1024 * 1024,
    )
    store = Store()
    outcome = await runner.stage_result(
        _submit_request(gateway), store=store, cancelled=lambda: False
    )
    assert outcome.terminal is not None
    assert len(objects.reads) == 1
    assert len(engine.calls) == 1
    frozen, used_policy = engine.calls[0]
    assert frozen.preview_text == "Custom narration."
    assert frozen.reference_text == REFERENCE_TEXT
    assert used_policy.sample_rate_hz == 24000
    assert len(store.results) == 1


def test_a_missing_immutable_revision_does_not_fall_back_to_builtin(
    admission_database: AdmissionDatabase,
) -> None:
    _seed(admission_database)
    db = admission_database.runtime.foundation.database
    publication = _publication(admission_database)
    voice_id = publication["voice_id"]
    db.query(
        "DELETE FROM speech_voice_revisions WHERE voice_id=%s AND revision=%s",
        (voice_id, str(publication["voice_revision"])),
    )
    before = db.query("SELECT count(*) AS n FROM admission_operations")[0]["n"]
    result = _store(admission_database).create(_custom_tts_job(voice_id, "1"))
    assert result.outcome == "rejected"
    assert result.error_code == "voice_not_published"
    assert db.query("SELECT count(*) AS n FROM admission_operations")[0]["n"] == before


def test_an_unpublished_voice_is_rejected_before_any_command_is_written(
    admission_database: AdmissionDatabase,
) -> None:
    _seed(admission_database)
    db = admission_database.runtime.foundation.database
    publication = _publication(admission_database)
    voice_id = publication["voice_id"]
    # Retire the published revision: the TTS Job names a voice the catalogue no
    # longer offers, and the immutable publication row still exists.
    revision_id = db.query(
        "SELECT revision_id FROM speech_publications "
        "WHERE kind='voice' AND object_id=%s AND revision=%s",
        (voice_id, str(publication["voice_revision"])),
    )[0]["revision_id"]
    db.query(
        "INSERT INTO speech_publication_retirements "
        "(kind, object_id, revision, revision_id, retired_at_us) "
        "VALUES ('voice',%s,%s,%s,%s)",
        (voice_id, str(publication["voice_revision"]), revision_id, NOW_US),
    )
    before = db.query("SELECT count(*) AS n FROM admission_operations")[0]["n"]
    result = _store(admission_database).create(_custom_tts_job(voice_id, "1"))
    assert result.outcome == "rejected"
    assert result.error_code == "voice_not_published"
    assert db.query("SELECT count(*) AS n FROM admission_operations")[0]["n"] == before
