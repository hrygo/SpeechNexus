"""Atomic public Job create: reservation and public rows commit or roll back together."""

from __future__ import annotations

import json
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import psycopg
import pytest
from psycopg.conninfo import make_conninfo
from tests.integration.test_coordination_foundation import foundation as foundation
from tests.integration.test_production_admission import AdmissionDatabase
from tests.integration.test_production_admission import admission_database as admission_database
from tests.integration.test_production_jobs import database as database
from tests.integration.test_production_runtime_intents import runtime_database as runtime_database

from speechnexus_backend.job_runtime import _verify_admission_privileges, _verify_privileges
from speechnexus_backend.speech.domain.public_jobs import (
    JobSubmission,
    NewPublicJob,
    canonical_digest,
    idempotency_digest,
)
from speechnexus_backend.speech.domain.publications import Publication
from speechnexus_backend.speech.infrastructure.job_cancellation_fence_schema import (
    cancellation_reconciliation_grants,
)
from speechnexus_backend.speech.infrastructure.job_dispatch_postgres import PostgresDispatchQueue
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
from speechnexus_backend.speech.infrastructure.public_jobs_postgres import PostgresPublicJobs
from speechnexus_backend.speech.infrastructure.publications_sql import append_publication
from speechnexus_backend.speech.infrastructure.voice_draft_schema import voice_draft_grants

pytestmark = [
    pytest.mark.integration,
    pytest.mark.parametrize("database", ["postgresql"], indirect=True),
]

WORKSPACE = "workspace-test"
BINDING = "binding-test"
INCOMPLETE = "binding-incomplete"
VOICE = "voice-vivian"
DIGEST = "sha256:" + "a" * 64
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
ASR = {
    "language": "auto",
    "turn_detection": {"mode": "server_vad", "silence_duration_ms": 500},
    "finalization": "full_segment",
    "preview_interval_ms": 200,
    "max_segment_ms": 30_000,
    "rollback_tokens": 0,
    "final_deadline_ms": 5_000,
    "auxiliary_deadline_ms": 1_000,
    "alignment": "off",
    "diarization": "off",
    "vocabulary_set_revision": None,
    "temperature": 0.0,
    "timeout_seconds": 60,
}


def _seed(admission: AdmissionDatabase, *, complete: bool = True) -> None:
    runtime = admission.runtime
    db = runtime.foundation.database
    role = runtime.foundation.speech_role
    schema = db.schema
    for grant in (
        *job_runtime_grants(schema, role),
        *job_dispatch_grants(schema, role),
        *public_job_grants(schema, role),
        *voice_draft_grants(schema, role),
        *public_job_submission_grants(schema, role),
        *cancellation_reconciliation_grants(schema, role),
        f'GRANT SELECT ON "{schema}".speech_workspaces, "{schema}".speech_publications, '
        f'"{schema}".speech_publication_heads, "{schema}".speech_catalog_state, '
        f'"{schema}".speech_publication_retirements, "{schema}".speech_binding_revisions, '
        f'"{schema}".speech_assets, "{schema}".speech_pronunciation_revisions TO "{role}"',
    ):
        db.query(grant)
    db.query(
        "INSERT INTO speech_workspaces "
        "(workspace_id, display_name, workspace_state, created_at_us) "
        "VALUES (%s, 'test', 'active', 1)",
        (WORKSPACE,),
    )
    definitions = {
        BINDING: {
            "binding_id": BINDING,
            "tasks": ["tts_file", "asr_file"],
            "release_tier": "production",
            "execution_recipe": {
                "recipe_revision": "1",
                "capacity_manifest_id": "manifest-test",
                "stage_roles": ["tts_talker"],
            },
            "execution_defaults": {"tts": TTS, "asr": ASR},
        },
        INCOMPLETE: {
            "binding_id": INCOMPLETE,
            "tasks": ["tts_file"],
            "release_tier": "production",
            "execution_defaults": {
                "tts": {name: value for name, value in TTS.items() if name != "timeout_seconds"}
            },
            "execution_recipe": {
                "recipe_revision": "1",
                "capacity_manifest_id": "manifest-test",
                "stage_roles": ["tts_talker"],
            },
        },
    }
    if not complete:
        definitions[BINDING].pop("execution_defaults")
    for binding_id, definition in definitions.items():
        db.query(
            "INSERT INTO speech_binding_families (binding_id, latest_revision) VALUES (%s, '1')",
            (binding_id,),
        )
        db.query(
            "INSERT INTO speech_binding_commands "
            "(key_digest, binding_id, document_version, intent_json, request_digest) "
            "VALUES (%s, %s, 1, '{}', %s)",
            (DIGEST if binding_id == BINDING else "sha256:" + "b" * 64, binding_id, DIGEST),
        )
        db.query(
            "INSERT INTO speech_binding_revisions "
            "(binding_id, revision, revision_id, key_digest, document_version, definition_json, "
            "definition_digest, request_digest, created_at_us) "
            "VALUES (%s, '1', %s, %s, 1, %s, %s, %s, 1)",
            (
                binding_id,
                binding_id,
                DIGEST if binding_id == BINDING else "sha256:" + "b" * 64,
                json.dumps(definition),
                DIGEST,
                DIGEST,
            ),
        )
    with db.admin.transaction(), db.admin.cursor() as cursor:
        assert append_publication(
            cursor,
            Publication("binding", BINDING, 1, BINDING, 1),
            expected_current_revision=None,
            expected_catalog_revision=1,
        )
        assert append_publication(
            cursor,
            Publication("binding", INCOMPLETE, 1, INCOMPLETE, 1),
            expected_current_revision=None,
            expected_catalog_revision=2,
        )
        assert append_publication(
            cursor,
            Publication("voice", VOICE, 2, "voice-rev-2", 2),
            expected_current_revision=None,
            expected_catalog_revision=3,
        )


def _store(admission: AdmissionDatabase) -> PostgresPublicJobs:
    pool = admission.runtime.pool
    assert pool is not None
    assert admission.admission_pool is not None
    return PostgresPublicJobs(
        pool,
        admission_pool=admission.admission_pool,
        require_admission=True,
        timeout_seconds=3,
        storage_bucket="transcription-inputs",
    )


def _job(
    *,
    kind: str = "tts",
    model: str = BINDING,
    input_value: dict[str, Any] | None = None,
    key: str = "synthetic-idem-0001",
    queue_wait_ms: int | None = None,
) -> NewPublicJob:
    job_id = "job-" + canonical_digest({"key": key}).removeprefix("sha256:")[:48]
    intent: dict[str, Any] = {
        "workspace_id": WORKSPACE,
        "kind": kind,
        "model": model,
        "input": input_value
        or ({"text": "synthetic public job"} if kind == "tts" else {"audio_asset_id": "asset-1"}),
        "output": (
            {"audio_format": "mp3", "save_artifacts": True}
            if kind == "tts"
            else {"transcript_format": "json", "save_artifacts": True}
        ),
        "constraints": {
            "allow_degradation": False,
            "require_exact_parameters": False,
            **({"max_queue_wait_ms": queue_wait_ms} if queue_wait_ms is not None else {}),
        },
    }
    if kind == "tts":
        intent["voice"] = {"id": VOICE, "revision": 2}
    return NewPublicJob(
        job_id=job_id,
        workspace_id=WORKSPACE,
        kind=kind,  # type: ignore[arg-type]
        model_id=model,
        request_digest=canonical_digest(intent),
        request_document=json.dumps(intent),
        initial_event_digest=canonical_digest({"job_id": job_id, "event_seq": 0}),
        idempotency_key_digest=idempotency_digest(key),
        submission=JobSubmission(
            request_id="request-" + job_id,
            client_reference=None,
            effective_configuration_json=json.dumps(
                {
                    "models": {
                        kind: {"id": model, "revision": "1"},
                    },
                    **({"voice": {"id": VOICE, "revision": 2}} if kind == "tts" else {}),
                }
            ),
            execution_revision=1,
            progress_unit="text_codepoints" if kind == "tts" else "audio_ms",
            progress_total=21 if kind == "tts" else 1000,
            submitted_at_us=1_800_000_000_000_000,
        ),
    )


def _counts(db: Any) -> dict[str, int]:
    tables = (
        "speech_jobs",
        "speech_job_events",
        "speech_job_submissions",
        "speech_job_intents",
        "admission_operations",
        "admission_attempts",
        "admission_requests",
        "admission_leases",
        "coordination_jobs",
        "coordination_outbox",
    )
    return {table: int(db.query(f"SELECT count(*) AS n FROM {table}")[0]["n"]) for table in tables}


def test_tts_create_reserves_and_writes_public_rows_atomically(
    admission_database: AdmissionDatabase,
) -> None:
    _seed(admission_database)
    db = admission_database.runtime.foundation.database
    before = _counts(db)
    result = _store(admission_database).create(_job())
    assert result.outcome == "created" and result.job is not None, result.error_code
    counts = _counts(db)
    for table in counts:
        assert counts[table] == before[table] + 1, table
    operation = db.query("SELECT * FROM admission_operations")[0]
    command = json.loads(operation["request_document"])
    assert command["kind"] == "tts_file"
    assert command["gateway_request"]["command"]["kind"] == "tts"
    assert command["gateway_request"]["operation_id"] == operation["operation_id"]
    lease = db.query(
        "SELECT * FROM admission_leases WHERE operation_id = %s",
        (operation["operation_id"],),
    )[0]
    assert lease["lease_id"] == "lease-" + result.job.job_id.removeprefix("job-")
    intent = db.query("SELECT request_digest, request_document FROM speech_job_intents")[0]
    assert json.loads(intent["request_document"])["kind"] == "tts"
    job_digest = db.query("SELECT request_digest FROM speech_jobs")[0]["request_digest"]
    assert intent["request_digest"] == job_digest
    assert operation["request_digest"] != intent["request_digest"]
    coordination = db.query(
        "SELECT operation_id, request_digest FROM coordination_jobs WHERE job_id = %s",
        (result.job.job_id,),
    )[0]
    assert coordination["operation_id"] == operation["operation_id"]
    assert coordination["request_digest"] == operation["request_digest"]


def test_same_key_replay_reserves_once_and_returns_the_original(
    admission_database: AdmissionDatabase,
) -> None:
    _seed(admission_database)
    store = _store(admission_database)
    db = admission_database.runtime.foundation.database
    first = store.create(_job())
    assert first.outcome == "created" and first.job is not None
    before = _counts(db)
    second = store.create(_job())
    assert second.outcome == "replayed" and second.job is not None
    assert second.job.job_id == first.job.job_id
    assert _counts(db) == before


def test_capacity_shortage_is_429_and_writes_zero_rows(
    admission_database: AdmissionDatabase,
) -> None:
    _seed(admission_database)
    db = admission_database.runtime.foundation.database
    before = _counts(db)
    db.query("UPDATE workspace_quotas SET held_attempts = max_attempts")
    result = _store(admission_database).create(_job(key="synthetic-idem-capacity"))
    assert result.outcome == "rejected"
    assert result.error_code == "job_capacity_exhausted"
    assert _counts(db) == before


def test_incomplete_binding_is_rejected_without_rows(
    admission_database: AdmissionDatabase,
) -> None:
    _seed(admission_database)
    db = admission_database.runtime.foundation.database
    before = _counts(db)
    result = _store(admission_database).create(
        _job(model=INCOMPLETE, key="synthetic-idem-incomplete")
    )
    assert result.outcome == "rejected"
    assert result.error_code == "job_binding_incomplete"
    assert _counts(db) == before


def test_text_asset_is_explicitly_unsupported(
    admission_database: AdmissionDatabase,
) -> None:
    _seed(admission_database)
    result = _store(admission_database).create(
        _job(input_value={"text_asset_id": "asset-1"}, key="synthetic-idem-text-asset")
    )
    assert result.outcome == "rejected"
    assert result.error_code == "job_text_asset_unsupported"


def test_deadline_uses_the_frozen_binding_timeout_not_queue_wait(
    admission_database: AdmissionDatabase,
) -> None:
    _seed(admission_database)
    db = admission_database.runtime.foundation.database
    before = admission_database.runtime.now()
    result = _store(admission_database).create(_job(queue_wait_ms=1, key="synthetic-idem-deadline"))
    assert result.outcome == "created" and result.job is not None
    deadline = db.query(
        "SELECT deadline_at_us FROM admission_leases WHERE lease_id = %s",
        ("lease-" + result.job.job_id.removeprefix("job-"),),
    )[0]["deadline_at_us"]
    assert before + 59_000_000 <= deadline <= admission_database.runtime.now() + 61_000_000


def test_asr_create_writes_a_complete_command_atomically(
    admission_database: AdmissionDatabase,
) -> None:
    _seed(admission_database)
    db = admission_database.runtime.foundation.database
    db.query(
        "INSERT INTO speech_assets (asset_id, workspace_id, purpose, asset_state, upload_phase, "
        "bytes, sha256, request_digest, object_key, media_type, duration_ms, sample_rate_hz, "
        "channels, created_at_us, expires_at_us) VALUES "
        "('asset-1', %s, 'asr_input', 'ready', 'settled', 1024, %s, %s, 'private/asset-1', "
        "'audio/wav', 1000, 16000, 1, 1, 1000000)",
        (WORKSPACE, DIGEST, DIGEST),
    )
    result = _store(admission_database).create(_job(kind="asr", key="synthetic-idem-asr"))
    assert result.outcome == "created", result.error_code
    command = json.loads(
        db.query("SELECT request_document FROM admission_operations")[0]["request_document"]
    )
    assert command["kind"] == "asr_file"
    assert command["gateway_request"]["command"]["audio"]["media_type"] == "audio/wav"


def test_writer_role_cannot_insert_admission_reservations(
    admission_database: AdmissionDatabase,
) -> None:
    _seed(admission_database)
    db = admission_database.runtime.foundation.database
    schema = db.schema
    writer = "writer_only_" + uuid.uuid4().hex[:10]
    db.query(f"CREATE ROLE \"{writer}\" LOGIN PASSWORD 'synthetic_writer_test'")
    try:
        for grant in (
            *job_runtime_grants(schema, writer),
            *job_dispatch_grants(schema, writer),
            *public_job_grants(schema, writer),
            *voice_draft_grants(schema, writer),
            *public_job_submission_grants(schema, writer),
            f'GRANT SELECT ON "{schema}".speech_workspaces, "{schema}".speech_publications, '
            f'"{schema}".speech_binding_revisions, "{schema}".speech_assets, '
            f'"{schema}".speech_pronunciation_revisions TO "{writer}"',
        ):
            db.query(grant)
        for table in ("admission_attempts", "admission_requests", "admission_leases"):
            row = db.query(
                "SELECT pg_catalog.has_table_privilege(%s, %s, 'INSERT') AS ok",
                (writer, f"{schema}.{table}"),
            )[0]
            assert row["ok"] is False, table
        row = db.query(
            "SELECT pg_catalog.has_column_privilege(%s, %s, 'request_document', 'INSERT') AS ok",
            (writer, f"{schema}.admission_operations"),
        )[0]
        assert row["ok"] is True
        connection = psycopg.connect(
            make_conninfo(db.url, user=writer, password="synthetic_writer_test"),
            autocommit=True,
        )
        try:
            connection.execute(f'SET search_path TO "{schema}", pg_catalog')
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                connection.execute(
                    "INSERT INTO admission_attempts (operation_id, attempt_id, workspace_id, "
                    "binding_revision_id, manifest_id, recipe_digest, policy_revision_id, "
                    "service_class, request_digest, required_stage_ids) VALUES "
                    "('operation-overreach', 'attempt-overreach', %s, %s, 'manifest-test', %s, "
                    "'policy-test', 'realtime', %s, '[]')",
                    (WORKSPACE, BINDING, DIGEST, DIGEST),
                )
        finally:
            connection.close()
    finally:
        db.query(f'DROP OWNED BY "{writer}"')
        db.query(f'DROP ROLE IF EXISTS "{writer}"')


def test_admission_role_startup_matrix_is_satisfied_without_delete(
    admission_database: AdmissionDatabase,
) -> None:
    db = admission_database.runtime.foundation.database
    role = "admission_only_" + uuid.uuid4().hex[:10]
    db.query(f"CREATE ROLE \"{role}\" LOGIN PASSWORD 'synthetic_admission_test'")
    try:
        for grant in public_job_admission_grants(db.schema, role):
            db.query(grant)
        connection = psycopg.connect(
            make_conninfo(db.url, user=role, password="synthetic_admission_test"),
            row_factory=__import__("psycopg.rows", fromlist=["dict_row"]).dict_row,
        )
        try:
            connection.execute(f'SET search_path TO "{db.schema}", pg_catalog')
            _verify_admission_privileges(connection, db.schema)
            for table in ("admission_operations", "admission_attempts", "admission_leases"):
                assert (
                    db.query(
                        "SELECT pg_catalog.has_table_privilege(%s, %s, 'DELETE') AS ok",
                        (role, f"{db.schema}.{table}"),
                    )[0]["ok"]
                    is False
                )
        finally:
            connection.close()
    finally:
        db.query(f'DROP OWNED BY "{role}"')
        db.query(f'DROP ROLE IF EXISTS "{role}"')


def test_concurrent_same_key_reserves_exactly_once(admission_database: AdmissionDatabase) -> None:
    _seed(admission_database)
    db = admission_database.runtime.foundation.database
    before = _counts(db)
    job = _job(key="concurrent-key")

    def attempt() -> Any:
        return _store(admission_database).create(job)

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: attempt(), range(2)))
    assert sorted(result.outcome for result in results) == ["created", "replayed"]
    after = _counts(db)
    for table in (
        "speech_jobs",
        "speech_job_intents",
        "admission_operations",
        "admission_attempts",
        "admission_requests",
        "admission_leases",
        "coordination_jobs",
        "coordination_outbox",
    ):
        assert after[table] == before[table] + 1, table


def test_a_capacity_refusal_does_not_poison_the_same_key_retry(
    admission_database: AdmissionDatabase,
) -> None:
    _seed(admission_database)
    db = admission_database.runtime.foundation.database
    before = _counts(db)
    job = _job(key="retry-after-capacity")
    db.query("UPDATE workspace_quotas SET held_attempts = max_attempts")
    refused = _store(admission_database).create(job)
    assert refused.outcome == "rejected" and refused.error_code == "job_capacity_exhausted"
    assert _counts(db) == before
    db.query("UPDATE workspace_quotas SET held_attempts = 0")
    retried = _store(admission_database).create(job)
    assert retried.outcome == "created", retried.error_code
    assert _counts(db)["speech_jobs"] == before["speech_jobs"] + 1


class _CommitLost:
    def __init__(self, inner: Any) -> None:
        self._inner = inner

    def __enter__(self) -> Any:
        return self._inner.__enter__()

    def __exit__(self, *exc: object) -> None:
        self._inner.__exit__(*exc)
        raise __import__("psycopg").OperationalError("synthetic commit acknowledgement lost")


def test_commit_acknowledgement_loss_reports_unknown_without_half_rows(
    admission_database: AdmissionDatabase,
) -> None:
    _seed(admission_database)
    db = admission_database.runtime.foundation.database
    db = admission_database.runtime.foundation.database
    before = _counts(db)
    store = _store(admission_database)
    real = store._admission_transaction
    store._admission_transaction = lambda deadline: _CommitLost(real(deadline))  # type: ignore[method-assign,assignment,return-value]
    result = store.create(_job(key="commit-lost"))
    assert result.outcome == "unknown" and result.error_code == "outcome_unknown"
    after = _counts(db)
    for table in (
        "speech_jobs",
        "speech_job_intents",
        "admission_operations",
        "admission_leases",
        "coordination_jobs",
    ):
        assert after[table] - before[table] in (0, 1), table
    jobs = after["speech_jobs"] - before["speech_jobs"]
    operations = after["admission_operations"] - before["admission_operations"]
    leases = after["admission_leases"] - before["admission_leases"]
    assert jobs == operations == leases


def test_different_body_under_one_key_is_a_deterministic_conflict(
    admission_database: AdmissionDatabase,
) -> None:
    _seed(admission_database)
    db = admission_database.runtime.foundation.database
    key = "different-body"
    first = _store(admission_database).create(_job(key=key, input_value={"text": "first"}))
    assert first.outcome == "created", first.error_code
    before = _counts(db)
    second = _store(admission_database).create(_job(key=key, input_value={"text": "second"}))
    assert second.outcome == "conflict" and second.error_code == "idempotency_conflict"
    assert _counts(db) == before


def test_no_ready_instance_is_a_capacity_refusal_not_an_unknown_store(
    admission_database: AdmissionDatabase,
) -> None:
    _seed(admission_database)
    db = admission_database.runtime.foundation.database
    before = _counts(db)
    db.query("UPDATE engine_instances SET instance_state = 'draining'")
    result = _store(admission_database).create(_job(key="no-ready-instance"))
    assert result.outcome == "rejected"
    assert result.error_code == "job_capacity_exhausted"
    assert _counts(db) == before


async def test_created_job_is_visible_to_the_a3_claim_join(
    admission_database: AdmissionDatabase,
) -> None:
    _seed(admission_database)
    result = _store(admission_database).create(_job(key="a3-join-key"))
    assert result.outcome == "created" and result.job is not None
    db = admission_database.runtime.foundation.database
    row = db.query(
        "SELECT a.operation_id, a.request_document, a.request_digest, "
        "a.initial_attempt_id, a.current_attempt_id, c.attempt_id "
        "FROM speech_jobs AS j "
        "LEFT JOIN admission_operations AS a ON a.operation_id = j.job_id "
        "LEFT JOIN coordination_jobs AS c ON c.job_id = j.job_id "
        "WHERE j.job_id = %s",
        (result.job.job_id,),
    )[0]
    assert row["operation_id"] == result.job.job_id
    assert row["request_document"]
    assert row["attempt_id"] == row["initial_attempt_id"]
    pool = admission_database.runtime.pool
    assert pool is not None
    claims = await PostgresDispatchQueue(pool).claim_due("worker-a3", 1000, 1)
    assert len(claims) == 1
    claimed = claims[0].job
    assert claimed.job_id == result.job.job_id
    assert claimed.request_document == row["request_document"]
    assert json.loads(claimed.request_document)["kind"] == "tts_file"
    assert (
        db.query(
            "SELECT count(*) AS n FROM admission_leases WHERE operation_id = %s",
            (result.job.job_id,),
        )[0]["n"]
        == 1
    )


def test_writer_column_level_attempt_insert_is_detected(
    admission_database: AdmissionDatabase,
) -> None:
    _seed(admission_database)
    db = admission_database.runtime.foundation.database
    role = admission_database.runtime.foundation.speech_role
    db.query(f'GRANT INSERT (operation_id) ON admission_attempts TO "{role}"')
    assert admission_database.runtime.pool is not None
    with (
        admission_database.runtime.pool.connection(timeout=3) as connection,
        pytest.raises(ValueError, match="admission reservations"),
    ):
        _verify_privileges(connection, db.schema)
