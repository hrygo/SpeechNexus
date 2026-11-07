"""Real migrated PG publication: evidence, immutability, replay and release."""

import json
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest
from psycopg import errors
from psycopg.conninfo import make_conninfo
from tests.integration.test_public_jobs_http import JobSandbox
from tests.integration.test_voice_drafts_pg import (
    _body,
    _candidate_draft,
    _provider,
    _seed_preview_artifact,
    _validation_document,
)
from tests.integration.test_voice_drafts_pg import (
    voice_db as voice_db,
)

from speechnexus_backend.speech.application.voice_capability_evidence import (
    VoiceEvidenceConflictError,
    VoiceEvidenceNotEligibleError,
)
from speechnexus_backend.speech.application.voice_drafts import VoiceDraftConflictError
from speechnexus_backend.speech.application.voice_publications import (
    VoicePublicationConflictError,
    VoicePublicationNotEligibleError,
    VoicePublicationNotFoundError,
    VoicePublications,
)
from speechnexus_backend.speech.domain.voice_lifecycle import VoiceRetirementCommand
from speechnexus_backend.speech.infrastructure.postgres_pool import speech_pool
from speechnexus_backend.speech.infrastructure.publications_postgres import PostgresPublications
from speechnexus_backend.speech.infrastructure.voice_capability_evidence_postgres import (
    PostgresVoiceEvidenceRecorder,
)
from speechnexus_backend.speech.infrastructure.voice_catalogue_postgres import (
    PostgresVoiceCatalogue,
)
from speechnexus_backend.speech.infrastructure.voice_publication_schema import (
    voice_evidence_recorder_grants,
    voice_publication_grants,
)
from speechnexus_backend.speech.infrastructure.voice_publications_postgres import (
    PostgresVoicePublications,
)
from speechnexus_backend.speech.infrastructure.voice_retirements_postgres import (
    PostgresVoiceRetirements,
)

pytestmark = pytest.mark.integration

_ASSET_SHA = "sha256:" + "b" * 64
_ASSET_BYTES = 64
_CAPABILITIES = {
    "languages": ["en"],
    "sample_rates_hz": [24000],
    "audio_formats": ["wav"],
    "modes": ["complete"],
}


def _report(*, revision: str = "1", material: dict[str, Any] | None = None) -> bytes:
    checks = [
        {"name": "intelligibility", "required": True, "status": "passed"},
        {"name": "reusable_tts", "required": True, "status": "passed"},
    ]
    document = {
        "synthesis_path": "reusable_tts",
        "capabilities": _CAPABILITIES,
        "coverage": {"validation_set_revision": "1", "checks": checks},
        "material": material
        if material is not None
        else {
            "asset_id": "synthetic-reference",
            "sha256": _ASSET_SHA,
            "bytes": _ASSET_BYTES,
            "media_type": "audio/wav",
        },
    }
    return json.dumps(document).encode("utf-8")


def _seed_asset_object(db: JobSandbox) -> None:
    db.admin.execute(
        "INSERT INTO speech_asset_objects "
        "(asset_id, object_key, storage_version_id, bytes, sha256, writer_id, verified_at_us) "
        "VALUES ('synthetic-reference','synthetic-reference','version-1',%s,%s,'test',1) "
        "ON CONFLICT (asset_id) DO NOTHING",
        (_ASSET_BYTES, _ASSET_SHA),
    )


def _digest(raw: bytes) -> str:
    import hashlib

    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _seed_report(db: JobSandbox, artifact_id: str, receipt: str, raw: bytes) -> None:
    db.admin.execute(
        "INSERT INTO speech_artifacts "
        "(artifact_id, workspace_id, artifact_kind, artifact_state, media_type, bytes, "
        "sha256, receipt_id, source_operation_id, object_key, storage_version_id, created_at_us) "
        "VALUES (%s, 'workspace-default', 'validation_report', 'ready', 'application/json', "
        "%s, %s, %s, NULL, %s, 'synthetic-version', 10)",
        (
            artifact_id,
            len(raw),
            _digest(raw),
            receipt,
            "private/artifacts/workspace-default/" + artifact_id,
        ),
    )


def _validating_draft(db: JobSandbox, *, kind: str = "clone") -> tuple[dict[str, Any], Any, bytes]:
    """A clone draft whose head has a succeeded validation Job and report."""
    provider, draft = _candidate_draft(db, kind)
    job = provider.validate(
        draft["id"],
        {"expected_draft_revision": draft["head_revision"], "validation_set_revision": "1"},
        request_id="request-validation-" + draft["id"],
        idempotency_key=None,
    )
    raw = _report(revision=draft["head_revision"])
    artifact_id, receipt = "report-" + job.job_id, "receipt-" + job.job_id
    _seed_report(db, artifact_id, receipt, raw)
    db.jobs.advance(job.job_id, "running")
    db.jobs.advance(
        job.job_id,
        "succeeded",
        result_json=json.dumps(
            {
                "kind": "voice_validation",
                "validation": _validation_document(draft["id"], draft["head_revision"]),
                "validation_report_artifact_id": artifact_id,
                "receipt_id": receipt,
            }
        ),
    )
    _seed_asset_object(db)
    # The shared voice fixture also seeds a builtin voice publication without a
    # qualification document. This slice's catalogue assertion needs a clean
    # voice publication set, so remove only that unrelated voice row.
    db.admin.execute("DELETE FROM speech_publication_heads WHERE kind='voice'")
    db.admin.execute("DELETE FROM speech_publications WHERE kind='voice'")
    return draft, job, raw


def _publications(db: JobSandbox) -> VoicePublications:
    return VoicePublications(PostgresVoicePublications(db.pool))


def _payload(draft: dict[str, Any], job_id: str) -> dict[str, Any]:
    return {
        "expected_draft_revision": draft["head_revision"],
        "validation_job_id": job_id,
        "manual_confirmation": True,
    }


def test_publish_writes_one_immutable_revision_and_reads_back(
    voice_db: JobSandbox,
) -> None:
    draft, job, raw = _validating_draft(voice_db)
    evidence, created = PostgresVoiceEvidenceRecorder(voice_db.pool).record(
        job.job_id, raw, "pytest/recorder"
    )
    assert created and evidence.draft_revision == draft["head_revision"]

    body, created = _publications(voice_db).publish(
        draft["id"],
        _payload(draft, job.job_id),
        request_id="request-publish-1",
        idempotency_key="publish-key-1",
    )
    assert created is True
    assert body["resource_type"] == "voice_publication"
    assert body["voice_id"] == "voice-" + draft["id"].removeprefix("draft-")
    assert body["voice_revision"] == "1"
    assert body["validation_job_id"] == job.job_id
    assert body["manual_confirmation"] is True
    assert voice_db.query("SELECT status FROM speech_voice_drafts")[0]["status"] == "published"
    revision = voice_db.query("SELECT document, material, source_type FROM speech_voice_revisions")[
        0
    ]
    assert revision["source_type"] == "cloned"
    assert revision["document"]["status"] == "published"
    assert revision["document"]["capabilities"] == _CAPABILITIES
    assert revision["document"]["source"] == {
        "type": "cloned",
        "draft_id": draft["id"],
        "draft_revision": "1",
        "source_asset_digest": _ASSET_SHA,
    }
    reference = voice_db.query("SELECT * FROM speech_asset_references")[0]
    assert reference["consumer_kind"] == "voice_revision"
    assert reference["released_at_us"] is None

    # The public catalogue view recognises the custom document: one row, not a
    # page-level 503 caused by a missing document.
    catalogue = PostgresVoiceCatalogue(voice_db.pool)
    listed = catalogue.list_voices(language=None, limit=50, cursor=None)
    assert [item["id"] for item in listed["data"]] == [body["voice_id"]]
    read = catalogue.get_revision(body["voice_id"], "1")
    assert read is not None and read["capabilities"] == _CAPABILITIES

    replay, replay_created = _publications(voice_db).publish(
        draft["id"],
        _payload(draft, job.job_id),
        request_id="request-publish-replay",
        idempotency_key="publish-key-1",
    )
    assert replay_created is False and replay == body
    # A keyless retry of the same target is the same publication, not a second one.
    keyless, keyless_created = _publications(voice_db).publish(
        draft["id"],
        _payload(draft, job.job_id),
        request_id="request-publish-retry",
        idempotency_key=None,
    )
    assert keyless_created is False and keyless == body
    assert voice_db.count("speech_voice_revisions") == 1
    assert voice_db.count("speech_voice_publication_intents") == 1


def test_same_key_different_intent_is_a_conflict(voice_db: JobSandbox) -> None:
    draft, job, raw = _validating_draft(voice_db)
    PostgresVoiceEvidenceRecorder(voice_db.pool).record(job.job_id, raw, "pytest/recorder")
    _publications(voice_db).publish(
        draft["id"],
        _payload(draft, job.job_id),
        request_id="request-publish-1",
        idempotency_key="publish-key-1",
    )
    with pytest.raises(VoicePublicationConflictError):
        _publications(voice_db).publish(
            draft["id"],
            {**_payload(draft, job.job_id), "display_name": "another intent"},
            request_id="request-publish-2",
            idempotency_key="publish-key-1",
        )


def test_stale_head_is_a_conflict(voice_db: JobSandbox) -> None:
    draft, job, raw = _validating_draft(voice_db)
    PostgresVoiceEvidenceRecorder(voice_db.pool).record(job.job_id, raw, "pytest/recorder")
    with pytest.raises(VoicePublicationNotFoundError):
        _publications(voice_db).publish(
            "draft-missing",
            _payload(draft, job.job_id),
            request_id="request-publish-missing",
            idempotency_key=None,
        )
    with pytest.raises(VoicePublicationConflictError):
        # A later head is not the publication target.
        _publications(voice_db).publish(
            draft["id"],
            {**_payload(draft, job.job_id), "expected_draft_revision": "2"},
            request_id="request-publish-stale",
            idempotency_key=None,
        )


def test_design_remains_preview_only(voice_db: JobSandbox) -> None:
    _, draft = _candidate_draft(voice_db, "design")
    with pytest.raises(VoicePublicationNotEligibleError) as rejected:
        _publications(voice_db).publish(
            draft["id"],
            {
                "expected_draft_revision": "1",
                "validation_job_id": "job-without-validation",
                "manual_confirmation": True,
            },
            request_id="request-publish-design",
            idempotency_key=None,
        )
    assert "design_preview_only" in str(rejected.value)


def test_missing_or_tampered_evidence_is_rejected(voice_db: JobSandbox) -> None:
    draft, job, raw = _validating_draft(voice_db)
    with pytest.raises(VoicePublicationNotEligibleError):
        _publications(voice_db).publish(
            draft["id"],
            _payload(draft, job.job_id),
            request_id="request-publish-no-evidence",
            idempotency_key=None,
        )

    PostgresVoiceEvidenceRecorder(voice_db.pool).record(job.job_id, raw, "pytest/recorder")
    # Tampering the committed report digest after measurement invalidates the
    # evidence, even though the report bytes were correct when recorded.
    voice_db.admin.execute(
        "UPDATE speech_artifacts SET sha256=%s WHERE artifact_id=%s",
        ("sha256:" + "c" * 64, "report-" + job.job_id),
    )
    with pytest.raises(VoicePublicationNotEligibleError) as rejected:
        _publications(voice_db).publish(
            draft["id"],
            _payload(draft, job.job_id),
            request_id="request-publish-tampered",
            idempotency_key=None,
        )
    assert "report_digest_mismatch" in str(rejected.value)


def test_missing_material_proof_is_rejected(voice_db: JobSandbox) -> None:
    draft, job, raw = _validating_draft(voice_db)
    PostgresVoiceEvidenceRecorder(voice_db.pool).record(job.job_id, raw, "pytest/recorder")
    voice_db.admin.execute(
        "UPDATE speech_assets SET asset_state='expired' WHERE asset_id='synthetic-reference'"
    )
    with pytest.raises(VoicePublicationNotEligibleError) as rejected:
        _publications(voice_db).publish(
            draft["id"],
            _payload(draft, job.job_id),
            request_id="request-publish-no-material",
            idempotency_key=None,
        )
    assert "material" in str(rejected.value)


def test_evidence_row_is_insert_only_and_bound_to_one_job(voice_db: JobSandbox) -> None:
    _draft, job, raw = _validating_draft(voice_db)
    recorder = PostgresVoiceEvidenceRecorder(voice_db.pool)
    first, created = recorder.record(job.job_id, raw, "pytest/recorder")
    assert created
    replay, replay_created = recorder.record(job.job_id, raw, "pytest/recorder")
    assert replay_created is False and replay == first
    # Different evidence facts for the same Job are a conflict, never an
    # overwrite; changed report bytes are rejected before they reach the table.
    with pytest.raises(VoiceEvidenceConflictError):
        recorder.record(job.job_id, raw, "another/recorder")
    with pytest.raises(VoiceEvidenceNotEligibleError):
        recorder.record(job.job_id, raw.replace(b'"en"', b'"fr"'), "pytest/recorder")


def test_retire_releases_the_material_reference(voice_db: JobSandbox) -> None:
    draft, job, raw = _validating_draft(voice_db)
    PostgresVoiceEvidenceRecorder(voice_db.pool).record(job.job_id, raw, "pytest/recorder")
    body, _ = _publications(voice_db).publish(
        draft["id"],
        _payload(draft, job.job_id),
        request_id="request-publish-retire",
        idempotency_key=None,
    )
    before = voice_db.query(
        "SELECT released_at_us FROM speech_asset_references WHERE consumer_id=%s",
        (body["voice_id"],),
    )
    assert before[0]["released_at_us"] is None
    retirements = PostgresVoiceRetirements(PostgresPublications(voice_db.pool))
    receipt = retirements.retire(
        VoiceRetirementCommand(
            voice_id=body["voice_id"],
            revision="1",
            mode="retire",
            reason="synthetic retirement",
            request_id="request-retire-1",
        )
    )
    assert receipt.replayed is False
    after = voice_db.query(
        "SELECT released_at_us FROM speech_asset_references WHERE consumer_id=%s",
        (body["voice_id"],),
    )
    assert after[0]["released_at_us"] == receipt.retired_at_us


def test_concurrent_publication_has_one_winner(voice_db: JobSandbox) -> None:
    draft, job, raw = _validating_draft(voice_db)
    PostgresVoiceEvidenceRecorder(voice_db.pool).record(job.job_id, raw, "pytest/recorder")

    def publish(_worker: int) -> tuple[int, str]:
        try:
            _body_ignored, created = _publications(voice_db).publish(
                draft["id"],
                _payload(draft, job.job_id),
                request_id="request-concurrent",
                idempotency_key=None,
            )
            return (201 if created else 200), ""
        except (VoicePublicationConflictError, Exception) as error:
            return 409, type(error).__name__

    with ThreadPoolExecutor(max_workers=2) as workers:
        results = list(workers.map(publish, range(2)))
    assert sorted(status for status, _ in results) == [200, 201], results
    assert voice_db.count("speech_voice_revisions") == 1


def test_duplicate_draft_revision_is_rejected_by_the_unique_index(
    voice_db: JobSandbox,
) -> None:
    draft, job, raw = _validating_draft(voice_db)
    PostgresVoiceEvidenceRecorder(voice_db.pool).record(job.job_id, raw, "pytest/recorder")
    body, _ = _publications(voice_db).publish(
        draft["id"],
        _payload(draft, job.job_id),
        request_id="request-publish-1",
        idempotency_key=None,
    )
    evidence_id = voice_db.query("SELECT evidence_id FROM speech_voice_capability_evidence")[0][
        "evidence_id"
    ]
    voice_db.admin.execute(
        "INSERT INTO speech_publications (kind, object_id, revision, revision_id, published_at_us) "
        "VALUES ('voice','voice-other','1','vrev-other-1',1)"
    )
    with pytest.raises(errors.UniqueViolation):
        voice_db.admin.execute(
            "INSERT INTO speech_voice_revisions "
            "(kind, voice_id, revision, revision_id, source_type, document, material, "
            "evidence_id, draft_id, draft_revision, published_at_us) "
            "VALUES ('voice','voice-other','1','vrev-other-1','cloned','{}'::jsonb,"
            "'{}'::jsonb,%s,%s,'1',1)",
            (evidence_id, draft["id"]),
        )
    assert body["voice_revision"] == "1"


def test_published_draft_cannot_generate_or_patch(voice_db: JobSandbox) -> None:
    draft, job, raw = _validating_draft(voice_db)
    PostgresVoiceEvidenceRecorder(voice_db.pool).record(job.job_id, raw, "pytest/recorder")
    _publications(voice_db).publish(
        draft["id"],
        _payload(draft, job.job_id),
        request_id="request-publish-1",
        idempotency_key=None,
    )
    with pytest.raises(VoiceDraftConflictError):
        _provider(voice_db).generate(
            draft["id"],
            {
                "expected_draft_revision": "1",
                "model": "voice-model",
                "input": _body("clone")["input"],
            },
            request_id="request-generate-after-publish",
            idempotency_key=None,
        )
    with pytest.raises(VoiceDraftConflictError):
        _provider(voice_db).update_metadata(
            draft["id"],
            {"expected_metadata_revision": 1, "display_name": "after publish"},
            "metadata-after-publish",
        )


def test_uncertainty_evidence_from_another_revision_is_rejected(voice_db: JobSandbox) -> None:
    draft, _job, _raw = _validating_draft(voice_db)
    # Append revision 2 and measure it, then force the head back to 1 so the
    # publication target is revision 1 while the only evidence names revision 2.
    provider = _provider(voice_db)
    _generated, generated_job = provider.generate(
        draft["id"],
        {
            "expected_draft_revision": "1",
            "model": "voice-model",
            "input": _body("clone")["input"],
        },
        request_id="request-generate-2",
        idempotency_key=None,
    )
    _seed_preview_artifact(voice_db, "preview-revision-2", receipt="receipt-revision-2")
    voice_db.jobs.advance(generated_job.job_id, "running")
    voice_db.jobs.advance(
        generated_job.job_id,
        "succeeded",
        result_json=json.dumps(
            {
                "kind": "voice_clone",
                "voice_draft_id": draft["id"],
                "draft_revision": "2",
                "preview_artifact_ids": ["preview-revision-2"],
                "receipt_id": "receipt-revision-2",
            }
        ),
    )
    second = provider.validate(
        draft["id"],
        {"expected_draft_revision": "2", "validation_set_revision": "1"},
        request_id="request-validate-2",
        idempotency_key=None,
    )
    second_raw = _report(revision="2")
    artifact_id, receipt = "report-" + second.job_id, "receipt-" + second.job_id
    _seed_report(voice_db, artifact_id, receipt, second_raw)
    voice_db.jobs.advance(second.job_id, "running")
    voice_db.jobs.advance(
        second.job_id,
        "succeeded",
        result_json=json.dumps(
            {
                "kind": "voice_validation",
                "validation": _validation_document(draft["id"], "2"),
                "validation_report_artifact_id": artifact_id,
                "receipt_id": receipt,
            }
        ),
    )
    _seed_asset_object(voice_db)
    PostgresVoiceEvidenceRecorder(voice_db.pool).record(
        second.job_id, second_raw, "pytest/recorder"
    )
    voice_db.admin.execute(
        "UPDATE speech_voice_drafts SET head_revision='1', status='validating' WHERE draft_id=%s",
        (draft["id"],),
    )
    with pytest.raises(VoicePublicationNotEligibleError) as rejected:
        _publications(voice_db).publish(
            draft["id"],
            _payload(draft, second.job_id),
            request_id="request-publish-cross-revision",
            idempotency_key=None,
        )
    assert "evidence_revision_mismatch" in str(rejected.value)


def test_evidence_grants_are_append_only_for_both_new_roles(voice_db: JobSandbox) -> None:
    _draft, job, raw = _validating_draft(voice_db)
    PostgresVoiceEvidenceRecorder(voice_db.pool).record(job.job_id, raw, "pytest/recorder")
    role = "sn519_evidence_append_only"
    voice_db.admin.execute(f'DROP ROLE IF EXISTS "{role}"')
    voice_db.admin.execute(f'CREATE ROLE "{role}" NOLOGIN')
    try:
        for grant in (
            *voice_evidence_recorder_grants(voice_db.schema, role),
            *voice_publication_grants(voice_db.schema, role),
        ):
            voice_db.admin.execute(grant)
        voice_db.admin.execute(f'SET ROLE "{role}"')
        try:
            with pytest.raises(errors.InsufficientPrivilege):
                voice_db.admin.execute(
                    "UPDATE speech_voice_capability_evidence SET measured_by='rewritten'"
                )
            with pytest.raises(errors.InsufficientPrivilege):
                voice_db.admin.execute("DELETE FROM speech_voice_capability_evidence")
        finally:
            voice_db.admin.execute("RESET ROLE")
    finally:
        voice_db.admin.execute(f'DROP OWNED BY "{role}"')
        voice_db.admin.execute(f'DROP ROLE IF EXISTS "{role}"')


def test_narrow_roles_can_record_and_publish(voice_db: JobSandbox) -> None:
    draft, job, raw = _validating_draft(voice_db)
    # Start from no evidence so the recorder identity performs the real INSERT.
    PostgresVoiceEvidenceRecorder(voice_db.pool).record(job.job_id, raw, "pytest/recorder")
    voice_db.admin.execute("DELETE FROM speech_voice_capability_evidence")
    recorder, publisher = "sn519_recorder_role", "sn519_publisher_role"
    for role in (recorder, publisher):
        if voice_db.admin.execute("SELECT 1 FROM pg_roles WHERE rolname=%s", (role,)).fetchone():
            voice_db.admin.execute(f'DROP OWNED BY "{role}"')
            voice_db.admin.execute(f'DROP ROLE "{role}"')
        voice_db.admin.execute(f"CREATE ROLE \"{role}\" LOGIN PASSWORD 'x'")
    recorder_pool = publisher_pool = None
    try:
        for grant in voice_evidence_recorder_grants(voice_db.schema, recorder):
            voice_db.admin.execute(grant)
        for grant in voice_publication_grants(voice_db.schema, publisher):
            voice_db.admin.execute(grant)
        recorder_pool = speech_pool(
            make_conninfo(voice_db.url, user=recorder, password="x"),
            schema=voice_db.schema,
            max_size=1,
            name="sn519-recorder-test",
        )
        recorder_pool.open()
        evidence, created = PostgresVoiceEvidenceRecorder(recorder_pool).record(
            job.job_id, raw, "pytest/recorder"
        )
        assert created and evidence.validation_job_id == job.job_id
        publisher_pool = speech_pool(
            make_conninfo(voice_db.url, user=publisher, password="x"),
            schema=voice_db.schema,
            max_size=1,
            name="sn519-publisher-test",
        )
        publisher_pool.open()
        body, created = VoicePublications(PostgresVoicePublications(publisher_pool)).publish(
            draft["id"],
            _payload(draft, job.job_id),
            request_id="request-publish-role",
            idempotency_key="publish-key-role",
        )
        assert created is True and body["voice_revision"] == "1"
    finally:
        if publisher_pool is not None:
            publisher_pool.close()
        if recorder_pool is not None:
            recorder_pool.close()
        for role in (recorder, publisher):
            voice_db.admin.execute(f'DROP OWNED BY "{role}"')
            voice_db.admin.execute(f'DROP ROLE IF EXISTS "{role}"')
