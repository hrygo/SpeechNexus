"""Private receipt durability on PG/InnoDB; synthetic VAD/ASR, real MinIO/codec."""

import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any, cast

import psycopg
import pymysql
import pytest
from alembic import command as migration
from alembic.config import Config
from minio import Minio
from sqlalchemy import create_engine
from sqlalchemy.pool import NullPool
from tests.fake.vad import ScriptedVadModel
from tests.integration.test_coordination_foundation import Foundation, UpgradeSandbox, seed
from tests.integration.test_coordination_foundation import foundation as foundation
from tests.integration.test_coordination_foundation import upgrade_sandbox as upgrade_sandbox
from tests.integration.test_gateway_asr_results import results
from tests.integration.test_gateway_frozen_inputs import (
    LIMITS,
    persist,
    snapshot,
    upload_command,
)
from tests.integration.test_gateway_frozen_inputs import object_store as object_store
from tests.integration.test_gateway_recovery import Ledger
from tests.integration.test_gateway_recovery import recovery_ledger as recovery_ledger
from tests.integration.test_gateway_stop_evidence import counters, new_pool
from tests.integration.test_gateway_stop_evidence import stop_ledger as stop_ledger
from tests.integration.test_production_jobs import ROOT, JobDatabase
from tests.integration.test_production_jobs import database as database
from tests.support.gateway_database import MySQLSubmissionLedger
from tests.support.migrations import HEAD
from tests.unit.gateway.test_file_asr import Recognizer

from speechnexus_gateway.application.execution_inputs import FileExecutionInputs
from speechnexus_gateway.domain.asr_results import PreparedAsrResult
from speechnexus_gateway.domain.execution_results import ExecutionStagingResult
from speechnexus_gateway.domain.media import AudioDecodeLimits
from speechnexus_gateway.domain.observation import ExecutionObservation, RecordExecutionObservation
from speechnexus_gateway.domain.submission import (
    ExecutionCleanupPendingError,
    ExecutionRejectedError,
    ExecutionState,
    StaleSubmissionVersionError,
    SubmissionIntent,
    SubmissionOutcomeUnknownError,
    SubmissionStoreError,
)
from speechnexus_gateway.domain.vad import VadPolicy
from speechnexus_gateway.infrastructure import observation_sql
from speechnexus_gateway.infrastructure.execution_objects_minio import MinioExecutionInputs
from speechnexus_gateway.infrastructure.observation_sql import (
    record_execution_observation_in_transaction,
)
from speechnexus_gateway.infrastructure.pyav_audio import PyAvAudioDecoder
from speechnexus_gateway.infrastructure.submit_postgres import PostgresSubmissionLedger
from speechnexus_gateway.infrastructure.submit_sql import save_staging_result
from speechnexus_gateway.interfaces.file_asr_command import CommandFileAsr

pytestmark = pytest.mark.integration


def start(f: Foundation, ledger: Ledger) -> tuple[Any, SubmissionIntent]:
    request, intent = persist(f, ledger)
    intent.mark_dispatched("engine-synthetic", at=datetime.now(UTC))
    saved = ledger.record_execution_observation(
        RecordExecutionObservation.from_intent(intent, expected_version=1)
    )
    return request, saved


def receipt(intent: SubmissionIntent) -> ExecutionStagingResult:
    prepared = PreparedAsrResult(
        intent.identity.workspace_id, intent.request_digest, b'{"text":""}'
    )
    return ExecutionStagingResult(
        intent.request_digest, prepared.reference("synthetic-private"), "synthetic-version"
    )


def registration(intent: SubmissionIntent) -> RecordExecutionObservation:
    return RecordExecutionObservation(
        ExecutionObservation.from_intent(intent), intent.version, receipt(intent)
    )


async def test_frozen_command_through_real_storage_to_durable_receipt(
    foundation: Foundation,
    recovery_ledger: Ledger,
    object_store: tuple[MinioExecutionInputs, Minio, str],
) -> None:
    inputs, admin, bucket = object_store
    command, _ = upload_command(admin, bucket)
    command["parameters"]["max_segment_ms"] = 1000
    request, initial = persist(foundation, recovery_ledger, command)
    initial.mark_dispatched("engine-synthetic", at=datetime.now(UTC))
    current = recovery_ledger.record_execution_observation(
        RecordExecutionObservation.from_intent(initial, expected_version=1)
    )
    restored = recovery_ledger.read_request(current, max_document_bytes=16384)
    recognizer = Recognizer()
    recognizer.binding_revision_id = current.identity.binding_revision_id
    consumer = CommandFileAsr(
        inputs=FileExecutionInputs(inputs),
        decoder=PyAvAudioDecoder(),
        vad_model=ScriptedVadModel(fallback=0.9),
        recognizer=recognizer,
        vad_tuning=VadPolicy(current.identity.policy_revision_id),
        max_completion_tokens=64,
        max_call_seconds=2,
        input_limits=LIMITS,
        decode_limits=AudioDecodeLimits(),
    )
    before = counters(foundation)
    store = results(bucket)
    try:
        staged = await consumer.stage_result(
            restored, store=store, max_result_bytes=4096, cancelled=lambda: False
        )
    finally:
        store.close()
    result = ExecutionStagingResult(staged.request_digest, staged.reference, staged.storage_version)
    saved = recovery_ledger.record_execution_observation(
        RecordExecutionObservation(
            ExecutionObservation.from_intent(current), current.version, result
        )
    )
    # A separate repository/connection reconstructs the complete receipt from DB,
    # not from the coroutine, object-store client or process-local cache.
    if foundation.database.dialect == "postgresql":
        pool = new_pool(foundation)
        try:
            recovered = PostgresSubmissionLedger(pool).query_outcome(
                saved.identity.submit_intent_id
            )
        finally:
            pool.close()
    else:
        recovered = MySQLSubmissionLedger(foundation).query_outcome(saved.identity.submit_intent_id)
    assert recovered is not None
    assert recovered.staging_response()["staging_result"] == result.value()
    assert recovered.intent.request_digest == request.request_digest
    assert recovered.intent.execution_state is ExecutionState.EXECUTING
    assert recovered.intent.stop_evidence_digest is None and recovered.intent.terminated_at is None
    assert counters(foundation) == before
    assert len(recognizer.requests) == 2


def test_duplicate_and_concurrent_registration_never_changes_result_or_budget_twice(
    foundation: Foundation, recovery_ledger: Ledger
) -> None:
    _, current = start(foundation, recovery_ledger)
    write = registration(current)
    before = counters(foundation)

    def run() -> str:
        try:
            recovery_ledger.record_execution_observation(write)
            return "committed"
        except StaleSubmissionVersionError:
            return "stale"

    with ThreadPoolExecutor(max_workers=2) as executor:
        assert sorted(executor.map(lambda _: run(), range(2))) == ["committed", "stale"]
    saved = recovery_ledger.get(current.identity.submit_intent_id)
    assert saved is not None and saved.version == current.version + 1
    replay = replace(write, expected_version=saved.version)
    assert recovery_ledger.record_execution_observation(replay) == saved
    assert counters(foundation) == before


@pytest.mark.parametrize("database", ["postgresql"], indirect=True)
def test_receipt_commit_ack_loss_recovers_original_without_a_second_write(
    foundation: Foundation, monkeypatch: pytest.MonkeyPatch
) -> None:
    pool = new_pool(foundation)
    try:
        ledger = PostgresSubmissionLedger(pool)
        _, current = start(foundation, ledger)
        write = registration(current)
        before = counters(foundation)
        real = pool.connection
        calls = 0

        @contextmanager
        def lose_ack(*args: Any, **kwargs: Any) -> Iterator[Any]:
            nonlocal calls
            calls += 1
            with real(*args, **kwargs) as connection:
                yield connection
            # The real transaction has committed before its acknowledgment is lost.
            raise psycopg.OperationalError("synthetic lost receipt commit acknowledgment")

        with monkeypatch.context() as patch:
            patch.setattr(pool, "connection", lose_ack)
            with pytest.raises(SubmissionOutcomeUnknownError):
                ledger.record_execution_observation(write)
        assert calls == 1
        recovered = ledger.query_outcome(current.identity.submit_intent_id)
        assert recovered is not None
        assert recovered.intent.version == current.version + 1
        assert recovered.intent.staging_result == write.staging_result
        assert recovered.staging_response()["staging_result"] == receipt(current).value()
        assert recovered.intent.execution_state is ExecutionState.EXECUTING
        assert recovered.intent.stop_evidence_digest is None
        assert recovered.intent.terminated_at is None
        committed = snapshot(foundation)
        with pytest.raises(StaleSubmissionVersionError):
            ledger.record_execution_observation(write)
        replay = ledger.record_execution_observation(
            replace(write, expected_version=recovered.intent.version)
        )
        assert replay == recovered.intent
        assert snapshot(foundation) == committed
        assert counters(foundation) == before
    finally:
        pool.close()


def test_cancel_before_registration_rejects_late_result(
    foundation: Foundation, recovery_ledger: Ledger
) -> None:
    _, current = start(foundation, recovery_ledger)
    stopping = replace(current)
    stopping.execution_state = ExecutionState.STOPPING
    cancelled = recovery_ledger.record_execution_observation(
        RecordExecutionObservation.from_intent(stopping, expected_version=current.version)
    )
    before = snapshot(foundation)
    with pytest.raises(ExecutionCleanupPendingError):
        recovery_ledger.record_execution_observation(registration(cancelled))
    assert snapshot(foundation) == before


def test_existing_execution_can_register_during_drain_without_releasing_budget(
    foundation: Foundation, recovery_ledger: Ledger
) -> None:
    _, current = start(foundation, recovery_ledger)
    foundation.database.query("UPDATE engine_instances SET instance_state = 'draining'")
    before = counters(foundation)
    saved = recovery_ledger.record_execution_observation(registration(current))
    assert saved.staging_result == receipt(current)
    assert saved.execution_state is ExecutionState.EXECUTING
    assert saved.stop_evidence_digest is None and saved.terminated_at is None
    assert counters(foundation) == before


def test_current_version_result_replay_preserves_a_later_cancel(
    foundation: Foundation, recovery_ledger: Ledger
) -> None:
    _, current = start(foundation, recovery_ledger)
    write = registration(current)
    saved = recovery_ledger.record_execution_observation(write)
    stopping = replace(saved, execution_state=ExecutionState.STOPPING)
    cancelled = recovery_ledger.record_execution_observation(
        RecordExecutionObservation.from_intent(stopping, expected_version=saved.version)
    )
    foundation.database.query("UPDATE engine_instances SET instance_state = 'stopping'")
    before = snapshot(foundation)
    replay = recovery_ledger.record_execution_observation(
        replace(write, expected_version=cancelled.version)
    )
    assert replay == cancelled
    assert snapshot(foundation) == before


@pytest.mark.parametrize("change", ["fenced", "stopping", "lease"])
def test_first_registration_rechecks_current_authority(
    foundation: Foundation, recovery_ledger: Ledger, change: str
) -> None:
    _, current = start(foundation, recovery_ledger)
    if change == "fenced":
        foundation.database.query(
            "UPDATE engine_instances SET last_fencing_token = %s",
            (current.identity.fencing_token + 1,),
        )
    elif change == "stopping":
        foundation.database.query("UPDATE engine_instances SET instance_state = 'stopping'")
    else:
        foundation.database.query(
            "UPDATE admission_leases SET lease_state = 'stopping' WHERE lease_id = %s",
            (current.identity.lease_id,),
        )
    before = snapshot(foundation)
    with pytest.raises(ExecutionRejectedError):
        recovery_ledger.record_execution_observation(registration(current))
    assert snapshot(foundation) == before


def test_receipt_and_version_roll_back_together_after_metadata_write(
    foundation: Foundation, recovery_ledger: Ledger, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, current = start(foundation, recovery_ledger)
    original = save_staging_result

    def fail(*args: Any) -> None:
        original(*args)
        raise SubmissionStoreError("synthetic failure after receipt write")

    before = snapshot(foundation)
    monkeypatch.setattr(observation_sql, "save_staging_result", fail)
    with pytest.raises(SubmissionStoreError):
        recovery_ledger.record_execution_observation(registration(current))
    assert snapshot(foundation) == before


def test_private_result_is_not_readable_by_speech_database_role(
    foundation: Foundation, recovery_ledger: Ledger
) -> None:
    _, current = start(foundation, recovery_ledger)
    recovery_ledger.record_execution_observation(registration(current))
    with (
        foundation.connection("speech") as connection,
        connection.cursor() as cursor,
        pytest.raises((psycopg.Error, pymysql.MySQLError)) as denied,
    ):
        cursor.execute("SELECT staging_result_document FROM submit_intents")
    if isinstance(denied.value, psycopg.Error):
        assert denied.value.sqlstate == "42501"
    else:
        assert denied.value.args[0] in {1142, 1143}


def test_legacy_key_is_not_backfilled_and_corrupt_receipt_is_not_projected(
    foundation: Foundation, recovery_ledger: Ledger
) -> None:
    _, current = start(foundation, recovery_ledger)
    foundation.database.query(
        "UPDATE submit_intents SET staging_output_object_key = 'synthetic-legacy'"
    )
    legacy = recovery_ledger.query_outcome(current.identity.submit_intent_id)
    assert legacy is not None and legacy.staging_response()["staging_result"] is None
    with pytest.raises(ExecutionCleanupPendingError):
        recovery_ledger.record_execution_observation(registration(legacy.intent))
    foundation.database.query(
        "UPDATE submit_intents SET staging_result_document = 'synthetic-corrupt'"
    )
    with pytest.raises(SubmissionStoreError):
        recovery_ledger.query_outcome(current.identity.submit_intent_id)


@pytest.mark.parametrize("database", ["postgresql"], indirect=True)
def test_additive_migration_preserves_legacy_rows_and_refuses_result_history_loss(
    upgrade_sandbox: UpgradeSandbox,
) -> None:
    sandbox = upgrade_sandbox
    sandbox.upgrade("0011_binding_parameter_defaults")

    class SeedConnection:
        def query(self, statement: str, parameters: tuple[Any, ...] = ()) -> list[Any]:
            cursor = sandbox.admin.execute(statement, parameters)
            return [] if cursor.description is None else cursor.fetchall()

    seed(cast(JobDatabase, SeedConnection()))
    sandbox.admin.execute(
        "UPDATE submit_intents SET staging_output_object_key = 'synthetic-legacy'"
    )
    before = sandbox.admin.execute("SELECT * FROM submit_intents").fetchall()
    sandbox.upgrade()
    sandbox.upgrade()
    after = sandbox.admin.execute("SELECT * FROM submit_intents").fetchall()
    assert sandbox.version() == HEAD
    assert after[0].pop("staging_result_document") is None
    assert after == before
    # An unreadable old receipt is still history: migration must not discard it.
    sandbox.admin.execute("UPDATE submit_intents SET staging_result_document = 'synthetic-history'")
    preserved = sandbox.admin.execute("SELECT * FROM submit_intents").fetchall()
    with sandbox.engine.connect() as connection:
        sandbox.config.attributes["connection"] = connection
        with pytest.raises(RuntimeError, match=r"history.*preservation"):
            migration.downgrade(sandbox.config, "0011_binding_parameter_defaults")
    assert sandbox.version() == HEAD
    assert sandbox.admin.execute("SELECT * FROM submit_intents").fetchall() == preserved


@pytest.mark.parametrize("database", ["postgresql"], indirect=True)
def test_downgrade_waits_for_in_flight_result_and_preserves_committed_receipt(
    foundation: Foundation,
    recovery_ledger: Ledger,
) -> None:
    _, current = start(foundation, recovery_ledger)
    db = foundation.database
    application = "sn_result_downgrade_" + db.schema

    def downgrade() -> None:
        engine = create_engine(
            db.url.replace("postgresql://", "postgresql+psycopg://", 1),
            poolclass=NullPool,
            hide_parameters=True,
            connect_args={"application_name": application},
        )
        config = Config(str(ROOT / "alembic.ini"))
        config.attributes["schema"] = db.schema
        try:
            with engine.connect() as connection:
                config.attributes["connection"] = connection
                migration.downgrade(config, "0011_binding_parameter_defaults")
        finally:
            engine.dispose()

    with ThreadPoolExecutor(max_workers=1) as workers:
        with (
            foundation.connection("gateway") as connection,
            connection.transaction(),
            connection.cursor() as cursor,
        ):
            saved = record_execution_observation_in_transaction(
                cursor, registration(current), clock=lambda: datetime.now(UTC)
            )
            pending = workers.submit(downgrade)
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                if db.query(
                    "SELECT pid FROM pg_stat_activity WHERE application_name = %s "
                    "AND cardinality(pg_blocking_pids(pid)) > 0",
                    (application,),
                ):
                    break
                assert not pending.done(), "downgrade must wait for the uncommitted result"
                time.sleep(0.01)
            else:
                pytest.fail("the actual result/downgrade lock wait was not observed")
        with pytest.raises(RuntimeError, match=r"history.*preservation"):
            pending.result(timeout=5)
    assert recovery_ledger.get(saved.identity.submit_intent_id) == saved
    assert db.query("SELECT version_num FROM alembic_version") == [{"version_num": HEAD}]
