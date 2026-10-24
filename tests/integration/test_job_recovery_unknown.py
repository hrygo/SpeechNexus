"""A committed original remains unknown when its read-only recovery cannot complete."""

from __future__ import annotations

import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from typing import Any, cast

import pymysql
import pytest
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool
from tests.integration.test_production_jobs import REQUEST, FaultConnection, JobDatabase
from tests.integration.test_production_jobs import database as database
from tests.support.job_database import MySQLJobTransactions

from speechnexus_backend.speech.application.jobs import CreateJobService, JobTransactions
from speechnexus_backend.speech.infrastructure.jobs_postgres import (
    JobPool,
    PostgresJobTransactions,
    job_pool,
)
from speechnexus_backend.speech.interfaces.job_transactions import JobTransactionBoundary

pytestmark = pytest.mark.integration
WIRE = {
    "job": {
        "job_id": REQUEST.job_id,
        "event_id": REQUEST.event_id,
        "event_digest": REQUEST.event_digest,
    },
    "identity": {"operation_id": REQUEST.operation_id, "attempt_id": REQUEST.attempt_id},
    "request_digest": REQUEST.request_digest,
}


def snapshot(db: JobDatabase) -> dict[str, Any]:
    return {
        "jobs": db.query("SELECT * FROM coordination_jobs ORDER BY job_id"),
        "events": db.query("SELECT * FROM coordination_outbox ORDER BY event_id"),
    }


def unknown(port: JobTransactions) -> None:
    # Exercise the actual boundary's source validation, not an unvalidated test DTO.
    wire = JobTransactionBoundary(CreateJobService(port)).recover_create_job(WIRE)
    assert wire["outcome"] == "unknown", wire
    assert wire["recovery"] == "query_original" and wire["error_code"] == "outcome_unknown"
    assert wire["scope_id"] == REQUEST.job_id and "value" not in wire


def lose_committed_reply(db: JobDatabase, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    if db.dialect == "postgresql":

        def configure(connection: FaultConnection) -> None:
            connection.execute(f'SET search_path TO "{db.schema}",pg_catalog,pg_temp')
            connection.commit()

        with ConnectionPool[FaultConnection](
            make_conninfo(db.url, user=db.role, password="synthetic_job_test"),
            connection_class=FaultConnection,
            kwargs={"row_factory": dict_row},
            configure=configure,
            min_size=1,
            max_size=1,
        ) as pool:
            with pool.connection() as connection:
                armed = connection
            armed.fault = "after"
            result = PostgresJobTransactions(cast(JobPool, pool)).create_job(REQUEST)
    else:
        original = MySQLJobTransactions.transaction

        @contextmanager
        def lost(self: MySQLJobTransactions) -> Iterator[Any]:
            with original(self) as cursor:
                yield cursor
            # The actual server has committed before the client loses its reply.
            raise pymysql.OperationalError(2013, "Synthetic committed response loss")

        with monkeypatch.context() as scoped:
            scoped.setattr(MySQLJobTransactions, "transaction", lost)
            result = db.port.create_job(REQUEST)
    assert result.outcome == "unknown" and result.recovery == "query_original"
    assert db.counts() == (1, 1)
    return snapshot(db)


def restored(db: JobDatabase, before: dict[str, Any]) -> None:
    if db.dialect == "postgresql":
        with job_pool(
            make_conninfo(db.url, user=db.role, password="synthetic_job_test"), schema=db.schema
        ) as pool:
            result = PostgresJobTransactions(pool).recover_create_job(REQUEST)
    else:
        result = db.port.recover_create_job(REQUEST)
    assert result.outcome == "replayed" and result.fact is not None
    assert (
        result.fact.job_id == REQUEST.job_id
        and result.fact.request_digest == REQUEST.request_digest
    )
    assert snapshot(db) == before and db.counts() == (1, 1)


@pytest.mark.parametrize("database", ["postgresql"], indirect=True)
@pytest.mark.parametrize("failure", ["closed", "pool-timeout", "queue-full", "checkout-deadline"])
def test_original_commit_cannot_be_rejected_by_pg_read_capacity(
    database: JobDatabase,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    db = database
    before = lose_committed_reply(db, monkeypatch)
    if failure == "closed":
        assert db.pool is not None
        db.pool.close()
        unknown(db.port)
    else:
        with job_pool(
            make_conninfo(db.url, user=db.role, password="synthetic_job_test"),
            schema=db.schema,
            max_size=1,
            max_waiting=1,
        ) as pool:
            short = PostgresJobTransactions(pool, timeout_seconds=0.1)
            if failure == "checkout-deadline":

                class SlowCheckout:
                    @contextmanager
                    def connection(self, *, timeout: float) -> Iterator[Any]:
                        with pool.connection(timeout=timeout) as connection:
                            time.sleep(0.15)  # Real checkout, delayed before the first query.
                            yield connection

                unknown(PostgresJobTransactions(cast(JobPool, SlowCheckout()), timeout_seconds=0.1))
            else:
                with pool.connection():
                    if failure == "pool-timeout":
                        unknown(short)
                    else:
                        with ThreadPoolExecutor(max_workers=1) as workers:
                            pending = workers.submit(
                                PostgresJobTransactions(pool, timeout_seconds=1).recover_create_job,
                                REQUEST,
                            )
                            deadline = time.monotonic() + 1
                            while pool.get_stats().get("requests_waiting", 0) != 1:
                                assert time.monotonic() < deadline, "No native pool waiter observed"
                                time.sleep(0.01)
                            unknown(short)
                            delayed = pending.result(timeout=2)
                            assert (
                                delayed.outcome == "unknown"
                                and delayed.recovery == "query_original"
                            )
    restored(db, before)


def wait_for_read(db: JobDatabase) -> int:
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        if db.dialect == "postgresql":
            rows = db.query(
                "SELECT pid FROM pg_stat_activity WHERE usename=%s "
                "AND cardinality(pg_blocking_pids(pid))>0",
                (db.role,),
            )
        else:
            rows = db.query(
                "SELECT ID AS pid FROM information_schema.PROCESSLIST WHERE USER=%s "
                "AND STATE LIKE 'Waiting%%' AND INFO LIKE 'SELECT%%coordination_jobs%%'",
                (db.role,),
            )
        if rows:
            assert len(rows) == 1
            return int(rows[0]["pid"])
        time.sleep(0.01)
    pytest.fail("A native blocked original-result SELECT was not observed")


@pytest.mark.parametrize("failure", ["query-timeout", "query-cancel"])
def test_original_commit_survives_native_read_failure_on_both_engines(
    database: JobDatabase,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    db = database
    before = lose_committed_reply(db, monkeypatch)
    blocker = db.blocker()
    try:
        if db.dialect == "postgresql":
            blocker.execute("LOCK TABLE coordination_jobs IN ACCESS EXCLUSIVE MODE")
            assert db.pool is not None
            port: JobTransactions = PostgresJobTransactions(
                db.pool, timeout_seconds=0.5 if failure == "query-timeout" else 3
            )
        else:
            blocker.cursor().execute("LOCK TABLES coordination_jobs WRITE")
            original_connect = MySQLJobTransactions.connect

            def limited(self: MySQLJobTransactions) -> Any:
                connection = original_connect(self)
                connection.cursor().execute("SET SESSION lock_wait_timeout=1")
                return connection

            monkeypatch.setattr(MySQLJobTransactions, "connect", limited)
            port = db.port
        with ThreadPoolExecutor(max_workers=1) as workers:
            pending = workers.submit(unknown, port)
            pid = wait_for_read(db)
            if failure == "query-cancel":
                if db.dialect == "postgresql":
                    assert db.query("SELECT pg_cancel_backend(%s) AS cancelled", (pid,))[0][
                        "cancelled"
                    ]
                else:
                    db.query(f"KILL QUERY {pid}")
            pending.result(timeout=4)
    finally:
        if db.dialect == "postgresql":
            blocker.rollback()
        else:
            blocker.cursor().execute("UNLOCK TABLES")
        blocker.close()
    restored(db, before)


def test_original_commit_is_not_rejected_when_its_read_role_loses_select(
    database: JobDatabase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = database
    before = lose_committed_reply(db, monkeypatch)
    table = (
        f'"{db.schema}".coordination_jobs'
        if db.dialect == "postgresql"
        else f"`{db.schema}`.coordination_jobs"
    )
    target = f'"{db.role}"' if db.dialect == "postgresql" else f"'{db.role}'@'%'"
    db.query(f"REVOKE SELECT ON {table} FROM {target}")
    try:
        unknown(db.port)
        assert snapshot(db) == before
    finally:
        # Restore only the ephemeral fixture role's original SELECT authority.
        db.query(f"GRANT SELECT ON {table} TO {target}")
    restored(db, before)
