"""PG and InnoDB isolation with identical SQL/domain and non-owner roles."""

from __future__ import annotations

import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from typing import Any

import pytest
from psycopg.conninfo import make_conninfo
from tests.integration.test_coordination_foundation import Foundation
from tests.integration.test_coordination_foundation import foundation as foundation
from tests.integration.test_gateway_recovery import pending, unknown_request
from tests.integration.test_gateway_recovery import recovery_ledger as recovery_ledger
from tests.integration.test_gateway_stop_evidence import stop_ledger as stop_ledger
from tests.integration.test_production_jobs import database as database
from tests.support.gateway_database import MySQLSubmissionLedger
from tests.support.job_database import MySQLJobTransactions
from tests.support.lease_database import MySQLLeaseTransactions

from speechnexus_backend.speech.application.leases import LeaseService, LeaseTransactions
from speechnexus_backend.speech.domain.leases import LeaseRejectedError, QuarantineLease
from speechnexus_backend.speech.domain.owners import MAX_VERSION
from speechnexus_backend.speech.infrastructure import leases_sql
from speechnexus_backend.speech.infrastructure.coordination_grants import DIMENSIONS
from speechnexus_backend.speech.infrastructure.leases_postgres import PostgresLeaseTransactions
from speechnexus_backend.speech.infrastructure.postgres_pool import SpeechPool, speech_pool
from speechnexus_backend.speech.interfaces.lease_transactions import LeaseTransactionBoundary
from speechnexus_gateway.infrastructure.submit_postgres import PostgresSubmissionLedger

pytestmark = pytest.mark.integration
LEASE_ID = "lease-test-stage-main"
REASON = "engine_transport_unknown"
TABLES = (
    "resource_pools",
    "workspace_quotas",
    "host_nodes",
    "engine_instances",
    "admission_attempts",
    "admission_leases",
    "submit_intents",
    "execution_stop_evidence",
    "coordination_jobs",
    "coordination_outbox",
)


@dataclass
class LeaseDatabase:
    foundation: Foundation = field(repr=False)
    port: LeaseTransactions = field(repr=False)
    pool: SpeechPool | None = field(repr=False)

    def now(self) -> int:
        expression = (
            "EXTRACT(EPOCH FROM clock_timestamp()) * 1000000"
            if self.foundation.database.dialect == "postgresql"
            else "UNIX_TIMESTAMP(CURRENT_TIMESTAMP(6)) * 1000000"
        )
        return int(self.foundation.database.query(f"SELECT {expression} AS now_us")[0]["now_us"])

    def request(self, lease_id: str = LEASE_ID) -> QuarantineLease:
        return QuarantineLease(lease_id, REASON, self.now())

    def snapshot(self, tables: tuple[str, ...] = TABLES) -> dict[str, Any]:
        return {table: self.foundation.database.query(f"SELECT * FROM {table}") for table in tables}

    def wait_for_lock(self, count: int = 1) -> None:
        db = self.foundation.database
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            if db.dialect == "postgresql":
                rows = db.query(
                    "SELECT count(*) AS n FROM pg_stat_activity WHERE usename = %s "
                    "AND cardinality(pg_blocking_pids(pid)) > 0",
                    (self.foundation.speech_role,),
                )
            else:
                rows = db.query(
                    "SELECT count(*) AS n FROM performance_schema.data_lock_waits w "
                    "JOIN performance_schema.data_locks l ON "
                    "w.REQUESTING_ENGINE_LOCK_ID = l.ENGINE_LOCK_ID WHERE l.OBJECT_SCHEMA = %s",
                    (db.schema,),
                )
            if rows[0]["n"] >= count:
                return
            time.sleep(0.01)
        pytest.fail("Lease concurrency needs an observed database row-lock wait")


@pytest.fixture
def lease_database(foundation: Foundation) -> Iterator[LeaseDatabase]:
    db = foundation.database
    pool: SpeechPool | None = None
    if db.dialect == "postgresql":
        pool = speech_pool(
            make_conninfo(
                db.url, user=foundation.speech_role, password="synthetic_foundation_test"
            ),
            schema=db.schema,
        )
        pool.open()
        port: LeaseTransactions = PostgresLeaseTransactions(pool)
    else:
        port = MySQLLeaseTransactions(
            MySQLJobTransactions(
                db.url,
                db.schema,
                foundation.speech_role,
                "synthetic_foundation_test",
            )
        )
    try:
        yield LeaseDatabase(foundation, port, pool)
    finally:
        if pool is not None:
            pool.close()


def _unknown(lease: LeaseDatabase) -> None:
    # Synthetic persisted execution facts, separate from the native B joint case.
    lease.foundation.database.query(
        "UPDATE submit_intents SET execution_state = 'unknown_quarantined', "
        "cleanup_state = 'quarantined' WHERE lease_id = %s",
        (LEASE_ID,),
    )


def test_isolation_is_atomic_preserves_budgets_and_queries_never_write(
    lease_database: LeaseDatabase,
) -> None:
    lease, db = lease_database, lease_database.foundation.database
    boundary = LeaseTransactionBoundary(LeaseService(lease.port))
    before = lease.snapshot()
    assert (
        boundary.query_outcome({"target": {"kind": "lease", "lease_id": LEASE_ID}})["value"][
            "lease_state"
        ]
        == "reserved"
    )
    assert lease.port.query_lease("lease-missing").outcome == "unknown"
    assert lease.snapshot() == before
    assert lease.port.quarantine_lease(lease.request()).error_code == "execution_cleanup_pending"
    assert lease.snapshot() == before  # deadline_at_us=1 is long expired, but is not evidence.
    _unknown(lease)
    gateway = db.query("SELECT * FROM submit_intents")
    request = lease.request()
    result = boundary.quarantine_lease(
        {"lease_id": LEASE_ID, "reason_code": REASON, "observed_at_us": request.observed_at_us}
    )
    assert result["outcome"] == "committed" and result["value"]["version"] == "2"
    assert db.query(
        "SELECT held_unified_physical_memory_bytes AS held, "
        "quarantined_unified_physical_memory_bytes AS q FROM host_nodes"
    ) == [{"held": 0, "q": 1}]
    assert db.query("SELECT held_slots, quarantined_slots FROM engine_instances") == [
        {"held_slots": 0, "quarantined_slots": 1}
    ]
    assert db.query("SELECT held_attempts, quarantined_attempts FROM workspace_quotas") == [
        {"held_attempts": 0, "quarantined_attempts": 1}
    ]
    snapshot = lease.snapshot()
    assert (
        lease.port.quarantine_lease(replace(request, observed_at_us=lease.now())).outcome
        == "replayed"
    )
    assert (
        boundary.query_outcome({"target": {"kind": "lease", "lease_id": LEASE_ID}})["value"]
        == result["value"]
    )
    assert lease.snapshot() == snapshot
    assert db.query("SELECT * FROM submit_intents") == gateway
    assert db.query("SELECT budget_released_at_us FROM admission_leases") == [
        {"budget_released_at_us": None}
    ]


@pytest.mark.parametrize(
    "state,cleanup",
    [
        ("submitting", "pending"),
        ("running", "pending"),
        ("succeeded", "complete"),
        ("failed", "complete"),
        ("aborted", "complete"),
        ("unknown_quarantined", "pending"),
    ],
)
def test_active_or_terminal_execution_is_not_authority_to_quarantine(
    lease_database: LeaseDatabase, state: str, cleanup: str
) -> None:
    lease, db = lease_database, lease_database.foundation.database
    db.query("UPDATE submit_intents SET execution_state = %s, cleanup_state = %s", (state, cleanup))
    snapshot = lease.snapshot()
    assert lease.port.quarantine_lease(lease.request()).error_code == "execution_cleanup_pending"
    assert lease.snapshot() == snapshot


def test_original_reason_conflicts_and_first_observation_survives_gateway_resolution(
    lease_database: LeaseDatabase,
) -> None:
    lease, db = lease_database, lease_database.foundation.database
    _unknown(lease)
    request = lease.request()
    assert lease.port.quarantine_lease(request).outcome == "committed"
    snapshot = lease.snapshot()
    assert (
        lease.port.quarantine_lease(
            replace(request, reason_code="cancellation_unconfirmed")
        ).outcome
        == "conflict"
    )
    assert lease.snapshot() == snapshot
    db.query("UPDATE submit_intents SET execution_state = 'aborted', cleanup_state = 'complete'")
    snapshot = lease.snapshot()
    assert lease.port.quarantine_lease(request).outcome == "replayed"
    assert lease.snapshot() == snapshot
    fact = db.query(
        "SELECT quarantine_observed_at_us, quarantine_reason_code FROM admission_leases"
    )[0]
    assert fact == {
        "quarantine_observed_at_us": request.observed_at_us,
        "quarantine_reason_code": REASON,
    }


def test_two_reconcilers_compete_and_only_one_moves_budget(lease_database: LeaseDatabase) -> None:
    lease, db = lease_database, lease_database.foundation.database
    _unknown(lease)
    command = lease.request()
    blocker = db.blocker()
    try:
        with blocker.cursor() as cursor:
            cursor.execute("SELECT pool_id FROM resource_pools FOR UPDATE")
            cursor.fetchall()
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(lease.port.quarantine_lease, command) for _ in range(2)]
            try:
                lease.wait_for_lock(2)
            finally:
                blocker.commit()
            results = [future.result(timeout=5) for future in futures]
        assert sorted(result.outcome for result in results) == ["committed", "replayed"]
        assert db.query("SELECT version FROM admission_leases") == [{"version": 2}]
        assert db.query("SELECT held_attempts, quarantined_attempts FROM workspace_quotas") == [
            {"held_attempts": 0, "quarantined_attempts": 1}
        ]
    finally:
        blocker.rollback()
        blocker.close()


def _second_stage(lease: LeaseDatabase) -> None:
    db = lease.foundation.database
    db.query("UPDATE engine_instances SET held_slots = 2")
    db.query("UPDATE host_nodes SET held_unified_physical_memory_bytes = 2")
    db.query(
        "UPDATE admission_attempts SET required_stage_ids = %s", ('["stage-main","stage-other"]',)
    )
    db.query(
        "INSERT INTO admission_leases (lease_id, operation_id, attempt_id, stage_id, host_node_id, "
        "instance_id, instance_generation, pool_id, gateway_id, fencing_token, reserved_slots, "
        "deadline_at_us, reserved_unified_physical_memory_bytes) SELECT 'lease-other', "
        "operation_id, attempt_id, 'stage-other', host_node_id, instance_id, instance_generation, "
        "pool_id, gateway_id, fencing_token, reserved_slots, deadline_at_us, "
        "reserved_unified_physical_memory_bytes FROM admission_leases WHERE lease_id = %s",
        (LEASE_ID,),
    )
    db.query(
        "INSERT INTO submit_intents (submit_intent_id, lease_id, operation_id, attempt_id, "
        "command_digest, request_digest, request_document, deadline_at_us, recorded_at_us, "
        "updated_at_us, execution_state, cleanup_state) SELECT 'submit-other', 'lease-other', "
        "operation_id, attempt_id, command_digest, request_digest, request_document, "
        "deadline_at_us, "
        "recorded_at_us, updated_at_us, 'unknown_quarantined', 'quarantined' FROM submit_intents "
        "WHERE lease_id = %s",
        (LEASE_ID,),
    )


@pytest.mark.parametrize("cross_pool", [False, True])
def test_concurrent_stages_isolate_attempt_quota_once(
    lease_database: LeaseDatabase,
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
            "max_slots, held_slots) "
            "SELECT 'instance-other', instance_generation, host_node_id, 'asr-realtime', "
            "gateway_id, binding_revision_id, manifest_id, approved_template_id, management_mode, "
            "loading_policy, config_digest, instance_state, max_slots, 1 FROM engine_instances "
            "WHERE instance_id = 'instance-test'"
        )
        db.query("UPDATE engine_instances SET held_slots = 1 WHERE instance_id = 'instance-test'")
        db.query(
            "UPDATE admission_leases SET instance_id = 'instance-other', "
            "pool_id = 'asr-realtime' WHERE lease_id = 'lease-other'"
        )
    db.query("UPDATE host_nodes SET quarantine_threshold_ppm = 1")
    commands = [lease.request(key) for key in (LEASE_ID, "lease-other")]
    blocker = db.blocker()
    try:
        with blocker.cursor() as cursor:
            cursor.execute("SELECT workspace_id FROM workspace_quotas FOR UPDATE")
            cursor.fetchall()
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [
                executor.submit(lease.port.quarantine_lease, command) for command in commands
            ]
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
    ) == [{"held_attempts": 0, "quarantined_attempts": 1, "version": 2}]
    assert db.query("SELECT quota_state, version FROM admission_attempts") == [
        {"quota_state": "quarantined", "version": 2}
    ]
    slots = db.query("SELECT held_slots, quarantined_slots FROM engine_instances")
    assert sum(row["held_slots"] for row in slots) == 0
    assert sum(row["quarantined_slots"] for row in slots) == 2
    assert db.query(
        "SELECT node_state, quarantined_unified_physical_memory_bytes AS q FROM host_nodes"
    ) == [{"node_state": "quarantined", "q": 2}]


@pytest.mark.parametrize("drift", ["host", "instance", "quota", "lease_version", "host_version"])
def test_corrupt_or_exhausted_projection_rolls_back_all_tables(
    lease_database: LeaseDatabase, drift: str
) -> None:
    lease, db = lease_database, lease_database.foundation.database
    _unknown(lease)
    if drift == "host":
        db.query("UPDATE host_nodes SET held_unified_physical_memory_bytes = 0")
    elif drift == "instance":
        db.query("UPDATE engine_instances SET held_slots = 0")
    elif drift == "quota":
        db.query("UPDATE workspace_quotas SET held_attempts = 0")
    elif drift == "lease_version":
        db.query("UPDATE admission_leases SET version = %s", (MAX_VERSION,))
    else:
        db.query("UPDATE host_nodes SET version = %s", (MAX_VERSION,))
    snapshot = lease.snapshot()
    assert lease.port.quarantine_lease(lease.request()).error_code == "reconciliation_required"
    assert lease.snapshot() == snapshot


def test_mid_transaction_failure_rolls_back_quota_and_host_moves(
    lease_database: LeaseDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    lease = lease_database
    _unknown(lease)
    snapshot = lease.snapshot()
    update = leases_sql._update

    def fail_after_host(cursor: Any, query: str, parameters: tuple[object, ...]) -> None:
        update(cursor, query, parameters)
        if query.startswith("UPDATE host_nodes"):
            raise LeaseRejectedError("reconciliation_required")

    monkeypatch.setattr(leases_sql, "_update", fail_after_host)
    assert lease.port.quarantine_lease(lease.request()).outcome == "rejected"
    assert lease.snapshot() == snapshot


def test_database_time_is_read_after_actual_wait(lease_database: LeaseDatabase) -> None:
    lease, db = lease_database, lease_database.foundation.database
    _unknown(lease)
    command = replace(lease.request(), observed_at_us=lease.now() + 250000)
    blocker = db.blocker()
    try:
        with blocker.cursor() as cursor:
            cursor.execute("SELECT host_node_id FROM host_nodes FOR UPDATE")
            cursor.fetchall()
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(lease.port.quarantine_lease, command)
            try:
                lease.wait_for_lock()
                time.sleep(0.3)
                assert lease.now() >= command.observed_at_us
            finally:
                blocker.commit()
            assert future.result(timeout=5).outcome == "committed"
    finally:
        blocker.rollback()
        blocker.close()


def test_latest_gateway_fact_after_submit_lock_wait_is_authoritative(
    lease_database: LeaseDatabase,
) -> None:
    lease, db = lease_database, lease_database.foundation.database
    _unknown(lease)
    snapshot = lease.snapshot(tuple(t for t in TABLES if t != "submit_intents"))
    blocker = db.blocker()
    command = lease.request()
    try:
        with blocker.cursor() as cursor:
            cursor.execute("SELECT submit_intent_id FROM submit_intents FOR UPDATE")
            cursor.fetchall()
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(lease.port.quarantine_lease, command)
            try:
                lease.wait_for_lock()
                with blocker.cursor() as cursor:
                    cursor.execute(
                        "UPDATE submit_intents SET execution_state = 'failed', "
                        "cleanup_state = 'complete'"
                    )
            finally:
                blocker.commit()
            assert future.result(timeout=5).error_code == "execution_cleanup_pending"
        assert lease.snapshot(tuple(t for t in TABLES if t != "submit_intents")) == snapshot
    finally:
        blocker.rollback()
        blocker.close()


def test_native_gateway_unknown_fact_is_consumed_without_changing_gateway_history(
    lease_database: LeaseDatabase,
    recovery_ledger: PostgresSubmissionLedger | MySQLSubmissionLedger,
) -> None:
    lease, db = lease_database, lease_database.foundation.database
    pending(lease.foundation, recovery_ledger)
    # Match synthetic admitted rows: one initial attempt and a second with two stages.
    db.query("UPDATE workspace_quotas SET held_attempts = 2")
    db.query("UPDATE engine_instances SET max_slots = 3, held_slots = 3")
    db.query("UPDATE host_nodes SET held_unified_physical_memory_bytes = 3")
    recovery_ledger.mark_execution_unknown(unknown_request(lease.foundation))
    gateway = db.query("SELECT * FROM submit_intents")
    result = lease.port.quarantine_lease(lease.request("lease-alt-stage-main"))
    assert result.outcome == "committed"
    assert db.query("SELECT * FROM submit_intents") == gateway
    assert db.query(
        "SELECT held_unified_physical_memory_bytes AS held, "
        "quarantined_unified_physical_memory_bytes AS q FROM host_nodes"
    ) == [{"held": 2, "q": 1}]
    assert db.query("SELECT held_attempts, quarantined_attempts FROM workspace_quotas") == [
        {"held_attempts": 1, "quarantined_attempts": 1}
    ]


@pytest.mark.parametrize("architecture", ["unified_memory", "discrete_device_memory"])
def test_all_resource_dimensions_move_without_releasing_other_occupancy(
    lease_database: LeaseDatabase,
    architecture: str,
) -> None:
    lease, db = lease_database, lease_database.foundation.database
    _unknown(lease)
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
    db.query(
        f"UPDATE host_nodes SET {assignments}, memory_architecture = %s",
        (
            *tuple(
                value
                for cap, base, charge in zip(capacity, other, reserved, strict=True)
                for value in (cap, base + charge)
            ),
            architecture,
        ),
    )
    assignments = ", ".join("reserved_" + name + " = %s" for name in DIMENSIONS)
    db.query(f"UPDATE admission_leases SET {assignments}", reserved)
    assert lease.port.quarantine_lease(lease.request()).outcome == "committed"
    host = db.query("SELECT * FROM host_nodes")[0]
    assert tuple(host["held_" + name] for name in DIMENSIONS) == other
    assert tuple(host["quarantined_" + name] for name in DIMENSIONS) == reserved
