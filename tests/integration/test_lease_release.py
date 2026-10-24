"""Identical PG/InnoDB settlement, real locks, native B facts and retained history."""

import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any, cast

import psycopg
import pytest
from tests.integration.test_coordination_foundation import foundation as foundation
from tests.integration.test_gateway_observation import observed_request
from tests.integration.test_gateway_recovery import Ledger, pending, unknown_request
from tests.integration.test_gateway_recovery import recovery_ledger as recovery_ledger
from tests.integration.test_gateway_stop_evidence import evidence_for
from tests.integration.test_gateway_stop_evidence import stop_ledger as stop_ledger
from tests.integration.test_gateway_submit_ledger import identity_for, request_for
from tests.integration.test_production_jobs import JobDatabase
from tests.integration.test_production_jobs import database as database
from tests.integration.test_production_leases import (
    LEASE_ID,
    LeaseDatabase,
    _second_stage,
    _unknown,
)
from tests.integration.test_production_leases import lease_database as lease_database

from speechnexus_backend.speech.application.leases import LeaseService
from speechnexus_backend.speech.domain.lease_release import ReleaseLease
from speechnexus_backend.speech.domain.leases import LeaseRejectedError
from speechnexus_backend.speech.domain.owners import MAX_VERSION
from speechnexus_backend.speech.infrastructure import lease_release_sql
from speechnexus_backend.speech.infrastructure.coordination_grants import DIMENSIONS
from speechnexus_backend.speech.infrastructure.leases_postgres import PostgresLeaseTransactions
from speechnexus_backend.speech.infrastructure.leases_sql import _update as update_projection
from speechnexus_backend.speech.infrastructure.postgres_pool import SpeechPool
from speechnexus_backend.speech.interfaces.lease_transactions import LeaseTransactionBoundary
from speechnexus_gateway.domain.submission import (
    ExecutionState,
    LeaseNotActiveError,
    RecordStop,
    StopEvidenceKind,
    SubmissionIntent,
)

pytestmark = pytest.mark.integration


def command(lease: LeaseDatabase, lease_id: str = LEASE_ID) -> ReleaseLease:
    fact = lease.port.query_lease(lease_id).fact
    assert fact is not None
    return ReleaseLease(lease_id, fact.version)


def stopped(
    lease: LeaseDatabase, *, kind: str = "engine_terminal_response", state: str = "succeeded"
) -> None:
    # Synthetic storage fixtures only. Native B stop/no-dispatch cases below use
    # its own transaction ports and verified synthetic evidence constructors.
    db, now = lease.foundation.database, lease.now()
    db.query(
        "UPDATE submit_intents SET execution_state = %s, cleanup_state = 'complete', "
        "terminated_at_us = %s, updated_at_us = %s, error_code = %s",
        (state, now, now, "engine_transport_unknown" if state == "failed" else None),
    )
    for row in db.query("SELECT submit_intent_id FROM submit_intents"):
        submit_id = row["submit_intent_id"]
        db.query(
            "INSERT INTO execution_stop_evidence (evidence_id, submit_intent_id, evidence_kind, "
            "evidence_digest, observed_at_us, execution_stopped, resources_released) "
            "VALUES (%s, %s, %s, %s, %s, 1, 1)",
            ("stop-" + submit_id, submit_id, kind, "sha256:" + "e" * 64, now),
        )


def test_stop_release_and_old_version_replay_preserve_gateway_facts(
    lease_database: LeaseDatabase,
) -> None:
    lease, db = lease_database, lease_database.foundation.database
    stopped(lease)
    original = command(lease)
    gateway = {
        table: db.query(f"SELECT * FROM {table}")
        for table in (
            "submit_intents",
            "execution_stop_evidence",
            "output_stream_heads",
            "output_sample_intervals",
        )
    }
    boundary = LeaseTransactionBoundary(LeaseService(lease.port))
    response = boundary.release_lease({"lease_id": LEASE_ID, "expected_version": "1"})
    assert response["outcome"] == "committed"
    assert response["value"] == {
        "kind": "lease",
        "lease_id": LEASE_ID,
        "lease_state": "released",
        "cleanup_state": "complete",
        "version": "2",
    }
    assert db.query(
        "SELECT held_unified_physical_memory_bytes AS held, "
        "quarantined_unified_physical_memory_bytes AS q FROM host_nodes"
    ) == [{"held": 0, "q": 0}]
    assert db.query("SELECT held_attempts, quarantined_attempts FROM workspace_quotas") == [
        {"held_attempts": 0, "quarantined_attempts": 0}
    ]
    snapshot = lease.snapshot()
    for request in (original, replace(original, expected_version=MAX_VERSION)):
        assert lease.port.release_lease(request).outcome == "replayed"
    assert lease.snapshot() == snapshot
    assert {table: db.query(f"SELECT * FROM {table}") for table in gateway} == gateway
    assert (
        boundary.query_outcome({"target": {"kind": "lease", "lease_id": LEASE_ID}})["value"]
        == response["value"]
    )


@pytest.mark.parametrize(
    "state,cleanup",
    [
        ("submitting", "pending"),
        ("executing", "pending"),
        ("stopping", "pending"),
        ("unknown_quarantined", "quarantined"),
        ("succeeded", "complete"),
        ("aborted", "complete"),
    ],
)
def test_expired_lease_and_unverified_terminal_never_free_resources(
    lease_database: LeaseDatabase, state: str, cleanup: str
) -> None:
    lease, db = lease_database, lease_database.foundation.database
    db.query("UPDATE submit_intents SET execution_state = %s, cleanup_state = %s", (state, cleanup))
    snapshot = lease.snapshot()
    assert lease.port.release_lease(command(lease)).error_code == "execution_cleanup_pending"
    assert lease.snapshot() == snapshot


def test_no_submit_release_blocks_later_native_gateway_submission(
    lease_database: LeaseDatabase, recovery_ledger: Ledger
) -> None:
    lease, db = lease_database, lease_database.foundation.database
    # Remove the unused synthetic seed before behavior starts; product roles
    # cannot delete execution history. This models a lease never submitted.
    db.query("DELETE FROM submit_intents")
    assert lease.port.release_lease(command(lease)).outcome == "committed"
    request = request_for(
        identity_for(LEASE_ID, submit_intent_id="intent-late", attempt_id="attempt-test")
    )
    now = datetime.now(UTC)
    intent = SubmissionIntent(
        identity=request.identity,
        recorded_at=now,
        updated_at=now,
        request_digest=request.request_digest,
        output_stream_id=request.output_stream_id,
    )
    with pytest.raises(LeaseNotActiveError):
        recovery_ledger.add(intent, request_document=request.request_document)
    assert db.query("SELECT count(*) AS n FROM submit_intents") == [{"n": 0}]


def test_two_reconcilers_release_once_after_observed_pool_wait(
    lease_database: LeaseDatabase,
) -> None:
    lease, db = lease_database, lease_database.foundation.database
    stopped(lease)
    request = command(lease)
    blocker = db.blocker()
    try:
        with blocker.cursor() as cursor:
            cursor.execute("SELECT pool_id FROM resource_pools FOR UPDATE")
            cursor.fetchall()
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(lease.port.release_lease, request) for _ in range(2)]
            try:
                lease.wait_for_lock(2)
            finally:
                blocker.commit()
            responses = [future.result(timeout=5) for future in futures]
        assert sorted(item.outcome for item in responses) == ["committed", "replayed"]
        assert responses[0].fact == responses[1].fact
        assert db.query("SELECT held_attempts, version FROM workspace_quotas") == [
            {"held_attempts": 0, "version": 2}
        ]
    finally:
        blocker.rollback()
        blocker.close()


@pytest.mark.parametrize("isolated", [False, True])
@pytest.mark.parametrize("cross_pool", [False, True])
def test_concurrent_last_stages_return_attempt_quota_exactly_once(
    lease_database: LeaseDatabase,
    isolated: bool,
    cross_pool: bool,
) -> None:
    lease, db = lease_database, lease_database.foundation.database
    _unknown(lease)
    _second_stage(lease)
    if cross_pool:
        db.query(
            "INSERT INTO resource_pools (pool_id, service_class, pool_state) "
            "VALUES ('asr-realtime', 'realtime', 'active')"
        )
        db.query(
            "INSERT INTO engine_instances (instance_id, instance_generation, host_node_id, "
            "pool_id, gateway_id, binding_revision_id, manifest_id, approved_template_id, "
            "management_mode, loading_policy, config_digest, instance_state, "
            "max_slots, held_slots) SELECT 'instance-other', instance_generation, host_node_id, "
            "'asr-realtime', gateway_id, binding_revision_id, manifest_id, approved_template_id, "
            "management_mode, loading_policy, config_digest, instance_state, max_slots, 1 "
            "FROM engine_instances WHERE instance_id = 'instance-test'"
        )
        db.query("UPDATE engine_instances SET held_slots=1 WHERE instance_id='instance-test'")
        db.query(
            "UPDATE admission_leases SET instance_id='instance-other', pool_id='asr-realtime' "
            "WHERE lease_id='lease-other'"
        )
    if isolated:
        db.query("UPDATE host_nodes SET quarantine_threshold_ppm = 1")
        for key in (LEASE_ID, "lease-other"):
            assert lease.port.quarantine_lease(lease.request(key)).outcome == "committed"
    stopped(
        lease,
        kind="instance_terminated_by_controller" if isolated else "engine_terminal_response",
        state="aborted" if isolated else "succeeded",
    )
    quota_version = db.query("SELECT version FROM workspace_quotas")[0]["version"]
    requests = [command(lease, key) for key in (LEASE_ID, "lease-other")]
    blocker = db.blocker()
    try:
        with blocker.cursor() as cursor:
            cursor.execute("SELECT workspace_id FROM workspace_quotas FOR UPDATE")
            cursor.fetchall()
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(lease.port.release_lease, request) for request in requests]
            try:
                lease.wait_for_lock(2)
            finally:
                blocker.commit()
            assert all(future.result(timeout=5).outcome == "committed" for future in futures)
    finally:
        blocker.rollback()
        blocker.close()
    assert db.query(
        "SELECT held_attempts, quarantined_attempts, version FROM workspace_quotas"
    ) == [{"held_attempts": 0, "quarantined_attempts": 0, "version": quota_version + 1}]
    assert db.query("SELECT quota_state FROM admission_attempts") == [{"quota_state": "released"}]
    assert all(
        row["held_slots"] == row["quarantined_slots"] == 0
        for row in db.query("SELECT held_slots, quarantined_slots FROM engine_instances")
    )
    if isolated:
        assert db.query("SELECT node_state FROM host_nodes") == [{"node_state": "quarantined"}]


@pytest.mark.parametrize("drift", ["host", "instance", "quota", "version", "stages", "future_stop"])
def test_inconsistent_or_incomplete_facts_roll_back_every_projection(
    lease_database: LeaseDatabase, drift: str
) -> None:
    lease, db = lease_database, lease_database.foundation.database
    stopped(lease)
    if drift == "host":
        db.query("UPDATE host_nodes SET held_unified_physical_memory_bytes = 0")
    elif drift == "instance":
        db.query("UPDATE engine_instances SET held_slots = 0")
    elif drift == "quota":
        db.query("UPDATE workspace_quotas SET held_attempts = 0")
    elif drift == "version":
        db.query("UPDATE host_nodes SET version = %s", (MAX_VERSION,))
    elif drift == "stages":
        db.query(
            "UPDATE admission_attempts SET required_stage_ids = %s",
            ('["stage-main","stage-missing"]',),
        )
    else:
        db.query(
            "UPDATE execution_stop_evidence SET observed_at_us = %s", (lease.now() + 10000000,)
        )
    request, snapshot = command(lease), lease.snapshot()
    response = lease.port.release_lease(request)
    assert response.outcome == "rejected"
    assert response.error_code in ("reconciliation_required", "execution_cleanup_pending")
    assert lease.snapshot() == snapshot


def test_mid_settlement_failure_restores_quota_and_host(
    lease_database: LeaseDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    lease = lease_database
    stopped(lease)
    request, snapshot = command(lease), lease.snapshot()
    update = update_projection

    def fail_after_host(cursor: Any, query: str, parameters: tuple[object, ...]) -> None:
        update(cursor, query, parameters)
        if query.startswith("UPDATE host_nodes"):
            raise LeaseRejectedError("reconciliation_required")

    monkeypatch.setattr(lease_release_sql, "_update", fail_after_host)
    assert lease.port.release_lease(request).outcome == "rejected"
    assert lease.snapshot() == snapshot


@pytest.mark.parametrize("unsent", [False, True])
def test_native_gateway_stop_or_no_dispatch_fact_then_release(
    lease_database: LeaseDatabase, recovery_ledger: Ledger, unsent: bool
) -> None:
    lease, db = lease_database, lease_database.foundation.database
    original = pending(lease.foundation, recovery_ledger)
    db.query("UPDATE workspace_quotas SET held_attempts = 2")
    db.query("UPDATE engine_instances SET max_slots = 3, held_slots = 3")
    db.query(
        "UPDATE host_nodes SET held_unified_physical_memory_bytes = 3, quarantine_threshold_ppm = 1"
    )
    target = "lease-alt-stage-main"
    if unsent:
        recovery_ledger.record_execution_observation(observed_request(original, unsent=True))
    else:
        recovery_ledger.mark_execution_unknown(unknown_request(lease.foundation))
        assert lease.port.quarantine_lease(lease.request(target)).outcome == "committed"
        assert (
            lease.port.release_lease(command(lease, target)).error_code
            == "execution_cleanup_pending"
        )
        recovery_ledger.record_stop(
            RecordStop(
                evidence_for(
                    lease.foundation,
                    evidence_kind=StopEvidenceKind.INSTANCE_TERMINATED_BY_CONTROLLER,
                ),
                ExecutionState.ABORTED,
            )
        )
    gateway = {
        table: db.query(f"SELECT * FROM {table}")
        for table in ("submit_intents", "execution_stop_evidence")
    }
    assert lease.port.release_lease(command(lease, target)).outcome == "committed"
    assert db.query(
        "SELECT quota_state FROM admission_attempts WHERE attempt_id='attempt-alt'"
    ) == [{"quota_state": "held" if unsent else "quarantined"}]
    assert lease.port.release_lease(command(lease, "lease-alt-stage-alt")).outcome == "committed"
    assert db.query("SELECT held_attempts, quarantined_attempts FROM workspace_quotas") == [
        {"held_attempts": 1, "quarantined_attempts": 0}
    ]
    assert db.query("SELECT held_slots, quarantined_slots FROM engine_instances") == [
        {"held_slots": 1, "quarantined_slots": 0}
    ]
    assert {table: db.query(f"SELECT * FROM {table}") for table in gateway} == gateway
    if not unsent:
        assert db.query("SELECT node_state FROM host_nodes") == [{"node_state": "quarantined"}]


def _wait_for_both_owners(lease: LeaseDatabase) -> None:
    db = lease.foundation.database
    if db.dialect == "mysql":
        lease.wait_for_lock(2)
        return
    until = time.monotonic() + 2
    while time.monotonic() < until:
        count = db.query(
            "SELECT count(*) AS n FROM pg_stat_activity WHERE usename IN (%s, %s) "
            "AND cardinality(pg_blocking_pids(pid)) > 0",
            (lease.foundation.speech_role, lease.foundation.gateway_role),
        )[0]["n"]
        if count >= 2:
            return
        time.sleep(0.01)
    pytest.fail("Submission/release race requires observed waits from both owners")


def test_native_submit_competes_with_never_submitted_release(
    lease_database: LeaseDatabase, recovery_ledger: Ledger
) -> None:
    lease, db = lease_database, lease_database.foundation.database
    db.query("DELETE FROM submit_intents")  # Remove the unused synthetic seed before behavior.
    request = request_for(
        identity_for(LEASE_ID, submit_intent_id="intent-race", attempt_id="attempt-test")
    )
    at = datetime.now(UTC)
    intent = SubmissionIntent(
        identity=request.identity,
        recorded_at=at,
        updated_at=at,
        request_digest=request.request_digest,
        output_stream_id=request.output_stream_id,
    )
    original = command(lease)

    def submit_once() -> str:
        try:
            recovery_ledger.add(intent, request_document=request.request_document)
            return "submitted"
        except LeaseNotActiveError:
            return "rejected"

    blocker = db.blocker()
    try:
        with blocker.cursor() as cursor:
            cursor.execute("SELECT pool_id FROM resource_pools FOR UPDATE")
            cursor.fetchall()
        with ThreadPoolExecutor(max_workers=2) as executor:
            submission = executor.submit(submit_once)
            settlement = executor.submit(lease.port.release_lease, original)
            try:
                _wait_for_both_owners(lease)
            finally:
                blocker.commit()
            submitted, released = submission.result(timeout=5), settlement.result(timeout=5)
        assert (submitted, released.outcome) in {
            ("submitted", "rejected"),
            ("rejected", "committed"),
        }
        if submitted == "submitted":
            assert released.error_code == "execution_cleanup_pending"
            assert db.query("SELECT held_attempts FROM workspace_quotas") == [{"held_attempts": 1}]
        else:
            assert db.query("SELECT count(*) AS n FROM submit_intents") == [{"n": 0}]
    finally:
        blocker.rollback()
        blocker.close()


def test_budget_release_time_is_after_the_last_submit_lock_wait(
    lease_database: LeaseDatabase,
) -> None:
    lease, db = lease_database, lease_database.foundation.database
    stopped(lease)
    request = command(lease)
    blocker = db.blocker()
    try:
        with blocker.cursor() as cursor:
            cursor.execute("SELECT submit_intent_id FROM submit_intents FOR UPDATE")
            cursor.fetchall()
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(lease.port.release_lease, request)
            try:
                lease.wait_for_lock()
                time.sleep(0.1)
                released_at = lease.now()
            finally:
                blocker.commit()
            outcome = future.result(timeout=5)
        assert outcome.outcome == "committed" and outcome.fact is not None
        assert outcome.fact.budget_released_at_us is not None
        assert outcome.fact.budget_released_at_us >= released_at
    finally:
        blocker.rollback()
        blocker.close()


@pytest.mark.parametrize("architecture", ["unified_memory", "discrete_device_memory"])
@pytest.mark.parametrize("isolated", [False, True])
def test_every_dimension_returns_only_the_target_lease_charge(
    lease_database: LeaseDatabase, architecture: str, isolated: bool
) -> None:
    lease, db = lease_database, lease_database.foundation.database
    _unknown(lease)
    _second_stage(lease)
    reserved = (5, 0, 0, 20, 30, 40) if architecture == "unified_memory" else (0, 5, 10, 20, 30, 40)
    capacity = (
        (100, 0, 0, 1000, 10000, 20000)
        if architecture == "unified_memory"
        else (0, 100, 200, 1000, 10000, 20000)
    )
    other = tuple(2 if value else 0 for value in reserved)
    assignments = ", ".join(
        "allocatable_" + name + " = %s, held_" + name + " = %s" for name in DIMENSIONS
    )
    parameters = tuple(
        value
        for cap, base, charge in zip(capacity, other, reserved, strict=True)
        for value in (cap, base + charge)
    )
    db.query(
        f"UPDATE host_nodes SET {assignments}, memory_architecture=%s, quarantine_threshold_ppm=1",
        (*parameters, architecture),
    )
    assignments = ", ".join("reserved_" + name + " = %s" for name in DIMENSIONS)
    db.query(f"UPDATE admission_leases SET {assignments} WHERE lease_id=%s", (*reserved, LEASE_ID))
    db.query(f"UPDATE admission_leases SET {assignments} WHERE lease_id='lease-other'", other)
    if isolated:
        assert lease.port.quarantine_lease(lease.request()).outcome == "committed"
    stopped(
        lease,
        kind="instance_terminated_by_controller" if isolated else "engine_terminal_response",
        state="aborted" if isolated else "succeeded",
    )
    assert lease.port.release_lease(command(lease)).outcome == "committed"
    host = db.query("SELECT * FROM host_nodes")[0]
    assert tuple(host["held_" + name] for name in DIMENSIONS) == other
    assert tuple(host["quarantined_" + name] for name in DIMENSIONS) == (0, 0, 0, 0, 0, 0)
    assert db.query("SELECT held_slots, quarantined_slots FROM engine_instances") == [
        {"held_slots": 1, "quarantined_slots": 0}
    ]


@pytest.mark.parametrize("isolated", [False, True])
def test_execution_release_does_not_return_runtime_baseline(
    lease_database: LeaseDatabase, isolated: bool
) -> None:
    lease, db = lease_database, lease_database.foundation.database
    db.query(
        "UPDATE engine_instances SET base_budget_kind='steady', "
        "peak_unified_physical_memory_bytes=3, steady_unified_physical_memory_bytes=3"
    )
    db.query("UPDATE host_nodes SET held_unified_physical_memory_bytes=4")
    if isolated:
        _unknown(lease)
        assert lease.port.quarantine_lease(lease.request()).outcome == "committed"
    stopped(
        lease,
        kind="instance_terminated_by_controller" if isolated else "engine_terminal_response",
        state="aborted" if isolated else "succeeded",
    )
    assert lease.port.release_lease(command(lease)).outcome == "committed"
    assert db.query(
        "SELECT base_budget_kind, steady_unified_physical_memory_bytes AS memory "
        "FROM engine_instances"
    ) == [{"base_budget_kind": "steady", "memory": 3}]
    assert db.query(
        "SELECT held_unified_physical_memory_bytes AS held, "
        "quarantined_unified_physical_memory_bytes AS q FROM host_nodes"
    ) == [{"held": 3, "q": 0}]


@pytest.mark.parametrize("database", ["postgresql"], indirect=True)
def test_actual_commit_ack_loss_recovers_original_without_second_deduction(
    lease_database: LeaseDatabase,
    database: JobDatabase,
) -> None:
    lease = lease_database
    assert database.dialect == "postgresql" and lease.pool is not None
    stopped(lease)
    request = command(lease)

    class CommitLostPool:
        def __init__(self, pool: SpeechPool) -> None:
            self.pool, self.calls = pool, 0

        @contextmanager
        def connection(self, *, timeout: float) -> Iterator[Any]:
            self.calls += 1
            with self.pool.connection(timeout=timeout) as connection:
                yield connection
            # The actual non-owner DB transaction has committed and returned
            # its connection. Lose only the acknowledgement at the adapter edge.
            raise psycopg.OperationalError("synthetic acknowledgement loss")

    fault = CommitLostPool(lease.pool)
    outcome = PostgresLeaseTransactions(cast(SpeechPool, fault)).release_lease(request)
    assert outcome.outcome == "unknown" and outcome.recovery == "query_original"
    assert outcome.fact is None and fault.calls == 1
    snapshot = lease.snapshot()
    recovered = lease.port.query_lease(LEASE_ID)
    assert recovered.fact is not None and recovered.fact.lease_state == "released"
    assert lease.port.release_lease(request).outcome == "replayed"
    assert lease.snapshot() == snapshot
