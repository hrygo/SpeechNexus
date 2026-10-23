"""Real PostgreSQL submit ledger: durable intent, replay, versions and lock order.

Every call runs as the non-owner gateway login, which only holds the reviewed column
grants, so these tests also show the adapter works without table-wide UPDATE, DELETE or
DDL. InnoDB carries the shared schema and constraint oracle only; the PostgreSQL adapter
is skipped there instead of being approximated.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
import pytest
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row
from tests.fake.execution import ScriptedEngine
from tests.integration.test_coordination_foundation import Foundation
from tests.integration.test_coordination_foundation import foundation as foundation
from tests.integration.test_production_jobs import database as database

from speechnexus_gateway.application.submit_execution import SubmitExecutionService
from speechnexus_gateway.domain.canonical import JsonValue, command_digest
from speechnexus_gateway.domain.submission import (
    ExecutionState,
    GatewayBarrierFencedError,
    InvalidExecutionRequestError,
    LeaseNotActiveError,
    StaleSubmissionVersionError,
    StopEvidenceKind,
    SubmissionIdentity,
    SubmissionIntentExistsError,
    SubmissionStoreError,
    SubmitRequest,
    VerifiedStopEvidence,
)
from speechnexus_gateway.infrastructure.postgres_pool import GatewayPool, gateway_pool
from speechnexus_gateway.infrastructure.submit_postgres import PostgresSubmissionLedger

pytestmark = pytest.mark.integration

DIGEST = "sha256:" + "a" * 64
PASSWORD = "synthetic_foundation_test"
NOW = datetime(2026, 10, 24, tzinfo=UTC)
COMMAND: JsonValue = {"schema_version": "1", "task": "tts.synthesize", "input": {"text": "hi"}}


def seed_second_attempt(foundation: Foundation) -> None:
    """A second admitted attempt with two stages that share one logical PCM stream."""
    foundation.database.query(
        "INSERT INTO admission_attempts (operation_id, attempt_id, workspace_id, "
        "binding_revision_id, manifest_id, recipe_digest, policy_revision_id, service_class, "
        "request_digest, required_stage_ids) VALUES ('operation-test', 'attempt-alt', "
        "'workspace-test', 'binding-test', 'manifest-test', %s, 'policy-test', 'realtime', "
        "%s, %s)",
        (DIGEST, DIGEST, '["stage-main","stage-alt"]'),
    )
    for lease_id, stage_id, fencing_token in (
        ("lease-alt-stage-main", "stage-main", 1),
        ("lease-alt-stage-alt", "stage-alt", 2),
    ):
        foundation.database.query(
            "INSERT INTO admission_leases (lease_id, operation_id, attempt_id, stage_id, "
            "host_node_id, instance_id, instance_generation, pool_id, gateway_id, fencing_token, "
            "reserved_slots, deadline_at_us, reserved_unified_physical_memory_bytes) VALUES "
            "(%s, 'operation-test', 'attempt-alt', %s, 'host-test', 'instance-test', 1, "
            "'tts-realtime', 'gateway-test', %s, 1, 1, 1)",
            (lease_id, stage_id, fencing_token),
        )


def identity_for(
    lease_id: str,
    *,
    submit_intent_id: str,
    attempt_id: str = "attempt-alt",
    stage_id: str = "stage-main",
    fencing_token: int = 1,
    sample_rate_hz: int | None = 24000,
    max_output_audio_ms: int | None = 2000,
) -> SubmissionIdentity:
    return SubmissionIdentity(
        submit_intent_id=submit_intent_id,
        lease_id=lease_id,
        operation_id="operation-test",
        attempt_id=attempt_id,
        stage_id=stage_id,
        workspace_id="workspace-test",
        pool_id="tts-realtime",
        host_node_id="host-test",
        gateway_id="gateway-test",
        instance_id="instance-test",
        instance_generation=1,
        binding_revision_id="binding-test",
        policy_revision_id="policy-test",
        voice_revision_id=None,
        fencing_token=fencing_token,
        command_digest=command_digest(COMMAND),
        deadline_at=NOW + timedelta(minutes=5),
        max_output_audio_ms=max_output_audio_ms,
        sample_rate_hz=sample_rate_hz,
    )


def request_for(identity: SubmissionIdentity, *, stream: str | None = "tts-main") -> SubmitRequest:
    return SubmitRequest(identity=identity, command=COMMAND, output_stream_id=stream)


def open_pool(foundation: Foundation) -> GatewayPool:
    if foundation.database.dialect != "postgresql":
        pytest.skip("The gateway submit ledger is a PostgreSQL adapter; InnoDB holds the schema")
    pool = gateway_pool(
        make_conninfo(foundation.database.url, user=foundation.gateway_role, password=PASSWORD),
        schema=foundation.database.schema,
    )
    pool.open()
    return pool


@pytest.fixture
def ledger(foundation: Foundation) -> Iterator[PostgresSubmissionLedger]:
    pool = open_pool(foundation)
    try:
        yield PostgresSubmissionLedger(pool)
    finally:
        pool.close()


def service_for(
    ledger: PostgresSubmissionLedger,
) -> tuple[SubmitExecutionService, ScriptedEngine]:
    engine = ScriptedEngine()
    return (
        SubmitExecutionService(ledger=ledger, dispatch=engine, observation=engine, now=lambda: NOW),
        engine,
    )


def count_intents(foundation: Foundation, lease_id: str) -> int:
    rows = foundation.database.query(
        "SELECT count(*) AS n FROM submit_intents WHERE lease_id = %s", (lease_id,)
    )
    return int(rows[0]["n"])


def await_gateway_lock_wait(foundation: Foundation) -> list[dict[str, Any]]:
    deadline = time.monotonic() + 2
    last: list[dict[str, Any]] = []
    while time.monotonic() < deadline:
        last = foundation.database.query(
            "SELECT pg_blocking_pids(pid) AS blockers, query FROM pg_stat_activity "
            "WHERE usename = %s AND cardinality(pg_blocking_pids(pid)) > 0",
            (foundation.gateway_role,),
        )
        if last:
            return last
        time.sleep(0.01)
    return last


def test_persist_then_dispatch_once_and_replay_after_a_restart(foundation: Foundation) -> None:
    seed_second_attempt(foundation)
    request = request_for(identity_for("lease-alt-stage-main", submit_intent_id="intent-a"))

    first_pool = open_pool(foundation)
    try:
        service, engine = service_for(PostgresSubmissionLedger(first_pool))
        outcome = service.submit(request)
        assert outcome.idempotent_replay is False
        assert engine.dispatch_count == 1
    finally:
        first_pool.close()

    restart_pool = open_pool(foundation)
    try:
        service, engine = service_for(PostgresSubmissionLedger(restart_pool))
        replay = service.submit(request)
        assert replay.idempotent_replay is True
        assert engine.dispatch_count == 0
        assert replay.intent.recorded_at == outcome.intent.recorded_at
        assert replay.intent.execution_state is ExecutionState.EXECUTING
    finally:
        restart_pool.close()

    assert count_intents(foundation, "lease-alt-stage-main") == 1
    row = foundation.database.query(
        "SELECT submit_intent_id, request_digest, request_document, output_stream_id, "
        "sample_rate_hz, hard_output_ceiling_samples, execution_state, cleanup_state, version "
        "FROM submit_intents WHERE lease_id = 'lease-alt-stage-main'"
    )[0]
    assert row["submit_intent_id"] == "intent-a"
    assert row["request_digest"] == request.request_digest
    assert row["request_document"] == request.request_document
    assert row["output_stream_id"] == "tts-main"
    assert row["sample_rate_hz"] == 24000
    assert row["hard_output_ceiling_samples"] == 48000
    assert row["execution_state"] == "executing"
    assert row["cleanup_state"] == "pending"
    assert row["version"] == 2
    heads = foundation.database.query(
        "SELECT output_stream_id, sample_rate_hz FROM output_stream_heads "
        "WHERE operation_id = 'operation-test' AND attempt_id = 'attempt-alt'"
    )
    assert heads == [{"output_stream_id": "tts-main", "sample_rate_hz": 24000}]


def test_the_stored_intent_rebuilds_the_full_identity(
    foundation: Foundation, ledger: PostgresSubmissionLedger
) -> None:
    seed_second_attempt(foundation)
    pool = open_pool(foundation)
    try:
        service, _ = service_for(PostgresSubmissionLedger(pool))
        identity = identity_for("lease-alt-stage-main", submit_intent_id="intent-a")
        service.submit(request_for(identity))
    finally:
        pool.close()

    intent = ledger.get("intent-a")

    assert intent is not None
    assert intent.identity == identity
    assert intent.identity.max_output_audio_ms == 2000
    assert intent.request_digest == request_for(identity).request_digest
    assert intent.output_stream_id == "tts-main"
    assert intent.version == 2
    assert intent.execution_state is ExecutionState.EXECUTING
    assert ledger.get("intent-missing") is None


def test_a_second_intent_for_the_same_lease_is_a_conflict(
    foundation: Foundation, ledger: PostgresSubmissionLedger
) -> None:
    seed_second_attempt(foundation)
    service, engine = service_for(ledger)
    service.submit(request_for(identity_for("lease-alt-stage-main", submit_intent_id="intent-a")))

    with pytest.raises(SubmissionIntentExistsError) as raised:
        service.submit(
            request_for(identity_for("lease-alt-stage-main", submit_intent_id="intent-b"))
        )

    assert raised.value.code == "idempotency_conflict"
    assert raised.value.recovery == "query_original"
    assert engine.dispatch_count == 1
    assert count_intents(foundation, "lease-alt-stage-main") == 1


def test_save_writes_conditions_on_the_version_the_caller_read(
    foundation: Foundation, ledger: PostgresSubmissionLedger
) -> None:
    seed_second_attempt(foundation)
    service, engine = service_for(ledger)
    service.submit(request_for(identity_for("lease-alt-stage-main", submit_intent_id="intent-a")))
    intent = ledger.get("intent-a")
    assert intent is not None
    read_version = intent.version
    intent.mark_stopping(at=NOW + timedelta(seconds=1))

    ledger.save(intent, expected_version=read_version)

    row = foundation.database.query(
        "SELECT execution_state, cleanup_state, version FROM submit_intents "
        "WHERE submit_intent_id = 'intent-a'"
    )[0]
    assert row == {"execution_state": "stopping", "cleanup_state": "pending", "version": 3}

    # A second writer moves the stored row on; the caller's next conditional write must fail.
    reread = ledger.get("intent-a")
    assert reread is not None
    foundation.database.query(
        "UPDATE submit_intents SET version = version + 1 WHERE submit_intent_id = 'intent-a'"
    )
    reread.mark_stopping(at=NOW + timedelta(seconds=2))
    with pytest.raises(StaleSubmissionVersionError) as raised:
        ledger.save(reread, expected_version=reread.version - 1)

    assert raised.value.code == "version_conflict"
    assert engine.dispatch_count == 1


def test_a_recorded_success_is_read_back_with_its_terminal_evidence(
    foundation: Foundation, ledger: PostgresSubmissionLedger
) -> None:
    seed_second_attempt(foundation)
    service, _ = service_for(ledger)
    service.submit(request_for(identity_for("lease-alt-stage-main", submit_intent_id="intent-a")))
    intent = ledger.get("intent-a")
    assert intent is not None
    version = intent.version
    intent.complete_successfully(at=NOW + timedelta(seconds=1))

    ledger.save(intent, expected_version=version)

    stored = ledger.get("intent-a")
    assert stored is not None
    assert stored.execution_state is ExecutionState.SUCCEEDED
    assert stored.stop_evidence_kind is StopEvidenceKind.ENGINE_TERMINAL_RESPONSE
    assert stored.error_code is None


def test_a_verified_stop_is_refused_until_its_own_port_exists(
    foundation: Foundation, ledger: PostgresSubmissionLedger
) -> None:
    seed_second_attempt(foundation)
    service, _ = service_for(ledger)
    service.submit(request_for(identity_for("lease-alt-stage-main", submit_intent_id="intent-a")))
    evidence = VerifiedStopEvidence(
        evidence_id="evidence-1",
        submit_intent_id="intent-a",
        evidence_kind=StopEvidenceKind.ENGINE_ABORT_ACK,
        evidence_digest=DIGEST,
        observed_at_us=1,
        execution_stopped=True,
        resources_released=True,
        reason="integration",
    )

    with pytest.raises(ValueError, match="record_stop"):
        service.record_stop(evidence)

    row = foundation.database.query(
        "SELECT execution_state, version FROM submit_intents WHERE submit_intent_id = 'intent-a'"
    )[0]
    assert row == {"execution_state": "executing", "version": 2}
    evidence_rows = foundation.database.query("SELECT count(*) AS n FROM execution_stop_evidence")
    assert evidence_rows[0]["n"] == 0


def test_a_lease_that_is_not_active_refuses_a_submit(
    foundation: Foundation, ledger: PostgresSubmissionLedger
) -> None:
    seed_second_attempt(foundation)
    with foundation.connection("speech") as connection:
        connection.execute(
            "UPDATE admission_leases SET lease_state = 'quarantined' WHERE lease_id = %s",
            ("lease-alt-stage-main",),
        )

    service, engine = service_for(ledger)
    with pytest.raises(LeaseNotActiveError) as raised:
        service.submit(
            request_for(identity_for("lease-alt-stage-main", submit_intent_id="intent-a"))
        )

    assert raised.value.error_code.value == "lease_not_active"
    assert engine.dispatch_count == 0
    assert count_intents(foundation, "lease-alt-stage-main") == 0


def test_a_sealed_barrier_refuses_a_lower_token_but_not_a_higher_one(
    foundation: Foundation, ledger: PostgresSubmissionLedger
) -> None:
    seed_second_attempt(foundation)
    with foundation.connection("gateway") as connection:
        connection.execute(
            "INSERT INTO gateway_fencing_barriers "
            "(gateway_id, pool_id, min_fencing_token, sealed_evidence_digest) "
            "VALUES ('gateway-test', 'tts-realtime', 2, %s)",
            (DIGEST,),
        )

    service, engine = service_for(ledger)
    with pytest.raises(GatewayBarrierFencedError) as raised:
        service.submit(
            request_for(identity_for("lease-alt-stage-main", submit_intent_id="intent-old"))
        )

    assert raised.value.error_code.value == "gateway_barrier_fenced"
    assert engine.dispatch_count == 0

    accepted = service.submit(
        request_for(
            identity_for(
                "lease-alt-stage-alt",
                submit_intent_id="intent-new",
                stage_id="stage-alt",
                fencing_token=2,
            )
        )
    )
    assert accepted.intent.identity.fencing_token == 2
    assert engine.dispatch_count == 1


def test_an_unknown_lease_is_rejected_without_writing_anything(
    foundation: Foundation, ledger: PostgresSubmissionLedger
) -> None:
    service, engine = service_for(ledger)

    with pytest.raises(InvalidExecutionRequestError, match="unknown lease"):
        service.submit(
            request_for(
                identity_for(
                    "lease-missing", submit_intent_id="intent-unknown", attempt_id="attempt-test"
                )
            )
        )

    assert engine.dispatch_count == 0
    rows = foundation.database.query("SELECT count(*) AS n FROM submit_intents")
    assert rows[0]["n"] == 1  # Only the foundation's synthetic row.


def test_a_stream_rate_conflict_rolls_the_whole_submit_back(
    foundation: Foundation, ledger: PostgresSubmissionLedger
) -> None:
    seed_second_attempt(foundation)
    with foundation.connection("gateway") as connection:
        connection.execute(
            "INSERT INTO output_stream_heads "
            "(operation_id, attempt_id, output_stream_id, sample_rate_hz) "
            "VALUES ('operation-test', 'attempt-alt', 'tts-main', 48000)"
        )

    service, engine = service_for(ledger)
    with pytest.raises(InvalidExecutionRequestError, match="sample rate"):
        service.submit(
            request_for(identity_for("lease-alt-stage-main", submit_intent_id="intent-a"))
        )

    assert engine.dispatch_count == 0
    assert count_intents(foundation, "lease-alt-stage-main") == 0


def test_two_stages_share_one_frozen_stream_head(
    foundation: Foundation, ledger: PostgresSubmissionLedger
) -> None:
    seed_second_attempt(foundation)
    service, engine = service_for(ledger)
    service.submit(request_for(identity_for("lease-alt-stage-main", submit_intent_id="intent-a")))
    service.submit(
        request_for(
            identity_for(
                "lease-alt-stage-alt",
                submit_intent_id="intent-b",
                stage_id="stage-alt",
                fencing_token=2,
            )
        )
    )

    assert engine.dispatch_count == 2
    heads = foundation.database.query(
        "SELECT output_stream_id, sample_rate_hz FROM output_stream_heads "
        "WHERE operation_id = 'operation-test' AND attempt_id = 'attempt-alt'"
    )
    assert heads == [{"output_stream_id": "tts-main", "sample_rate_hz": 24000}]


def test_a_stage_without_audio_creates_no_stream_and_no_ceiling(
    foundation: Foundation, ledger: PostgresSubmissionLedger
) -> None:
    seed_second_attempt(foundation)
    service, engine = service_for(ledger)
    identity = identity_for(
        "lease-alt-stage-main",
        submit_intent_id="intent-asr",
        sample_rate_hz=None,
        max_output_audio_ms=None,
    )

    service.submit(request_for(identity, stream=None))

    assert engine.dispatch_count == 1
    row = foundation.database.query(
        "SELECT output_stream_id, sample_rate_hz, hard_output_ceiling_samples "
        "FROM submit_intents WHERE submit_intent_id = 'intent-asr'"
    )[0]
    assert row == {
        "output_stream_id": None,
        "sample_rate_hz": None,
        "hard_output_ceiling_samples": None,
    }
    heads = foundation.database.query(
        "SELECT count(*) AS n FROM output_stream_heads WHERE attempt_id = 'attempt-alt'"
    )
    assert heads[0]["n"] == 0


def test_a_stored_row_the_gateway_cannot_reinterpret_is_reported(
    foundation: Foundation, ledger: PostgresSubmissionLedger
) -> None:
    seed_second_attempt(foundation)
    service, _ = service_for(ledger)
    service.submit(request_for(identity_for("lease-alt-stage-main", submit_intent_id="intent-a")))
    foundation.database.query(
        "UPDATE submit_intents SET sample_rate_hz = 8000 WHERE submit_intent_id = 'intent-a'"
    )

    with pytest.raises(SubmissionStoreError) as raised:
        ledger.get("intent-a")

    assert raised.value.code == "reconciliation_required"


def test_the_narrow_role_cannot_rewrite_recorded_identity(
    foundation: Foundation, ledger: PostgresSubmissionLedger
) -> None:
    seed_second_attempt(foundation)
    service, _ = service_for(ledger)
    service.submit(request_for(identity_for("lease-alt-stage-main", submit_intent_id="intent-a")))

    statements = (
        "UPDATE submit_intents SET command_digest = 'sha256:" + "b" * 64 + "'",
        "UPDATE submit_intents SET output_stream_id = 'other'",
        "DELETE FROM submit_intents",
        "TRUNCATE submit_intents",
    )
    with foundation.connection("gateway") as connection:
        for statement in statements:
            with pytest.raises(psycopg.Error):
                connection.execute(statement)
            connection.rollback()


def test_the_submit_transaction_waits_for_the_pool_row_lock(foundation: Foundation) -> None:
    seed_second_attempt(foundation)
    pool = open_pool(foundation)
    blocker = psycopg.connect(foundation.database.url, row_factory=dict_row, autocommit=False)
    try:
        blocker.execute(f'SET search_path TO "{foundation.database.schema}", pg_catalog')
        blocker.execute(
            "UPDATE resource_pools SET version = version WHERE pool_id = 'tts-realtime'"
        )
        service, engine = service_for(PostgresSubmissionLedger(pool, timeout_seconds=15))
        with ThreadPoolExecutor(max_workers=1) as workers:
            submitted = workers.submit(
                service.submit,
                request_for(identity_for("lease-alt-stage-main", submit_intent_id="intent-a")),
            )
            waiting = await_gateway_lock_wait(foundation)
            assert waiting, "the submit transaction never waited on the pool row"
            assert "resource_pools" in waiting[0]["query"]
            blocker.rollback()
            outcome = submitted.result(timeout=20)
        assert outcome.idempotent_replay is False
        assert engine.dispatch_count == 1
    finally:
        blocker.close()
        pool.close()
