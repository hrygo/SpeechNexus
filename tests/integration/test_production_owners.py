"""Owner production PG adapter and identical InnoDB policy/SQL qualification."""

from __future__ import annotations

import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, cast

import psycopg
import pytest
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool
from tests.integration.test_coordination_foundation import Foundation
from tests.integration.test_coordination_foundation import foundation as foundation
from tests.integration.test_production_jobs import database as database
from tests.integration.test_production_leases import LeaseDatabase
from tests.integration.test_production_leases import lease_database as lease_database
from tests.support.job_database import MySQLJobTransactions
from tests.support.owner_database import MySQLOwnerTransactions

from speechnexus_backend.speech.application.owners import OwnerService, OwnerTransactions
from speechnexus_backend.speech.domain.owners import (
    MAX_COUNTER,
    AcquireOwner,
    ActivateOwner,
    OwnerAuthority,
    OwnerResult,
    RenewOwner,
)
from speechnexus_backend.speech.infrastructure.owners_postgres import PostgresOwnerTransactions
from speechnexus_backend.speech.infrastructure.postgres_pool import SpeechPool, speech_pool
from speechnexus_backend.speech.interfaces.owner_transactions import OwnerTransactionBoundary

pytestmark = pytest.mark.integration
DIGEST = "sha256:" + "a" * 64
ACQUIRE = AcquireOwner("tts-realtime", "scheduler-test", 1, 60000)


@dataclass
class OwnerDatabase:
    foundation: Foundation = field(repr=False)
    port: OwnerTransactions = field(repr=False)

    def acquire(self) -> OwnerResult:
        return self.port.acquire_owner(ACQUIRE)

    def authority(self) -> OwnerAuthority:
        fact = self.port.query_owner("tts-realtime").fact
        assert fact is not None and fact.owner_id is not None
        return OwnerAuthority(fact.pool_id, fact.owner_id, fact.fencing_token)

    def seed_barrier(self, token: int = 1) -> None:
        self.foundation.database.query(
            "INSERT INTO gateway_fencing_barriers (gateway_id, pool_id, min_fencing_token, "
            "sealed_evidence_digest) VALUES ('gateway-test', 'tts-realtime', %s, %s)",
            (token, DIGEST),
        )

    def wait_for_lock(self, minimum: int = 1) -> None:
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
                    "SELECT count(*) AS n FROM performance_schema.data_lock_waits w JOIN "
                    "performance_schema.data_locks l "
                    "ON w.REQUESTING_ENGINE_LOCK_ID = l.ENGINE_LOCK_ID "
                    "WHERE l.OBJECT_SCHEMA = %s",
                    (db.schema,),
                )
            if rows[0]["n"] >= minimum:
                return
            time.sleep(0.01)
        pytest.fail("Owner concurrency requires an observed database row-lock wait")


@pytest.fixture
def owner_database(foundation: Foundation) -> Iterator[OwnerDatabase]:
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
        port: OwnerTransactions = PostgresOwnerTransactions(pool)
    else:
        port = MySQLOwnerTransactions(
            MySQLJobTransactions(
                db.url, db.schema, foundation.speech_role, "synthetic_foundation_test"
            )
        )
    try:
        yield OwnerDatabase(foundation, port)
    finally:
        if pool is not None:
            pool.close()


def test_owner_query_is_read_only_and_acquisition_returns_full_epoch(
    owner_database: OwnerDatabase,
) -> None:
    port = owner_database.port
    assert port.query_owner("tts-realtime").outcome == "unknown"
    assert port.query_owner("asr-batch").outcome == "unknown"
    result = owner_database.acquire()
    assert result.outcome == "committed" and result.fact is not None
    assert result.fact.fencing_token == 1 and result.fact.version == 2
    assert result.fact.owner_state == "reconciling_takeover"
    before = owner_database.foundation.database.query("SELECT * FROM resource_pools")
    assert port.query_owner("tts-realtime").fact == result.fact
    assert owner_database.foundation.database.query("SELECT * FROM resource_pools") == before
    assert port.acquire_owner(ACQUIRE).error_code == "version_conflict"
    busy = port.acquire_owner(AcquireOwner("tts-realtime", "other-scheduler", 2, 60000))
    assert busy.error_code == "owner_busy"


def test_renew_does_not_change_token_or_activate_owner(owner_database: OwnerDatabase) -> None:
    first = owner_database.acquire().fact
    assert first is not None
    result = owner_database.port.renew_owner(RenewOwner(owner_database.authority(), 2, 60000))
    assert result.outcome == "committed" and result.fact is not None
    assert result.fact.version == 3 and result.fact.fencing_token == first.fencing_token
    assert result.fact.owner_state == "reconciling_takeover"
    assert result.fact.expires_at_us >= first.expires_at_us
    assert (
        owner_database.port.renew_owner(RenewOwner(owner_database.authority(), 2, 60000)).error_code
        == "version_conflict"
    )


def test_expiry_never_revives_an_epoch_and_takeover_increments_token(
    owner_database: OwnerDatabase,
) -> None:
    owner_database.acquire()
    authority = owner_database.authority()
    owner_database.foundation.database.query("UPDATE resource_pools SET owner_expires_at_us = 0")
    assert (
        owner_database.port.renew_owner(RenewOwner(authority, 2, 60000)).error_code
        == "stale_fencing_token"
    )
    takeover = owner_database.port.acquire_owner(
        AcquireOwner("tts-realtime", "new-scheduler", 2, 60000)
    )
    assert takeover.fact is not None and takeover.fact.fencing_token == 2
    assert takeover.fact.owner_state == "reconciling_takeover"
    assert (
        owner_database.port.renew_owner(RenewOwner(authority, 3, 60000)).error_code
        == "stale_fencing_token"
    )


def test_activation_requires_barriers_and_resolved_dispatch_without_releasing_budgets(
    owner_database: OwnerDatabase,
    lease_database: LeaseDatabase,
) -> None:
    db = owner_database.foundation.database
    owner_database.acquire()
    request = ActivateOwner(owner_database.authority(), 2)
    assert owner_database.port.activate_owner(request).error_code == "reconciliation_required"
    owner_database.seed_barrier()
    assert owner_database.port.activate_owner(request).error_code == "reconciliation_required"
    db.query(
        "UPDATE submit_intents SET execution_state = 'unknown_quarantined', "
        "cleanup_state = 'quarantined'"
    )
    assert owner_database.port.activate_owner(request).error_code == "reconciliation_required"
    assert lease_database.port.quarantine_lease(lease_database.request()).outcome == "committed"
    budgets = [
        db.query(f"SELECT * FROM {table}")
        for table in ("host_nodes", "engine_instances", "workspace_quotas", "admission_leases")
    ]
    result = owner_database.port.activate_owner(request)
    assert result.outcome == "committed" and result.fact is not None
    assert result.fact.owner_state == "active" and result.fact.version == 3
    assert budgets == [
        db.query(f"SELECT * FROM {table}")
        for table in ("host_nodes", "engine_instances", "workspace_quotas", "admission_leases")
    ]


def test_activation_does_not_ignore_a_second_gateway_or_stale_barrier(
    owner_database: OwnerDatabase,
    lease_database: LeaseDatabase,
) -> None:
    db = owner_database.foundation.database
    owner_database.acquire()
    db.query(
        "UPDATE submit_intents SET execution_state='unknown_quarantined',"
        "cleanup_state='quarantined'"
    )
    assert lease_database.port.quarantine_lease(lease_database.request()).outcome == "committed"
    db.query(
        "INSERT INTO engine_instances (instance_id, instance_generation, host_node_id, pool_id, "
        "gateway_id, binding_revision_id, manifest_id, approved_template_id, management_mode, "
        "loading_policy, config_digest, instance_state, max_slots) VALUES "
        "('instance-other', 1, 'host-test', 'tts-realtime', 'gateway-other', 'binding-test', "
        "'manifest-test', 'template-test', 'managed', 'on_demand', %s, 'ready', 1)",
        (DIGEST,),
    )
    owner_database.seed_barrier()
    request = ActivateOwner(owner_database.authority(), 2)
    assert owner_database.port.activate_owner(request).error_code == "reconciliation_required"
    db.query(
        "INSERT INTO gateway_fencing_barriers (gateway_id, pool_id, min_fencing_token, "
        "sealed_evidence_digest) VALUES ('gateway-other', 'tts-realtime', 1, %s)",
        (DIGEST,),
    )
    db.query("UPDATE resource_pools SET fencing_token = 2")
    request = ActivateOwner(OwnerAuthority("tts-realtime", "scheduler-test", 2), 2)
    assert owner_database.port.activate_owner(request).error_code == "reconciliation_required"
    db.query("UPDATE gateway_fencing_barriers SET min_fencing_token = 2")
    assert owner_database.port.activate_owner(request).outcome == "committed"


def test_counter_overflow_and_missing_pool_leave_rows_unchanged(
    owner_database: OwnerDatabase,
) -> None:
    db = owner_database.foundation.database
    assert (
        owner_database.port.acquire_owner(
            AcquireOwner("asr-batch", "scheduler-test", 1, 100)
        ).error_code
        == "not_found"
    )
    db.query(
        "UPDATE resource_pools SET owner_id = 'scheduler-old', fencing_token = %s", (MAX_COUNTER,)
    )
    before = db.query("SELECT * FROM resource_pools")
    assert owner_database.acquire().error_code == "invalid_counter"
    assert db.query("SELECT * FROM resource_pools") == before


def test_two_schedulers_contend_on_the_authority_row(owner_database: OwnerDatabase) -> None:
    db = owner_database.foundation.database
    blocker = db.blocker()
    try:
        with blocker.cursor() as cursor:
            cursor.execute("SELECT pool_id FROM resource_pools FOR UPDATE")
        with ThreadPoolExecutor(max_workers=2) as executor:
            first = executor.submit(owner_database.port.acquire_owner, ACQUIRE)
            second = executor.submit(
                owner_database.port.acquire_owner,
                AcquireOwner("tts-realtime", "other-scheduler", 1, 60000),
            )
            owner_database.wait_for_lock(minimum=2)
            blocker.commit()
            results = [first.result(timeout=5), second.result(timeout=5)]
        assert sorted(r.outcome for r in results) == ["committed", "conflict"]
        fact = owner_database.port.query_owner("tts-realtime").fact
        assert fact is not None and fact.fencing_token == 1
    finally:
        blocker.rollback()
        blocker.close()


def test_wire_boundary_returns_a_real_owner_snapshot(owner_database: OwnerDatabase) -> None:
    boundary = OwnerTransactionBoundary(OwnerService(owner_database.port))
    acquired = boundary.acquire_owner(
        {
            "pool_id": "tts-realtime",
            "owner_id": "scheduler-test",
            "expected_version": "1",
            "ttl_ms": 60000,
        }
    )
    snapshot = acquired["value"]
    assert snapshot["authority"]["fencing_token"] == 1
    result = boundary.query_outcome({"target": {"kind": "pool", "pool_id": "tts-realtime"}})
    assert result["value"] == snapshot and result["outcome"] == "replayed"


@pytest.mark.parametrize("operation", ["renew", "activate"])
def test_expiry_is_rechecked_using_database_time_after_lock_waits(
    owner_database: OwnerDatabase,
    operation: str,
) -> None:
    db = owner_database.foundation.database
    owner_database.acquire()
    authority = owner_database.authority()
    owner_database.seed_barrier()
    db.query("UPDATE submit_intents SET execution_state = 'unknown_quarantined'")
    clock_sql = (
        "SELECT CAST(EXTRACT(EPOCH FROM clock_timestamp()) * 1000000 AS BIGINT) AS now_us"
        if db.dialect == "postgresql"
        else "SELECT CAST(UNIX_TIMESTAMP(CURRENT_TIMESTAMP(6)) * 1000000 AS SIGNED) AS now_us"
    )
    expires = db.query(clock_sql)[0]["now_us"] + 500000
    db.query("UPDATE resource_pools SET owner_expires_at_us = %s", (expires,))
    blocker = db.blocker()
    try:
        with blocker.cursor() as cursor:
            table = "resource_pools" if operation == "renew" else "gateway_fencing_barriers"
            cursor.execute(f"SELECT pool_id FROM {table} FOR UPDATE")
        with ThreadPoolExecutor(max_workers=1) as executor:
            if operation == "renew":
                pending = executor.submit(
                    owner_database.port.renew_owner, RenewOwner(authority, 2, 60000)
                )
            else:
                pending = executor.submit(
                    owner_database.port.activate_owner, ActivateOwner(authority, 2)
                )
            owner_database.wait_for_lock()
            deadline = time.monotonic() + 2
            while db.query(clock_sql)[0]["now_us"] < expires:
                assert time.monotonic() < deadline, "Database clock did not reach owner expiry"
                time.sleep(0.01)
            blocker.commit()
            result = pending.result(timeout=5)
        assert result.error_code == "stale_fencing_token"
        fact = owner_database.port.query_owner("tts-realtime").fact
        assert fact is not None and fact.version == 2 and fact.expires_at_us == expires
    finally:
        blocker.rollback()
        blocker.close()


@pytest.mark.parametrize("database", ["postgresql"], indirect=True)
@pytest.mark.parametrize("fault", ["before", "after"])
def test_unknown_commit_only_allows_current_authority_query(
    owner_database: OwnerDatabase,
    fault: str,
) -> None:
    db = owner_database.foundation.database
    armed = [False]
    failures = [0]

    class FaultConnection(psycopg.Connection[dict[str, Any]]):
        def commit(self) -> None:
            if not armed[0]:
                return super().commit()
            armed[0] = False
            failures[0] += 1
            if fault == "before":
                self.execute("SELECT pg_terminate_backend(pg_backend_pid())")
            super().commit()
            raise psycopg.OperationalError("synthetic lost owner commit acknowledgment")

    def configure(connection: FaultConnection) -> None:
        connection.execute(
            "SELECT set_config('search_path', %s, false)", (db.schema + ",pg_catalog",)
        )
        connection.execute("SET TIME ZONE 'UTC'")
        connection.commit()

    with ConnectionPool[FaultConnection](
        make_conninfo(
            db.url, user=owner_database.foundation.speech_role, password="synthetic_foundation_test"
        ),
        connection_class=FaultConnection,
        kwargs={"row_factory": dict_row},
        configure=configure,
        min_size=1,
        max_size=1,
        max_waiting=2,
    ) as fault_pool:
        fault_pool.wait(timeout=3)
        armed[0] = True
        fault_port = PostgresOwnerTransactions(cast(SpeechPool, fault_pool))
        result = fault_port.acquire_owner(ACQUIRE)
        assert result.outcome == "unknown" and result.recovery == "query_current_authority"
        assert failures[0] == 1
        before = db.query("SELECT * FROM resource_pools")
        recovered = owner_database.port.query_owner("tts-realtime")
        assert recovered.outcome == ("unknown" if fault == "before" else "replayed")
        assert db.query("SELECT * FROM resource_pools") == before
        assert before[0]["fencing_token"] == (0 if fault == "before" else 1)


@pytest.mark.parametrize("database", ["postgresql"], indirect=True)
def test_temporary_relation_cannot_shadow_the_authority_table(
    owner_database: OwnerDatabase,
) -> None:
    db = owner_database.foundation.database
    with speech_pool(
        make_conninfo(
            db.url, user=owner_database.foundation.speech_role, password="synthetic_foundation_test"
        ),
        schema=db.schema,
        max_size=1,
    ) as pool:
        with pool.connection() as connection:
            connection.execute(
                "CREATE TEMP TABLE resource_pools "
                "(pool_id TEXT, owner_id TEXT, fencing_token BIGINT, "
                "owner_expires_at_us BIGINT, owner_state TEXT, version BIGINT)"
            )
            connection.execute(
                "INSERT INTO pg_temp.resource_pools VALUES "
                "('tts-realtime', 'scheduler-injected', 99, %s, 'active', 99)",
                (MAX_COUNTER,),
            )
        port = PostgresOwnerTransactions(pool)
        assert port.query_owner("tts-realtime").outcome == "unknown"
        assert port.acquire_owner(ACQUIRE).outcome == "committed"
        with pool.connection() as connection:
            row = connection.execute("SELECT fencing_token FROM pg_temp.resource_pools").fetchone()
            assert row is not None and row["fencing_token"] == 99
        assert db.query("SELECT fencing_token FROM resource_pools")[0]["fencing_token"] == 1
