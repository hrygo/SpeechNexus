"""Real PG: committed validation reports become evidence without an operator.

The dispatch path, the report Artifact rows and the evidence ledger are real
PostgreSQL; only the execution gateway, the staging bucket and the Artifact blob
store are fakes. The recorder role is the documented ``voice_evidence_recorder_grants``
identity, not the API superset.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from typing import Any

import pytest
from psycopg import errors
from psycopg.conninfo import make_conninfo
from tests.integration.test_coordination_foundation import foundation as foundation
from tests.integration.test_production_admission import admission_database as admission_database
from tests.integration.test_production_jobs import database as database
from tests.integration.test_production_runtime_intents import runtime_database as runtime_database
from tests.integration.test_voice_validation_dispatch_pg import (
    SILENT_WAV_BODY,
    WAV_BODY,
    ValidationSandbox,
    _Blobs,
    _create_generation,
    _dispatcher,
    _Gateway,
)
from tests.integration.test_voice_validation_dispatch_pg import (
    validation_db as validation_db,
)

from speechnexus_backend.speech.application.voice_capability_evidence import (
    VoiceReportUnavailableError,
)
from speechnexus_backend.speech.application.voice_evidence_recording import (
    CommittedReportArtifacts,
    VoiceEvidenceAutoRecorder,
)
from speechnexus_backend.speech.domain.artifacts import ArtifactError, ArtifactRecord
from speechnexus_backend.speech.infrastructure.postgres_pool import speech_pool
from speechnexus_backend.speech.infrastructure.voice_capability_evidence_postgres import (
    PostgresVoiceEvidenceRecorder,
)
from speechnexus_backend.speech.infrastructure.voice_publication_schema import (
    EVIDENCE_TABLE,
    voice_evidence_recorder_grants,
)
from speechnexus_backend.voice_evidence_worker import _verify_recorder_worker

pytestmark = [
    pytest.mark.integration,
    pytest.mark.parametrize("database", ["postgresql"], indirect=True),
]

LABEL = "voice-evidence-recorder-worker/v1"


class _UnreadableBlobs:
    def read(self, artifact: ArtifactRecord) -> bytes:
        raise ArtifactError("artifact_unavailable")


@contextmanager
def _recorder_pool(sandbox: ValidationSandbox, *, extra: Sequence[str] = ()) -> Iterator[Any]:
    """Provision the documented recorder identity, plus explicit over-grants."""
    suffix = uuid.uuid4().hex[:10]
    role = "sn555_recorder_" + suffix
    schema = sandbox.db.schema
    sandbox.db.query(f"CREATE ROLE \"{role}\" LOGIN PASSWORD 'x'")
    try:
        for grant in voice_evidence_recorder_grants(schema, role):
            sandbox.db.query(grant)
        for statement in extra:
            sandbox.db.query(statement.format(schema=schema, role=role))
        pool = speech_pool(
            make_conninfo(sandbox.db.url, user=role, password="x"),
            schema=schema,
            max_size=2,
            name="evidence-recorder-" + suffix,
        )
        pool.open()
        try:
            yield pool
        finally:
            pool.close()
    finally:
        sandbox.db.query(f'DROP OWNED BY "{role}"')
        sandbox.db.query(f'DROP ROLE IF EXISTS "{role}"')


def _worker(pool: Any, blobs: Any, *, max_attempts: int = 5) -> VoiceEvidenceAutoRecorder:
    store = PostgresVoiceEvidenceRecorder(pool, timeout_seconds=3)
    return VoiceEvidenceAutoRecorder(
        pending=store,
        reports=CommittedReportArtifacts(store, blobs),
        recorder=store,
        measured_by=LABEL,
        max_attempts=max_attempts,
        batch_size=16,
    )


def _run_validation(
    sandbox: ValidationSandbox,
    *,
    body: bytes = WAV_BODY,
) -> tuple[dict[str, Any], Any, str, _Blobs]:
    draft, _generation, _preview = _create_generation(sandbox)
    validation = sandbox.drafts.validate(
        draft["id"],
        {"expected_draft_revision": draft["head_revision"], "validation_set_revision": "1"},
        request_id="request-validation-evidence",
        idempotency_key=None,
    )
    dispatcher, blobs = _dispatcher(sandbox, _Gateway(body), staging_body=body)
    batch = asyncio.run(dispatcher.run_once("worker-validation-evidence"))
    assert batch.succeeded == 1
    job = sandbox.jobs.get(validation.job_id)
    result = json.loads(job.result_json or "{}")
    return draft, job, str(result["validation_report_artifact_id"]), blobs


def _evidence_rows(sandbox: ValidationSandbox) -> list[dict[str, Any]]:
    return sandbox.query(
        f"SELECT evidence_id, validation_job_id, draft_id, draft_revision, report_artifact_id, "
        f"report_sha256, measured_by FROM {EVIDENCE_TABLE} ORDER BY measured_at_us"
    )


def test_committed_report_is_recorded_once_and_replayed(
    validation_db: ValidationSandbox,
) -> None:
    sandbox = validation_db
    _draft, job, artifact_id, blobs = _run_validation(sandbox)
    with _recorder_pool(sandbox) as pool:
        store = PostgresVoiceEvidenceRecorder(pool, timeout_seconds=3)
        assert store.pending(16) == [job.job_id]
        assert _evidence_rows(sandbox) == []

        worker = _worker(pool, blobs)
        batch = worker.run_once()
        assert (batch.scanned, batch.recorded, batch.existing) == (1, 1, 0)
        assert store.pending(16) == []

        rows = _evidence_rows(sandbox)
        assert len(rows) == 1
        assert rows[0]["evidence_id"] == "vce-" + job.job_id
        assert rows[0]["validation_job_id"] == job.job_id
        assert rows[0]["report_artifact_id"] == artifact_id
        assert (
            rows[0]["report_sha256"]
            == "sha256:" + hashlib.sha256(blobs.bodies[artifact_id]).hexdigest()
        )
        assert rows[0]["measured_by"] == LABEL

        # A second process that scanned before the first INSERT still replays the
        # original row instead of writing another one.
        replayed = _worker(pool, blobs).consume(job.job_id)
        assert (replayed.outcome, replayed.reason) == ("existing", None)
        assert len(_evidence_rows(sandbox)) == 1

        # The identity is the Job, not the attempt: even a hand-written row with
        # another evidence_id for the same Job is refused by the table check.
        with pytest.raises(errors.CheckViolation):
            sandbox.db.query(
                f"INSERT INTO {EVIDENCE_TABLE} "
                "(evidence_id, draft_id, draft_revision, validation_job_id, validation_id, "
                "binding_revision_id, report_artifact_id, report_sha256, report_receipt_id, "
                "evidence_kind, measured_by, measured_at_us, capabilities, coverage, material, "
                "evidence_digest) "
                f"SELECT 'vce-other-' || validation_job_id, draft_id, draft_revision, "
                "validation_job_id, validation_id, binding_revision_id, report_artifact_id, "
                "report_sha256, report_receipt_id, evidence_kind, measured_by, measured_at_us, "
                f"capabilities, coverage, material, evidence_digest FROM {EVIDENCE_TABLE} "
                "WHERE validation_job_id=%s",
                (job.job_id,),
            )
        assert len(_evidence_rows(sandbox)) == 1


def test_duplicate_consumption_keeps_one_row(validation_db: ValidationSandbox) -> None:
    sandbox = validation_db
    _draft, job, _artifact_id, blobs = _run_validation(sandbox)
    with _recorder_pool(sandbox) as pool:
        first = _worker(pool, blobs).consume(job.job_id)
        second = _worker(pool, blobs).consume(job.job_id)
    # Two independent consumers of the same Job must leave exactly one row.
    assert len(_evidence_rows(sandbox)) == 1
    assert first.outcome == "recorded"
    assert second.outcome == "existing"


def test_scan_heals_the_artifact_result_evidence_window(
    validation_db: ValidationSandbox,
) -> None:
    sandbox = validation_db
    _draft, job, _artifact_id, blobs = _run_validation(sandbox)
    before = sandbox.jobs.get(job.job_id)
    assert before.state == "succeeded"
    assert _evidence_rows(sandbox) == []

    # The worker starts after the interruption; it holds no memory of the Job.
    with _recorder_pool(sandbox) as pool:
        batch = _worker(pool, blobs).run_once()
        assert batch.recorded == 1

    assert len(_evidence_rows(sandbox)) == 1
    after = sandbox.jobs.get(job.job_id)
    # The ledger write never reopened or rewrote the Job it proves.
    assert after.state == "succeeded"
    assert after.result_json == before.result_json


def test_replaced_report_bytes_are_refused(
    validation_db: ValidationSandbox,
) -> None:
    sandbox = validation_db
    _draft, job, artifact_id, blobs = _run_validation(sandbox)
    original = blobs.bodies[artifact_id]
    # Same JSON document plus a byte: the Artifact row still commits the old
    # digest, so recording these bytes would qualify content nobody committed.
    blobs.bodies[artifact_id] = original + b"\n"
    with _recorder_pool(sandbox) as pool:
        outcome = _worker(pool, blobs).consume(job.job_id)
    assert outcome.outcome == "rejected"
    assert outcome.reason == "report_bytes_mismatch"
    assert _evidence_rows(sandbox) == []


def test_unreadable_artifact_is_retried_then_bounded(
    validation_db: ValidationSandbox,
) -> None:
    sandbox = validation_db
    _draft, _job, _artifact_id, _blobs = _run_validation(sandbox)
    with _recorder_pool(sandbox) as pool:
        worker = _worker(pool, _UnreadableBlobs(), max_attempts=2)
        outcomes = [worker.run_once().outcomes[0] for _ in range(2)]
        assert worker.run_once().scanned == 0
    assert [item.outcome for item in outcomes] == ["retry", "exhausted"]
    assert {item.reason for item in outcomes} == {"report_artifact_unavailable"}
    assert _evidence_rows(sandbox) == []


def test_failed_quality_report_is_refused(validation_db: ValidationSandbox) -> None:
    sandbox = validation_db
    _draft, job, _artifact_id, blobs = _run_validation(sandbox, body=SILENT_WAV_BODY)
    with _recorder_pool(sandbox) as pool:
        outcome = _worker(pool, blobs).consume(job.job_id)
    assert outcome.outcome == "rejected"
    assert outcome.reason == "validation_not_eligible"
    assert _evidence_rows(sandbox) == []


def test_forged_passed_report_cannot_put_a_silent_probe_in_the_ledger(
    validation_db: ValidationSandbox,
) -> None:
    sandbox = validation_db
    _draft, job, artifact_id, blobs = _run_validation(sandbox)
    document = json.loads(blobs.bodies[artifact_id])
    probe = document["coverage"]["probes"][0]
    # A report that claims the coverage checks passed while its only probe is a
    # 1 ms silent clip. The recorder must recompute the quality floor itself.
    for name, value in (
        ("sample_frames", 24),
        ("audio_duration_ms", 1),
        ("output_rms", 0),
        ("output_peak", 0),
        ("output_bytes", 48),
        ("output_sha256", "sha256:" + "0" * 64),
    ):
        probe[name] = value
    forged = json.dumps(document, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    assert all(check["status"] == "passed" for check in document["coverage"]["checks"])
    blobs.bodies[artifact_id] = forged
    sandbox.db.query(
        "UPDATE speech_artifacts SET sha256=%s, bytes=%s WHERE artifact_id=%s",
        ("sha256:" + hashlib.sha256(forged).hexdigest(), len(forged), artifact_id),
    )
    with _recorder_pool(sandbox) as pool:
        outcome = _worker(pool, blobs).consume(job.job_id)
    assert outcome.outcome == "rejected"
    assert outcome.reason == "coverage_probes_invalid"
    assert _evidence_rows(sandbox) == []


def test_ledger_write_failure_is_retried_and_bounded(
    validation_db: ValidationSandbox,
) -> None:
    sandbox = validation_db
    _draft, job, _artifact_id, blobs = _run_validation(sandbox)
    schema = sandbox.db.schema
    with _recorder_pool(sandbox) as pool:
        with pool.connection() as connection:
            identity = connection.execute("SELECT CURRENT_USER AS role").fetchone()
        assert identity is not None
        recorder_role = identity["role"]
        sandbox.db.query(f'REVOKE INSERT ON "{schema}".{EVIDENCE_TABLE} FROM "{recorder_role}"')
        try:
            worker = _worker(pool, blobs, max_attempts=2)
            outcomes = [worker.run_once().outcomes[0] for _ in range(2)]
            assert worker.run_once().scanned == 0
            assert [item.outcome for item in outcomes] == ["retry", "exhausted"]
            assert {item.reason for item in outcomes} == {"evidence_storage_unavailable"}
            assert _evidence_rows(sandbox) == []
        finally:
            sandbox.db.query(f'GRANT INSERT ON "{schema}".{EVIDENCE_TABLE} TO "{recorder_role}"')
        # A fresh pass after restoration records the still-committed Job.
        restored = _worker(pool, blobs).run_once()
        assert restored.recorded == 1
    assert len(_evidence_rows(sandbox)) == 1
    assert sandbox.jobs.get(job.job_id).state == "succeeded"


def test_non_terminal_job_is_refused(validation_db: ValidationSandbox) -> None:
    sandbox = validation_db
    draft, _generation, _preview = _create_generation(sandbox)
    validation = sandbox.drafts.validate(
        draft["id"],
        {"expected_draft_revision": draft["head_revision"], "validation_set_revision": "1"},
        request_id="request-validation-pending",
        idempotency_key=None,
    )
    with _recorder_pool(sandbox) as pool:
        outcome = _worker(pool, _Blobs()).consume(validation.job_id)
    assert outcome.outcome == "rejected"
    assert outcome.reason == "validation_job_not_succeeded"
    assert _evidence_rows(sandbox) == []


def test_malformed_result_row_does_not_break_the_scan(
    validation_db: ValidationSandbox,
) -> None:
    sandbox = validation_db
    _draft, job, _artifact_id, blobs = _run_validation(sandbox)
    corrupt = "job-" + uuid.uuid4().hex
    sandbox.db.query(
        "INSERT INTO speech_jobs "
        "(job_id, workspace_id, kind, model_id, request_digest, state, result_json, "
        "created_at_us, updated_at_us) "
        "VALUES (%s, 'workspace-default', 'voice_validation', 'voice-model', %s, "
        "'succeeded', 'not-json', 1, 1)",
        (corrupt, "sha256:" + "a" * 64),
    )
    with _recorder_pool(sandbox) as pool:
        store = PostgresVoiceEvidenceRecorder(pool, timeout_seconds=3)
        # The CASE guard keeps one corrupt historical row from aborting a pass.
        assert store.pending(16) == [job.job_id]
        assert _worker(pool, blobs).run_once().recorded == 1


def test_worker_refuses_a_broader_identity(validation_db: ValidationSandbox) -> None:
    sandbox = validation_db
    schema = sandbox.db.schema
    # The documented recorder role passes.
    with _recorder_pool(sandbox) as pool:
        _verify_recorder_worker(pool, schema)
    # The dispatch worker role is a superset: it may write Jobs and more.
    with pytest.raises(ValueError):
        _verify_recorder_worker(sandbox.worker_pool, schema)
    # Each of the #549 gaps must be detected on its own.
    for statement in (
        f'GRANT REFERENCES ON "{{schema}}".{EVIDENCE_TABLE} TO "{{role}}"',
        'GRANT TRIGGER ON "{schema}".speech_jobs TO "{role}"',
        'GRANT INSERT (job_id) ON "{schema}".speech_jobs TO "{role}"',
        f'GRANT UPDATE (measured_by) ON "{{schema}}".{EVIDENCE_TABLE} TO "{{role}}"',
        'GRANT SELECT ON "{schema}".speech_publications TO "{role}"',
    ):
        with (
            _recorder_pool(sandbox, extra=[statement]) as pool,
            pytest.raises(ValueError),
        ):
            _verify_recorder_worker(pool, schema)


def test_recorder_identity_reads_the_artifact_row_and_report_object(
    validation_db: ValidationSandbox,
) -> None:
    sandbox = validation_db
    _draft, job, artifact_id, blobs = _run_validation(sandbox)
    with _recorder_pool(sandbox) as pool:
        store = PostgresVoiceEvidenceRecorder(pool, timeout_seconds=3)
        artifact = store.report(job.job_id)
        assert artifact.artifact_id == artifact_id
        assert artifact.kind == "validation_report"
        assert artifact.status == "ready"
        assert artifact.sha256 == "sha256:" + hashlib.sha256(blobs.bodies[artifact_id]).hexdigest()
        reports = CommittedReportArtifacts(store, blobs)
        assert reports.read(job.job_id) == blobs.bodies[artifact_id]
        with pytest.raises(VoiceReportUnavailableError):
            CommittedReportArtifacts(store, _UnreadableBlobs()).read(job.job_id)


def test_pending_is_bounded_and_ordered(validation_db: ValidationSandbox) -> None:
    sandbox = validation_db
    _draft, job, _artifact_id, _blobs = _run_validation(sandbox)
    with _recorder_pool(sandbox) as pool:
        store = PostgresVoiceEvidenceRecorder(pool, timeout_seconds=3)
        assert store.pending(1) == [job.job_id]
        assert store.pending(16, exclude=[job.job_id]) == []
        assert store.pending(16, exclude=["job-other"]) == [job.job_id]
        with pytest.raises(ValueError):
            store.pending(0)
        with pytest.raises(ValueError):
            store.pending(129)
