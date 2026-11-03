"""PostgreSQL-only TTS persistence and voice retirement serialization.

This separate fixture is not the dual-database coordination acceptance gate.
"""

from __future__ import annotations

import os
import time
import uuid
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row
from sqlalchemy import create_engine
from sqlalchemy.pool import NullPool
from tests.integration.test_coordination_foundation import foundation as foundation
from tests.integration.test_production_admission import AdmissionDatabase
from tests.integration.test_production_admission import admission_database as admission_database
from tests.integration.test_production_jobs import JobDatabase
from tests.integration.test_production_runtime_intents import runtime_database as runtime_database
from tests.integration.test_transcription_disable_barrier_pg import _seed
from tests.unit.backend.test_tts_durable_intent import plan

from speechnexus_backend.speech.application.admission import AdmissionService
from speechnexus_backend.speech.application.speech_synthesis import SpeechUnavailableError
from speechnexus_backend.speech.infrastructure.admission_postgres import (
    PostgresAdmissionTransactions,
    tts_voice_admission_grants,
)
from speechnexus_backend.speech.infrastructure.job_schema import (
    job_dispatch_grants,
    job_runtime_grants,
)
from speechnexus_backend.speech.infrastructure.jobs_postgres import (
    PostgresJobTransactions,
    job_pool,
)
from speechnexus_backend.speech.infrastructure.tts_admission import (
    PostgresOriginalTtsCommands,
    PostgresTtsAdmission,
    PostgresTtsFacts,
    TtsRecipe,
)
from speechnexus_backend.speech.infrastructure.tts_settlement_schema import (
    tts_settlement_writer_grants,
)

pytestmark = pytest.mark.integration
ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def database() -> Iterator[JobDatabase]:
    url = os.environ.get("SPEECHNEXUS_TEST_DATABASE_URL")
    if not url:
        pytest.skip("Isolated PostgreSQL URL required")
    suffix = uuid.uuid4().hex[:12]
    schema, role = "sn_tts_" + suffix, "sn_tts_role_" + suffix
    engine = create_engine(
        url.replace("postgresql://", "postgresql+psycopg://", 1),
        poolclass=NullPool,
        hide_parameters=True,
    )
    config = Config(str(ROOT / "alembic.ini"))
    config.attributes["schema"] = schema
    try:
        with engine.connect() as connection:
            config.attributes["connection"] = connection
            command.upgrade(config, "head")
    finally:
        engine.dispose()
    admin = psycopg.connect(url, row_factory=dict_row, autocommit=True)
    pool = None
    role_created = False
    try:
        admin.execute(f'SET search_path TO "{schema}", pg_catalog')
        admin.execute(f"CREATE ROLE \"{role}\" LOGIN PASSWORD 'synthetic_job_test'")
        role_created = True
        for grant in job_runtime_grants(schema, role) + job_dispatch_grants(schema, role):
            admin.execute(grant)
        pool = job_pool(make_conninfo(url, user=role, password="synthetic_job_test"), schema=schema)
        pool.open()
        yield JobDatabase(
            "postgresql", url, schema, role, admin, PostgresJobTransactions(pool), pool
        )
    finally:
        if pool is not None:
            pool.close()
        admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
        if role_created:
            admin.execute(f'DROP ROLE "{role}"')
        admin.close()


def adapter(admission: AdmissionDatabase, *, seed: bool = True) -> PostgresTtsAdmission:
    db = admission.runtime.foundation.database
    if seed:
        _seed(admission)
        db.query(
            "INSERT INTO speech_publications (kind,object_id,revision,revision_id,published_at_us) "
            "VALUES ('voice','voice-test','2','voice-revision-test',1)"
        )
        role = admission.runtime.foundation.speech_role
        for grant in (
            *tts_voice_admission_grants(db.schema, role),
            *tts_settlement_writer_grants(db.schema, role),
        ):
            db.query(grant)
    pool = admission.runtime.pool
    assert pool is not None
    # The transaction/retirement test does not claim this synthetic callback is qualification.
    port = PostgresAdmissionTransactions(
        pool,
        max_command_bytes=1_048_576,
        timeout_seconds=10,
        require_builtin_voice=lambda *a, **k: None,
    )
    recipe = TtsRecipe(
        "model-test",
        "stage-main",
        "instance-new",
        1,
        replace(
            admission.command.approved_recipe,
            admission_expires_at_us=admission.runtime.now() + 60_000_000,
        ),
    )
    return PostgresTtsAdmission(
        PostgresTtsFacts(pool), AdmissionService(port), PostgresOriginalTtsCommands(pool), recipe
    )


def test_postgres_persists_and_recovers_original(admission_database: AdmissionDatabase) -> None:
    service = adapter(admission_database)
    request = service.admit(plan())
    row = admission_database.runtime.foundation.database.query(
        "SELECT request_document FROM admission_operations WHERE operation_id=%s",
        (request["operation_id"],),
    )[0]
    assert "Synthetic text." in row["request_document"]
    roots = admission_database.runtime.foundation.database.query(
        "SELECT target_document FROM speech_tts_settlement"
    )
    assert len(roots) == 1
    assert "Synthetic text." not in roots[0]["target_document"]
    restarted = adapter(admission_database, seed=False)
    restored_plan, restored_request = restarted.recover("request-test")
    assert restored_plan == plan()
    assert restored_request == request
    db = admission_database.runtime.foundation.database
    db.query(
        "INSERT INTO speech_publication_retirements "
        "(kind,object_id,revision,revision_id,retired_at_us) "
        "VALUES ('voice','voice-test','2','voice-revision-test',2)"
    )
    assert restarted.admit(plan()) == request
    assert db.query("SELECT count(*) AS n FROM speech_tts_settlement")[0]["n"] == 1
    with pytest.raises(SpeechUnavailableError):
        restarted.admit(replace(plan(), request_id="request-new"))
    assert db.query("SELECT count(*) AS n FROM admission_operations")[0]["n"] == 1


def test_retirement_waits_and_new_admission_rechecks(admission_database: AdmissionDatabase) -> None:
    service = adapter(admission_database)
    db = admission_database.runtime.foundation.database
    blocker = db.blocker()
    try:
        blocker.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended("
            "'speechnexus:catalog:' || current_schema() || ':voice:' || %s, 0))",
            ("voice-test",),
        )
        with ThreadPoolExecutor(max_workers=1) as worker:
            pending = worker.submit(service.admit, plan())
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                if db.query(
                    "SELECT pid FROM pg_stat_activity WHERE %s=ANY(pg_blocking_pids(pid))",
                    (blocker.info.backend_pid,),
                ):
                    break
                if pending.done():
                    pending.result()
                    pytest.fail("admission completed before the voice lock was released")
                time.sleep(0.01)
            else:
                pytest.fail("no admission transaction waited on the voice lock")
            blocker.execute(
                "INSERT INTO speech_publication_retirements "
                "(kind,object_id,revision,revision_id,retired_at_us) "
                "VALUES ('voice','voice-test','2','voice-revision-test',2)"
            )
            blocker.commit()
            with pytest.raises(SpeechUnavailableError):
                pending.result(timeout=10)
        assert db.query("SELECT count(*) AS n FROM admission_operations")[0]["n"] == 0
    finally:
        blocker.close()


def test_settlement_root_failure_rolls_back_entire_admission(
    admission_database: AdmissionDatabase,
) -> None:
    service = adapter(admission_database)
    db = admission_database.runtime.foundation.database
    role = admission_database.runtime.foundation.speech_role
    db.query(f'REVOKE INSERT ON speech_tts_settlement FROM "{role}"')
    tables = (
        "admission_operations",
        "admission_attempts",
        "admission_leases",
        "speech_tts_settlement",
    )
    before = {table: db.query(f"SELECT count(*) AS n FROM {table}")[0]["n"] for table in tables}
    with pytest.raises((SpeechUnavailableError, psycopg.Error)):
        service.admit(plan())
    for table in tables:
        assert db.query(f"SELECT count(*) AS n FROM {table}")[0]["n"] == before[table]


@pytest.mark.parametrize("cancel_first", [True, False])
def test_cancellation_is_persisted_across_admission_order(
    admission_database: AdmissionDatabase, cancel_first: bool
) -> None:
    from speechnexus_backend.speech.infrastructure.postgres_pool import speech_pool
    from speechnexus_backend.speech.infrastructure.tts_admission import _identities
    from speechnexus_backend.speech.infrastructure.tts_settlement_postgres import (
        PostgresTtsSettlementStore,
    )

    service = adapter(admission_database)
    db = admission_database.runtime.foundation.database
    pool = speech_pool(db.url, schema=db.schema, name="tts-cancel-test")
    pool.open()
    try:
        store = PostgresTtsSettlementStore(pool)
        operation_id = _identities("request-test")[0].operation_id
        if cancel_first:
            store.request_cancellation(operation_id, "client_cancelled")
        service.admit(plan())
        if not cancel_first:
            store.request_cancellation(operation_id, "client_cancelled")
        store.request_cancellation(operation_id, "stream_disconnected")
        row = db.query("SELECT abort_reason FROM speech_tts_settlement")[0]
        assert row["abort_reason"] == "client_cancelled"
        assert (
            db.query("SELECT reason FROM speech_tts_cancellation")[0]["reason"]
            == "client_cancelled"
        )
    finally:
        pool.close()


def test_cancellation_waits_for_operation_lock(admission_database: AdmissionDatabase) -> None:
    from speechnexus_backend.speech.infrastructure.postgres_pool import speech_pool
    from speechnexus_backend.speech.infrastructure.tts_admission import _identities
    from speechnexus_backend.speech.infrastructure.tts_settlement_postgres import (
        PostgresTtsSettlementStore,
    )

    adapter(admission_database)
    db = admission_database.runtime.foundation.database
    operation_id = _identities("request-test")[0].operation_id
    pool = speech_pool(db.url, schema=db.schema, name="tts-cancel-lock-test")
    pool.open()
    blocker = db.blocker()
    try:
        blocker.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended('speechnexus:tts-cancel:' "
            "|| current_schema() || ':' || %s, 0))",
            (operation_id,),
        )
        with ThreadPoolExecutor(max_workers=1) as executor:
            pending = executor.submit(
                PostgresTtsSettlementStore(pool).request_cancellation,
                operation_id,
                "client_cancelled",
            )
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                if db.query(
                    "SELECT pid FROM pg_stat_activity WHERE %s=ANY(pg_blocking_pids(pid))",
                    (blocker.info.backend_pid,),
                ):
                    break
                if pending.done():
                    pytest.fail("cancellation did not wait for operation lock")
                time.sleep(0.01)
            else:
                pytest.fail("no cancellation transaction waited for operation lock")
            blocker.commit()
            pending.result(timeout=3)
        assert (
            db.query("SELECT reason FROM speech_tts_cancellation")[0]["reason"]
            == "client_cancelled"
        )
    finally:
        blocker.close()
        pool.close()


@pytest.mark.parametrize("stopped", [False, True])
async def test_deadline_rescan_releases_unsubmitted_original_and_allows_next_admission(
    admission_database: AdmissionDatabase, stopped: bool
) -> None:
    from speechnexus_backend.speech.application.leases import LeaseService
    from speechnexus_backend.speech.application.tts_settlement import TtsSettlementWorker
    from speechnexus_backend.speech.infrastructure.binding_disable_targets_schema import (
        disable_settlement_grants,
    )
    from speechnexus_backend.speech.infrastructure.leases_postgres import PostgresLeaseTransactions
    from speechnexus_backend.speech.infrastructure.postgres_pool import speech_pool
    from speechnexus_backend.speech.infrastructure.tts_settlement_postgres import (
        PostgresTtsSettlementStore,
    )
    from speechnexus_backend.speech.infrastructure.tts_settlement_schema import (
        tts_settlement_worker_grants,
    )
    from speechnexus_backend.tts_settlement_worker import _verify_worker

    service = adapter(admission_database)
    first = service.admit(plan())
    db = admission_database.runtime.foundation.database
    db.query("UPDATE speech_tts_settlement SET deadline_at_us=1")
    if stopped:
        from datetime import datetime

        from speechnexus_contracts.execution_canonical import effective_execution_request
        from tests.integration.test_gateway_observation import observed_request
        from tests.integration.test_gateway_stop_evidence import evidence_for, new_pool

        from speechnexus_gateway.domain.submission import (
            ExecutionState,
            RecordStop,
            StopEvidenceKind,
            SubmissionIdentity,
            SubmissionIntent,
            SubmitRequest,
        )
        from speechnexus_gateway.infrastructure.submit_postgres import PostgresSubmissionLedger
        from speechnexus_gateway.infrastructure.submit_sql import from_unix_micros

        fields = {
            key: first[key]
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
        }
        identity = SubmissionIdentity(
            **cast(Any, fields),
            deadline_at=datetime.fromisoformat(first["deadline_at"].replace("Z", "+00:00")),
        )
        effective = effective_execution_request(first)
        submit = SubmitRequest(identity, cast(Any, first["command"]), effective["output_stream_id"])
        at = from_unix_micros(admission_database.runtime.now())
        intent = SubmissionIntent(
            identity, at, at, submit.request_digest, output_stream_id=submit.output_stream_id
        )
        gateway_pool = new_pool(admission_database.runtime.foundation)
        try:
            ledger = PostgresSubmissionLedger(gateway_pool)
            ledger.add(intent, request_document=submit.request_document)
            ledger.record_execution_observation(observed_request(intent))
            ledger.record_stop(
                RecordStop(
                    evidence_for(
                        admission_database.runtime.foundation,
                        submit_intent_id=identity.submit_intent_id,
                        evidence_kind=StopEvidenceKind.ENGINE_TERMINAL_RESPONSE,
                    ),
                    ExecutionState.SUCCEEDED,
                )
            )
        finally:
            gateway_pool.close()
    role = "sn_tts_settle_" + uuid.uuid4().hex[:12]
    db.query(f"CREATE ROLE \"{role}\" LOGIN PASSWORD 'synthetic_settlement_test'")
    for grant in (
        *disable_settlement_grants(db.schema, role),
        *tts_settlement_worker_grants(db.schema, role),
    ):
        db.query(grant)
    root_pool = speech_pool(
        make_conninfo(db.url, user=role, password="synthetic_settlement_test"),
        schema=db.schema,
        name="tts-settlement-test",
    )
    root_pool.open()

    class UnavailableGateway:
        async def query_tts_target(self, target: object) -> object:
            raise RuntimeError("synthetic unavailable")

        async def abort_tts(self, target: object, *, reason: str, wait_timeout_ms: int) -> object:
            raise RuntimeError("synthetic unavailable")

    try:
        _verify_worker(root_pool, db.schema)
        store = PostgresTtsSettlementStore(root_pool)
        worker = TtsSettlementWorker(
            store,
            UnavailableGateway(),
            LeaseService(PostgresLeaseTransactions(root_pool)),
            gateway_id=first["gateway_id"],
            node_id=first["host_node_id"],
        )
        result = await worker.run_once()
        assert result.released == 1
        assert db.query("SELECT settled_at_us FROM speech_tts_settlement")[0]["settled_at_us"]
        second = service.admit(replace(plan(), request_id="request-second"))
        assert second["lease_id"] != first["lease_id"]
        db.query(f'GRANT UPDATE (target_document) ON speech_tts_settlement TO "{role}"')
        with pytest.raises(ValueError, match="worker boundary"):
            _verify_worker(root_pool, db.schema)
    finally:
        root_pool.close()
        db.query(f'DROP OWNED BY "{role}"')
        db.query(f'DROP ROLE "{role}"')


def test_worker_lifecycle_with_distinct_restricted_pg_identity(
    admission_database: AdmissionDatabase,
) -> None:
    from speechnexus_backend.speech.infrastructure.binding_disable_targets_schema import (
        disable_settlement_grants,
    )
    from speechnexus_backend.speech.infrastructure.tts_settlement_schema import (
        tts_settlement_worker_grants,
    )
    from speechnexus_backend.tts_settlement_worker import (
        TtsSettlementRuntime,
        TtsSettlementWorkerSettings,
    )

    db = admission_database.runtime.foundation.database
    role = "sn_tts_life_" + uuid.uuid4().hex[:12]
    db.query(f"CREATE ROLE \"{role}\" LOGIN PASSWORD 'synthetic_settlement_test'")
    for grant in (
        *disable_settlement_grants(db.schema, role),
        *tts_settlement_worker_grants(db.schema, role),
    ):
        db.query(grant)
    settings = TtsSettlementWorkerSettings.model_validate(
        {
            "SPEECHNEXUS_DATABASE_URL": db.url,
            "SPEECHNEXUS_TTS_SETTLEMENT_DATABASE_URL": make_conninfo(
                db.url, user=role, password="synthetic_settlement_test"
            ),
            "SPEECHNEXUS_DATABASE_SCHEMA": db.schema,
            "SPEECHNEXUS_TTS_GATEWAY_ID": "gateway-test",
            "SPEECHNEXUS_TTS_HOST_NODE_ID": "host-test",
            "SPEECHNEXUS_EXECUTION_GATEWAY_BASE_URL": "http://127.0.0.1:9",
            "SPEECHNEXUS_EXECUTION_GATEWAY_COMPONENT_CREDENTIAL": "synthetic-test-token",
        }
    )
    runtime = TtsSettlementRuntime(settings)
    try:
        store = runtime.open()
        assert store.due("gateway-test", "host-test", 1) == []
        runtime.close()
    finally:
        runtime.close()
        db.query(f'DROP OWNED BY "{role}"')
        db.query(f'DROP ROLE "{role}"')


def test_worker_fails_closed_when_admission_and_settlement_roles_match(
    admission_database: AdmissionDatabase,
) -> None:
    from speechnexus_backend.tts_settlement_worker import (
        TtsSettlementRuntime,
        TtsSettlementWorkerSettings,
    )

    db = admission_database.runtime.foundation.database
    settings = TtsSettlementWorkerSettings.model_validate(
        {
            "SPEECHNEXUS_DATABASE_URL": db.url,
            "SPEECHNEXUS_TTS_SETTLEMENT_DATABASE_URL": db.url,
            "SPEECHNEXUS_DATABASE_SCHEMA": db.schema,
            "SPEECHNEXUS_TTS_GATEWAY_ID": "gateway-test",
            "SPEECHNEXUS_TTS_HOST_NODE_ID": "host-test",
            "SPEECHNEXUS_EXECUTION_GATEWAY_BASE_URL": "http://127.0.0.1:9",
            "SPEECHNEXUS_EXECUTION_GATEWAY_COMPONENT_CREDENTIAL": "synthetic-test-token",
        }
    )
    runtime = TtsSettlementRuntime(settings)
    with pytest.raises(ValueError, match="failed to start"):
        runtime.open()
    runtime.close()


def test_preflight_rejects_original_unsettled_tts_without_root(
    admission_database: AdmissionDatabase,
) -> None:
    from speechnexus_backend.speech.infrastructure.tts_settlement_preflight import (
        require_complete_tts_roots,
    )

    service = adapter(admission_database)
    service.admit(plan())
    assert admission_database.runtime.pool is not None
    require_complete_tts_roots(admission_database.runtime.pool)
    db = admission_database.runtime.foundation.database
    db.query("DELETE FROM speech_tts_settlement")
    with pytest.raises(ValueError, match="lacks a settlement root"):
        require_complete_tts_roots(admission_database.runtime.pool)
    db.query(
        "UPDATE admission_operations SET request_document="
        "jsonb_set(request_document::jsonb, '{kind}', '\"other\"'::jsonb)::text "
        "WHERE request_document::jsonb->>'kind'='tts_file'"
    )
    with pytest.raises(ValueError, match="lacks a settlement root"):
        require_complete_tts_roots(admission_database.runtime.pool)
