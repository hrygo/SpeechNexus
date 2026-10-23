"""Real PG claims and identical SQL/domain under the isolated InnoDB oracle."""

from __future__ import annotations

import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from typing import Any, cast

import psycopg
import pymysql
import pytest
from psycopg.conninfo import make_conninfo
from tests.integration.test_coordination_foundation import Foundation
from tests.integration.test_coordination_foundation import foundation as foundation
from tests.integration.test_production_jobs import REQUEST as CREATE
from tests.integration.test_production_jobs import database as database
from tests.support.claim_database import MySQLClaimTransactions
from tests.support.job_database import MySQLJobTransactions

from speechnexus_backend.speech.application.claims import ClaimService, ClaimTransactions
from speechnexus_backend.speech.domain.claims import MAX_COUNTER, ClaimJob
from speechnexus_backend.speech.infrastructure.claims_postgres import PostgresClaimTransactions
from speechnexus_backend.speech.infrastructure.postgres_pool import SpeechPool, speech_pool
from speechnexus_backend.speech.interfaces.claim_transactions import ClaimTransactionBoundary

pytestmark = pytest.mark.integration
REQUEST = ClaimJob("job-test", "worker-test", 0, 60000)


@dataclass
class ClaimDatabase:
    foundation: Foundation = field(repr=False)
    port: ClaimTransactions = field(repr=False)
    pool: SpeechPool | None = field(repr=False)

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
        pytest.fail("Claim concurrency requires an observed real row-lock wait")

    def now(self) -> int:
        expression = (
            "EXTRACT(EPOCH FROM clock_timestamp()) * 1000000"
            if self.foundation.database.dialect == "postgresql"
            else "UNIX_TIMESTAMP(CURRENT_TIMESTAMP(6)) * 1000000"
        )
        return int(self.foundation.database.query(f"SELECT {expression} AS now_us")[0]["now_us"])


@pytest.fixture
def claim_database(foundation: Foundation) -> Iterator[ClaimDatabase]:
    db = foundation.database
    assert db.port.create_job(CREATE).outcome == "committed"
    pool: SpeechPool | None = None
    if db.dialect == "postgresql":
        pool = speech_pool(
            make_conninfo(
                db.url, user=foundation.speech_role, password="synthetic_foundation_test"
            ),
            schema=db.schema,
        )
        pool.open()
        port: ClaimTransactions = PostgresClaimTransactions(pool)
    else:
        port = MySQLClaimTransactions(
            MySQLJobTransactions(
                db.url, db.schema, foundation.speech_role, "synthetic_foundation_test"
            )
        )
    try:
        yield ClaimDatabase(foundation, port, pool)
    finally:
        if pool is not None:
            pool.close()


def test_claim_and_read_only_current_authority_do_not_release_or_dispatch(
    claim_database: ClaimDatabase,
) -> None:
    db = claim_database.foundation.database
    before = {
        table: db.query(f"SELECT * FROM {table}")
        for table in (
            "coordination_outbox",
            "admission_leases",
            "submit_intents",
            "workspace_quotas",
            "engine_instances",
            "host_nodes",
        )
    }
    boundary = ClaimTransactionBoundary(ClaimService(claim_database.port))
    assert (
        boundary.query_outcome({"target": {"kind": "job", "job_id": "job-test"}})["outcome"]
        == "unknown"
    )
    result = boundary.claim_job(
        {
            "job_id": "job-test",
            "worker_id": "worker-test",
            "expected_claim_version": 0,
            "ttl_ms": 60000,
        }
    )
    assert result["outcome"] == "committed"
    assert result["value"]["claim_version"] == 1
    assert result["value"]["expires_at_us"] > claim_database.now()
    snapshot = db.query("SELECT * FROM coordination_jobs")
    for _ in range(2):
        queried = boundary.query_outcome({"target": {"kind": "job", "job_id": "job-test"}})
        assert queried["value"] == result["value"]
    assert db.query("SELECT * FROM coordination_jobs") == snapshot
    assert {table: db.query(f"SELECT * FROM {table}") for table in before} == before
    for command in (
        REQUEST,
        replace(REQUEST, expected_claim_version=1),
        replace(REQUEST, worker_id="worker-other", expected_claim_version=1),
    ):
        assert claim_database.port.claim_job(command).error_code == "version_conflict"


def test_two_workers_compete_for_one_version_under_actual_lock_wait(
    claim_database: ClaimDatabase,
) -> None:
    db = claim_database.foundation.database
    blocker = db.blocker()
    try:
        with blocker.cursor() as cursor:
            cursor.execute(
                "SELECT job_id FROM coordination_jobs WHERE job_id = %s FOR UPDATE", ("job-test",)
            )
            cursor.fetchall()
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [
                executor.submit(claim_database.port.claim_job, command)
                for command in (REQUEST, replace(REQUEST, worker_id="worker-other"))
            ]
            try:
                claim_database.wait_for_lock(2)
            finally:
                blocker.commit()
            results = [future.result(timeout=4) for future in futures]
        assert sorted(result.outcome for result in results) == ["committed", "conflict"]
        assert db.query("SELECT claim_version FROM coordination_jobs") == [{"claim_version": 1}]
        assert db.counts() == (1, 1)
    finally:
        blocker.rollback()
        blocker.close()


def test_expiry_is_evaluated_after_lock_wait_and_old_version_stays_fenced(
    claim_database: ClaimDatabase,
) -> None:
    db = claim_database.foundation.database
    assert claim_database.port.claim_job(REQUEST).outcome == "committed"
    db.query(
        "UPDATE coordination_jobs SET claim_expires_at_us = %s", (claim_database.now() + 300000,)
    )
    blocker = db.blocker()
    try:
        with blocker.cursor() as cursor:
            cursor.execute(
                "SELECT job_id FROM coordination_jobs WHERE job_id = %s FOR UPDATE", ("job-test",)
            )
            cursor.fetchall()
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(
                claim_database.port.claim_job,
                replace(REQUEST, worker_id="worker-next", expected_claim_version=1),
            )
            try:
                claim_database.wait_for_lock()
                time.sleep(0.35)
                released_at = claim_database.now()
            finally:
                blocker.commit()
            result = future.result(timeout=4)
        assert result.outcome == "committed" and result.fact is not None
        assert result.fact.claim_version == 2
        assert result.fact.expires_at_us >= released_at + 60000000
        assert (
            claim_database.port.claim_job(replace(REQUEST, expected_claim_version=1)).error_code
            == "version_conflict"
        )
    finally:
        blocker.rollback()
        blocker.close()


def test_query_reports_expired_or_later_authority_without_renewing_it(
    claim_database: ClaimDatabase,
) -> None:
    port, db = claim_database.port, claim_database.foundation.database
    port.claim_job(REQUEST)
    db.query("UPDATE coordination_jobs SET claim_expires_at_us = 1")
    expired = port.query_claim("job-test")
    assert expired.fact is not None and expired.fact.expires_at_us == 1
    assert (
        port.claim_job(replace(REQUEST, worker_id="worker-next", expected_claim_version=1)).outcome
        == "committed"
    )
    later = port.query_claim("job-test")
    assert later.fact is not None and later.fact.worker_id == "worker-next"
    assert later.fact.claim_version == 2
    assert db.counts() == (1, 1)


@pytest.mark.parametrize("job_state", ["completed", "failed", "cancelled"])
def test_terminal_jobs_cannot_be_claimed(claim_database: ClaimDatabase, job_state: str) -> None:
    db = claim_database.foundation.database
    db.query("UPDATE coordination_jobs SET job_state = %s", (job_state,))
    assert claim_database.port.claim_job(REQUEST).error_code == "version_conflict"
    assert db.query("SELECT claim_version FROM coordination_jobs") == [{"claim_version": 0}]


def test_counter_overflow_and_missing_job_preserve_original_rows(
    claim_database: ClaimDatabase,
) -> None:
    db, port = claim_database.foundation.database, claim_database.port
    assert port.claim_job(replace(REQUEST, job_id="job-absent")).error_code == "not_found"
    assert port.query_claim("job-absent").error_code == "not_found"
    db.query(
        "UPDATE coordination_jobs SET claim_owner_id = 'worker-test', "
        "claim_version = %s, claim_expires_at_us = 1",
        (MAX_COUNTER,),
    )
    original = db.query("SELECT * FROM coordination_jobs")
    assert (
        port.claim_job(replace(REQUEST, expected_claim_version=MAX_COUNTER)).error_code
        == "invalid_counter"
    )
    assert db.query("SELECT * FROM coordination_jobs") == original


def test_gateway_cannot_mutate_worker_claim_authority(claim_database: ClaimDatabase) -> None:
    expected = (
        psycopg.errors.InsufficientPrivilege
        if claim_database.foundation.database.dialect == "postgresql"
        else pymysql.err.OperationalError
    )
    with (
        claim_database.foundation.connection("gateway") as connection,
        pytest.raises(expected),
        connection.cursor() as cursor,
    ):
        cursor.execute(
            "UPDATE coordination_jobs SET claim_version = claim_version + 1 WHERE job_id = %s",
            ("job-test",),
        )


@pytest.mark.parametrize("database", ["postgresql"], indirect=True)
def test_lost_commit_acknowledgment_queries_current_claim_without_second_write(
    claim_database: ClaimDatabase,
) -> None:
    assert claim_database.pool is not None
    pool = claim_database.pool

    class LostAckPool:
        calls = 0

        @contextmanager
        def connection(self, *, timeout: float) -> Iterator[Any]:
            self.calls += 1
            with pool.connection(timeout=timeout) as connection:
                yield connection
            raise psycopg.OperationalError("synthetic lost acknowledgment after real commit")

    lost = LostAckPool()
    result = PostgresClaimTransactions(cast(SpeechPool, lost)).claim_job(REQUEST)
    assert result.outcome == "unknown" and result.recovery == "query_current_authority"
    assert lost.calls == 1
    recovered = claim_database.port.query_claim("job-test")
    assert recovered.fact is not None and recovered.fact.claim_version == 1
    assert recovered.fact.worker_id == "worker-test"
    assert claim_database.foundation.database.counts() == (1, 1)
