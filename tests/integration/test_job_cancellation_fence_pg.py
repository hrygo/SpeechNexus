"""Real PostgreSQL proof for the cancellation fence and submit adjudication.

The fence, the gateway submit transaction and the reconciliation schedule run as
separate non-owner roles. No test skips into an unqualified pass: an absent
isolated DSN is an explicit skip, and every assertion below exercises real rows,
real advisory locks and real commit boundaries.
"""

from __future__ import annotations

import asyncio
import os
import threading
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row
from sqlalchemy import create_engine
from sqlalchemy.pool import NullPool
from tests.integration.test_coordination_foundation import seed
from tests.integration.test_gateway_submit_ledger import seed_second_attempt

from speechnexus_backend.speech.application.job_cancellation_reconciliation import (
    Confirmation,
    JobCancellationReconciler,
)
from speechnexus_backend.speech.infrastructure.coordination_grants import (
    coordination_runtime_grants,
)
from speechnexus_backend.speech.infrastructure.job_cancellation_fence import (
    CancellationFence,
    write_cancellation_fence,
)
from speechnexus_backend.speech.infrastructure.job_cancellation_fence_schema import (
    cancellation_reconciliation_grants,
    gateway_cancellation_fence_grants,
    job_cancellation_fence_grants,
)
from speechnexus_backend.speech.infrastructure.job_cancellation_reconciliation_postgres import (
    PostgresCancellationReconciliationStore,
)
from speechnexus_backend.speech.infrastructure.job_schema import (
    job_dispatch_grants,
    job_runtime_grants,
)
from speechnexus_backend.speech.infrastructure.postgres_pool import speech_pool
from speechnexus_backend.speech.infrastructure.public_job_schema import public_job_grants
from speechnexus_backend.speech.infrastructure.public_job_submission_schema import (
    public_job_submission_grants,
)
from speechnexus_gateway.domain.canonical import JsonValue, command_digest
from speechnexus_gateway.domain.submission import (
    ExecutionErrorCode,
    SubmissionIdentity,
    SubmissionIntent,
    SubmissionIntentExistsError,
    SubmitRequest,
)
from speechnexus_gateway.infrastructure.postgres_pool import gateway_pool
from speechnexus_gateway.infrastructure.submit_postgres import PostgresSubmissionLedger

pytestmark = pytest.mark.integration
ROOT = Path(__file__).resolve().parents[2]
VARIABLE = "SPEECHNEXUS_TEST_DATABASE_URL"
PASSWORD = "synthetic_fence_test"
DIGEST = "sha256:" + "a" * 64
DEADLINE = datetime(2099, 1, 1, tzinfo=UTC)
COMMAND: JsonValue = {"schema_version": "1", "task": "tts.synthesize", "input": {"text": "hi"}}


@dataclass
class FenceSandbox:
    url: str = field(repr=False)
    schema: str
    speech_role: str
    gateway_role: str
    admin: psycopg.Connection[dict[str, Any]] = field(repr=False)
    speech: Any = field(repr=False)
    gateway: Any = field(repr=False)

    def query(self, statement: str, parameters: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        with self.admin.cursor() as cursor:
            cursor.execute(statement, parameters or None)
            return list(cursor.fetchall()) if cursor.description else []

    def speech_connection(self) -> psycopg.Connection[dict[str, Any]]:
        connection = psycopg.connect(
            make_conninfo(self.url, user=self.speech_role, password=PASSWORD),
            row_factory=dict_row,
        )
        connection.execute(f'SET search_path TO "{self.schema}", pg_catalog')
        return connection

    def seed_job(self, job_id: str, *, state: str = "running") -> None:
        self.query(
            "INSERT INTO speech_jobs (job_id, workspace_id, kind, model_id, request_digest, "
            "state, status_version, created_at_us, updated_at_us) VALUES "
            "(%s, 'workspace-test', 'tts', 'tts-quality', %s, %s, 2, 1, 1)",
            (job_id, DIGEST, state),
        )

    def seed_lease(self, job_id: str, attempt_id: str, lease_id: str) -> None:
        self.query(
            "INSERT INTO admission_attempts (operation_id, attempt_id, workspace_id, "
            "binding_revision_id, manifest_id, recipe_digest, policy_revision_id, service_class, "
            "request_digest, required_stage_ids) VALUES (%s, %s, 'workspace-test', "
            "'binding-test', 'manifest-test', %s, 'policy-test', 'realtime', %s, %s)",
            (job_id, attempt_id, DIGEST, DIGEST, '["stage-main"]'),
        )
        self.query(
            "INSERT INTO admission_leases (lease_id, operation_id, attempt_id, stage_id, "
            "host_node_id, instance_id, instance_generation, pool_id, gateway_id, fencing_token, "
            "reserved_slots, deadline_at_us, reserved_unified_physical_memory_bytes) VALUES "
            "(%s, %s, %s, 'stage-main', 'host-test', 'instance-test', 1, 'tts-realtime', "
            "'gateway-test', 1, 1, %s, 1)",
            (lease_id, job_id, attempt_id, int(DEADLINE.timestamp() * 1_000_000)),
        )


class _QueryDatabase:
    def __init__(self, admin: psycopg.Connection[dict[str, Any]]) -> None:
        self.admin = admin

    def query(self, statement: str, parameters: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        with self.admin.cursor() as cursor:
            cursor.execute(statement, parameters or None)
            return list(cursor.fetchall()) if cursor.description else []


@pytest.fixture
def fence_db() -> Iterator[FenceSandbox]:
    url = os.environ.get(VARIABLE)
    if not url:
        pytest.fail(
            f"Configure an isolated PostgreSQL DSN in {VARIABLE}; skip is not fence evidence"
        )
    suffix = uuid.uuid4().hex[:12]
    schema = "sn_fence_" + suffix
    speech, gateway = schema + "_speech", schema + "_gateway"
    engine = create_engine(
        url.replace("postgresql://", "postgresql+psycopg://", 1), poolclass=NullPool
    )
    config = Config(str(ROOT / "alembic.ini"))
    config.attributes["schema"] = schema
    try:
        with engine.connect() as connection:
            config.attributes["connection"] = connection
            command.upgrade(config, "head")
    finally:
        engine.dispose()
    admin = psycopg.connect(url, autocommit=True, row_factory=dict_row)
    admin.execute(f'SET search_path TO "{schema}", pg_catalog')
    speech_pool_handle = gateway_pool_handle = None
    try:
        for role in (speech, gateway):
            admin.execute(f"CREATE ROLE \"{role}\" LOGIN PASSWORD '{PASSWORD}'")
        grants = (
            *coordination_runtime_grants(schema, speech_role=speech, gateway_role=gateway),
            *job_runtime_grants(schema, speech),
            *job_dispatch_grants(schema, speech),
            *public_job_grants(schema, speech),
            *public_job_submission_grants(schema, speech),
            *job_cancellation_fence_grants(schema, speech),
            *cancellation_reconciliation_grants(schema, speech),
            *gateway_cancellation_fence_grants(schema, gateway),
        )
        for grant in grants:
            admin.execute(grant)
        seed(cast(Any, _QueryDatabase(admin)))
        seed_second_attempt(cast(Any, SimpleNamespace(database=_QueryDatabase(admin))))
        admin.execute(
            "INSERT INTO speech_jobs (job_id, workspace_id, kind, model_id, request_digest, "
            "state, status_version, created_at_us, updated_at_us) VALUES "
            "('operation-test', 'workspace-test', 'tts', 'tts-quality', %s, 'running', 2, 1, 1)",
            (DIGEST,),
        )
        speech_pool_handle = speech_pool(
            make_conninfo(url, user=speech, password=PASSWORD), schema=schema
        )
        speech_pool_handle.open()
        gateway_pool_handle = gateway_pool(
            make_conninfo(url, user=gateway, password=PASSWORD), schema=schema
        )
        gateway_pool_handle.open()
        yield FenceSandbox(
            url, schema, speech, gateway, admin, speech_pool_handle, gateway_pool_handle
        )
    finally:
        if speech_pool_handle is not None:
            speech_pool_handle.close()
        if gateway_pool_handle is not None:
            gateway_pool_handle.close()
        admin.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        for role in reversed((speech, gateway)):
            admin.execute(f'DROP OWNED BY "{role}"')
            admin.execute(f'DROP ROLE IF EXISTS "{role}"')
        admin.close()


def identity(lease_id: str, attempt_id: str, job_id: str) -> SubmissionIdentity:
    return SubmissionIdentity(
        submit_intent_id="submit-" + uuid.uuid4().hex,
        lease_id=lease_id,
        operation_id=job_id,
        attempt_id=attempt_id,
        stage_id="stage-main",
        workspace_id="workspace-test",
        pool_id="tts-realtime",
        host_node_id="host-test",
        gateway_id="gateway-test",
        instance_id="instance-test",
        instance_generation=1,
        binding_revision_id="binding-test",
        policy_revision_id="policy-test",
        voice_revision_id=None,
        fencing_token=1,
        command_digest=command_digest(COMMAND),
        deadline_at=DEADLINE,
        max_output_audio_ms=2000,
        sample_rate_hz=24000,
    )


def submit_request(intent: SubmissionIdentity) -> SubmitRequest:
    return SubmitRequest(identity=intent, command=COMMAND, output_stream_id="tts-main")


def submission_intent(intent: SubmissionIdentity) -> SubmissionIntent:
    request = submit_request(intent)
    now = datetime.now(UTC)
    return SubmissionIntent(
        identity=intent,
        recorded_at=now,
        updated_at=now,
        request_digest=request.request_digest,
        output_stream_id=request.output_stream_id,
    )


def _accept(
    db: FenceSandbox,
    job_id: str,
    *,
    request_id: str,
    accepted_at_us: int | None = None,
) -> CancellationFence:
    """Test-only acceptance: write the fence in one explicit transaction."""
    with db.speech.connection() as connection:
        if accepted_at_us is None:
            row = connection.execute(
                "SELECT CAST(EXTRACT(EPOCH FROM clock_timestamp()) * 1000000 AS BIGINT) AS now_us"
            ).fetchone()
            assert row is not None
            accepted_at_us = int(row["now_us"])
        with connection.cursor() as cursor:
            return write_cancellation_fence(
                cursor,
                job_id=job_id,
                request_id=request_id,
                accepted_at_us=accepted_at_us,
            )


def test_fence_committed_before_submit_refuses_creation(fence_db: FenceSandbox) -> None:
    accepted = _accept(fence_db, "operation-test", request_id="request-fence-1")
    ledger = PostgresSubmissionLedger(fence_db.gateway)
    intent = submission_intent(identity("lease-alt-stage-main", "attempt-alt", "operation-test"))

    refused = ledger.add(intent, request_document=submit_request(intent.identity).request_document)

    assert refused is not None
    assert refused.refused_before_execution
    assert refused.error_code is ExecutionErrorCode.GATEWAY_BARRIER_FENCED
    assert refused.dispatched_at is None and refused.engine_request_id is None
    stored = ledger.get(intent.identity.submit_intent_id)
    assert stored is not None and stored.refused_before_execution
    assert fence_db.query(
        "SELECT state, error_code FROM speech_jobs WHERE job_id = 'operation-test'"
    ) == [{"state": "running", "error_code": None}]
    assert fence_db.query(
        "SELECT count(*) AS n FROM submit_intents WHERE operation_id = 'operation-test' "
        "AND execution_state = 'failed' AND error_code = 'gateway_barrier_fenced'"
    ) == [{"n": 1}]
    assert accepted.request_id == "request-fence-1"


def test_fence_accept_is_first_wins_and_replayable(fence_db: FenceSandbox) -> None:
    first = _accept(fence_db, "operation-test", request_id="request-first")
    same = _accept(fence_db, "operation-test", request_id="request-first")
    other = _accept(fence_db, "operation-test", request_id="request-other")

    assert (same.accepted_at_us, same.request_id) == (first.accepted_at_us, first.request_id)
    assert (other.accepted_at_us, other.request_id) == (first.accepted_at_us, first.request_id)
    assert fence_db.query(
        "SELECT count(*) AS n FROM speech_job_cancellation_fences WHERE job_id = 'operation-test'"
    ) == [{"n": 1}]


def test_store_exhaustion_prevents_further_claims(fence_db: FenceSandbox) -> None:
    fence_db.seed_job("job-reconcile-3")
    accepted = _accept(fence_db, "job-reconcile-3", request_id="request-reconcile-3")
    store = PostgresCancellationReconciliationStore(fence_db.speech)
    now = accepted.accepted_at_us + 1
    assert (
        len(
            store.claim_due(
                now_us=now, limit=8, lease_until_us=now + 1_000_000, accepted_before_us=now
            )
        )
        == 1
    )
    store.record_exhausted("job-reconcile-3", now_us=now + 2_000_000, outcome="exhausted")

    assert (
        store.claim_due(
            now_us=now + 60_000_000,
            limit=8,
            lease_until_us=now + 61_000_000,
            accepted_before_us=now + 60_000_000,
        )
        == []
    )
    row = store.read("job-reconcile-3")
    assert row is not None and row["last_outcome"] == "exhausted"
    assert row["settled_at_us"] is None


def test_execution_created_before_acceptance_is_not_rewritten(fence_db: FenceSandbox) -> None:
    ledger = PostgresSubmissionLedger(fence_db.gateway)
    intent = submission_intent(identity("lease-alt-stage-main", "attempt-alt", "operation-test"))
    assert (
        ledger.add(intent, request_document=submit_request(intent.identity).request_document)
        is None
    )
    before = ledger.get(intent.identity.submit_intent_id)
    assert before is not None and before.execution_state.value == "submitting"

    accepted = _accept(fence_db, "operation-test", request_id="request-fence-2")

    after = ledger.get(intent.identity.submit_intent_id)
    assert after == before
    assert ledgers_unchanged(fence_db, before)

    with pytest.raises(SubmissionIntentExistsError):
        ledger.add(intent, request_document=submit_request(intent.identity).request_document)
    assert ledger.get(intent.identity.submit_intent_id) == before
    assert accepted.accepted_at_us > 0


def ledgers_unchanged(fence_db: FenceSandbox, before: Any) -> bool:
    row = fence_db.query(
        "SELECT state, status_version FROM speech_jobs WHERE job_id = 'operation-test'"
    )[0]
    return row == {"state": "running", "status_version": 2} and before.execution_state.value in (
        "submitting",
        "executing",
    )


def test_fence_joins_the_acceptance_transaction(fence_db: FenceSandbox) -> None:
    fence_db.seed_job("job-rollback-1")
    connection = fence_db.speech_connection()
    connection.commit()
    try:
        with connection.transaction():
            connection.execute(
                "INSERT INTO speech_job_events (job_id, event_seq, state, occurred_at_us) "
                "VALUES ('operation-test', 1, 'running', 1)"
            )
            with connection.cursor() as cursor:
                write_cancellation_fence(
                    cursor,
                    job_id="operation-test",
                    request_id="request-atomic-1",
                    accepted_at_us=1_000_000,
                )
        assert fence_db.query(
            "SELECT count(*) AS n FROM speech_job_events WHERE job_id = 'operation-test'"
        ) == [{"n": 1}]
        assert fence_db.query(
            "SELECT count(*) AS n FROM speech_job_cancellation_fences "
            "WHERE job_id = 'operation-test'"
        ) == [{"n": 1}]

        with pytest.raises(RuntimeError), connection.transaction():
            connection.execute(
                "INSERT INTO speech_job_events (job_id, event_seq, state, occurred_at_us) "
                "VALUES ('job-rollback-1', 1, 'running', 1)"
            )
            with connection.cursor() as cursor:
                write_cancellation_fence(
                    cursor,
                    job_id="job-rollback-1",
                    request_id="request-atomic-2",
                    accepted_at_us=2_000_000,
                )
            raise RuntimeError("simulated acceptance failure")
        assert fence_db.query(
            "SELECT count(*) AS n FROM speech_job_events WHERE job_id = 'job-rollback-1'"
        ) == [{"n": 0}]
        assert fence_db.query(
            "SELECT count(*) AS n FROM speech_job_cancellation_fences "
            "WHERE job_id = 'job-rollback-1'"
        ) == [{"n": 0}]
    finally:
        connection.close()


def test_acceptance_lock_waits_for_submit_and_then_refuses(fence_db: FenceSandbox) -> None:
    fence_db.seed_job("job-lock-1")
    fence_db.seed_lease("job-lock-1", "attempt-lock-1", "lease-lock-1")
    intent = submission_intent(identity("lease-lock-1", "attempt-lock-1", "job-lock-1"))
    request = submit_request(intent.identity)
    ledger = PostgresSubmissionLedger(fence_db.gateway)
    outcome: dict[str, Any] = {}

    def submit() -> None:
        outcome["result"] = ledger.add(intent, request_document=request.request_document)

    holder = fence_db.speech_connection()
    holder.commit()
    try:
        with holder.cursor() as cursor:
            accepted = write_cancellation_fence(
                cursor,
                job_id="job-lock-1",
                request_id="request-lock-1",
                accepted_at_us=int(time.time() * 1_000_000),
            )
        # This transaction owns the lock but is not committed yet.
        worker = threading.Thread(target=submit)
        worker.start()
        deadline = time.monotonic() + 5
        waiting = 0
        while time.monotonic() < deadline:
            waiting = fence_db.query(
                "SELECT count(*) AS n FROM pg_stat_activity "
                "WHERE usename = %s AND wait_event_type = 'Lock'",
                (fence_db.gateway_role,),
            )[0]["n"]
            if waiting:
                break
            time.sleep(0.01)
        assert waiting >= 1, "the gateway submit did not block on the fence lock"
        holder.commit()
        worker.join(timeout=5)
        assert not worker.is_alive()
    finally:
        holder.close()

    refused = outcome["result"]
    assert refused is not None and refused.refused_before_execution
    assert accepted.job_id == "job-lock-1"
    assert fence_db.query(
        "SELECT count(*) AS n FROM submit_intents WHERE operation_id = 'job-lock-1' "
        "AND execution_state = 'executing'"
    ) == [{"n": 0}]


def test_migration_down_refuses_to_drop_nonempty_fences(fence_db: FenceSandbox) -> None:
    fence_db.seed_job("job-migration-1")
    accepted = _accept(fence_db, "job-migration-1", request_id="request-migration-1")
    fence_db.query(
        "INSERT INTO speech_job_cancellation_not_submitted "
        "(job_id, observed_at_us, observer) VALUES (%s, %s, 'worker-a')",
        ("job-migration-1", accepted.accepted_at_us),
    )

    def downgrade() -> None:
        engine = create_engine(
            fence_db.url.replace("postgresql://", "postgresql+psycopg://", 1),
            poolclass=NullPool,
        )
        config = Config(str(ROOT / "alembic.ini"))
        config.attributes["schema"] = fence_db.schema
        try:
            with engine.connect() as connection:
                config.attributes["connection"] = connection
                command.downgrade(config, "0033_profile_publications")
        finally:
            engine.dispose()

    with pytest.raises(RuntimeError, match="not-submitted"):
        downgrade()
    fence_db.query("DELETE FROM speech_job_cancellation_not_submitted")
    with pytest.raises(RuntimeError, match="preservation"):
        downgrade()
    assert fence_db.query("SELECT count(*) AS n FROM speech_job_cancellation_fences") == [{"n": 1}]
    assert fence_db.query("SELECT count(*) AS n FROM speech_job_cancellation_reconciliations") == [
        {"n": 0}
    ]

    fence_db.query("DELETE FROM speech_job_cancellation_reconciliations")
    fence_db.query("DELETE FROM speech_job_cancellation_fences")
    downgrade()
    assert fence_db.query("SELECT to_regclass('speech_job_cancellation_fences') AS name") == [
        {"name": None}
    ]


def test_reconciliation_store_lease_retry_settle_and_restart(fence_db: FenceSandbox) -> None:
    fence_db.seed_job("job-reconcile-1")
    accepted = _accept(fence_db, "job-reconcile-1", request_id="request-reconcile-1")
    store = PostgresCancellationReconciliationStore(fence_db.speech)
    now = accepted.accepted_at_us + 1

    first = store.claim_due(
        now_us=now, limit=8, lease_until_us=now + 10_000_000, accepted_before_us=now
    )
    assert [candidate.job_id for candidate in first] == ["job-reconcile-1"]
    assert first[0].attempts == 1
    assert (
        store.claim_due(
            now_us=now, limit=8, lease_until_us=now + 10_000_000, accepted_before_us=now
        )
        == []
    )

    store.record_retry(
        "job-reconcile-1",
        now_us=now + 1_000_000,
        outcome="pending",
        next_check_at_us=now + 2_000_000,
    )
    restarted = PostgresCancellationReconciliationStore(fence_db.speech)
    second = restarted.claim_due(
        now_us=now + 2_000_000,
        limit=8,
        lease_until_us=now + 3_000_000,
        accepted_before_us=now + 2_000_000,
    )
    assert [candidate.job_id for candidate in second] == ["job-reconcile-1"]
    assert second[0].attempts == 2

    restarted.record_settled("job-reconcile-1", now_us=now + 2_000_000)
    assert (
        restarted.claim_due(
            now_us=now + 60_000_000,
            limit=8,
            lease_until_us=now + 61_000_000,
            accepted_before_us=now + 60_000_000,
        )
        == []
    )
    row = restarted.read("job-reconcile-1")
    assert row is not None and row["last_outcome"] == "settled"
    assert row["settled_at_us"] == now + 2_000_000


class _SettledAfterRestart:
    def __init__(self) -> None:
        self.calls = 0

    async def confirm(self, job_id: str, *, not_submitted: bool = False) -> Confirmation:
        self.calls += 1
        return Confirmation("settled" if self.calls >= 2 else "pending")


def test_worker_restart_converges_and_does_not_repeat_settlement(fence_db: FenceSandbox) -> None:
    fence_db.seed_job("job-reconcile-2")
    accepted = _accept(fence_db, "job-reconcile-2", request_id="request-reconcile-2")
    store = PostgresCancellationReconciliationStore(fence_db.speech)
    now = accepted.accepted_at_us + 1
    confirmer = _SettledAfterRestart()

    first = JobCancellationReconciler(
        store=store,
        confirmer=confirmer,
        max_attempts=3,
        batch_size=8,
        base_backoff_seconds=1,
        max_backoff_seconds=4,
        budget_seconds=2,
        min_age_seconds=0,
    )
    assert asyncio.run(first.run_once(now_us=now)).retried == 1
    assert asyncio.run(first.run_once(now_us=now + 500_000)).claimed == 0

    restarted = JobCancellationReconciler(
        store=PostgresCancellationReconciliationStore(fence_db.speech),
        confirmer=confirmer,
        max_attempts=3,
        batch_size=8,
        base_backoff_seconds=1,
        max_backoff_seconds=4,
        budget_seconds=2,
        min_age_seconds=0,
    )
    second = asyncio.run(restarted.run_once(now_us=now + 1_000_000))
    third = asyncio.run(restarted.run_once(now_us=now + 60_000_000))

    assert (second.claimed, second.settled) == (1, 1)
    assert third.claimed == 0
    row = store.read("job-reconcile-2")
    assert row is not None and row["last_outcome"] == "settled"
    assert confirmer.calls == 2
