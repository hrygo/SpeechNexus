"""Production Job/outbox behavior, real PG adapter and shared InnoDB SQL oracle."""

from __future__ import annotations

import os
import subprocess
import sys
import time
import uuid
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, cast
from urllib.parse import unquote, urlparse

import psycopg
import pymysql
import pytest
from alembic import command
from alembic.config import Config
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool
from pymysql.cursors import DictCursor
from sqlalchemy import create_engine
from sqlalchemy.pool import NullPool
from tests.support.job_database import MySQLJobTransactions

from speechnexus_backend.speech.application.jobs import CreateJobService, JobTransactions
from speechnexus_backend.speech.domain.jobs import CreateJob
from speechnexus_backend.speech.infrastructure.job_schema import JOB_DDL, job_runtime_grants
from speechnexus_backend.speech.infrastructure.jobs_postgres import (
    JobPool,
    PostgresJobTransactions,
    job_pool,
)
from speechnexus_backend.speech.infrastructure.jobs_sql import JobCursor, create_in_transaction
from speechnexus_backend.speech.interfaces.job_transactions import JobTransactionBoundary

pytestmark = pytest.mark.integration
ROOT = Path(__file__).resolve().parents[2]
DIGEST = "sha256:" + "a" * 64
REQUEST = CreateJob("job-test", "operation-test", "attempt-test", "event-test", DIGEST, DIGEST)
VARIABLES = ("SPEECHNEXUS_TEST_DATABASE_URL", "SPEECHNEXUS_TEST_MYSQL_URL")


@dataclass
class JobDatabase:
    dialect: str
    url: str = field(repr=False)
    schema: str
    role: str
    admin: Any = field(repr=False)
    port: JobTransactions = field(repr=False)
    pool: JobPool | None = field(repr=False)

    def query(self, statement: str, parameters: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        with self.admin.cursor() as cursor:
            cursor.execute(statement, parameters or None)
            return cast(list[dict[str, Any]], list(cursor.fetchall())) if cursor.description else []

    def counts(self) -> tuple[int, int]:
        return (
            self.query("SELECT count(*) AS n FROM coordination_jobs")[0]["n"],
            self.query("SELECT count(*) AS n FROM coordination_outbox")[0]["n"],
        )

    def blocker(self) -> Any:
        if self.dialect == "postgresql":
            connection = psycopg.connect(self.url, row_factory=dict_row)
            connection.execute(f'SET search_path TO "{self.schema}", pg_catalog')
            connection.commit()
            return connection
        return MySQLJobTransactions(self.url, self.schema).connect()

    def await_waiter(self) -> None:
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            if self.dialect == "postgresql":
                rows = self.query(
                    "SELECT count(*) AS n FROM pg_stat_activity "
                    "WHERE usename = %s AND cardinality(pg_blocking_pids(pid)) > 0",
                    (self.role,),
                )
            else:
                rows = self.query(
                    "SELECT count(*) AS n FROM performance_schema.data_lock_waits w "
                    "JOIN performance_schema.data_locks l "
                    "ON w.REQUESTING_ENGINE_LOCK_ID = l.ENGINE_LOCK_ID "
                    "WHERE l.OBJECT_SCHEMA = %s",
                    (self.schema,),
                )
            if rows[0]["n"]:
                return
            time.sleep(0.01)
        pytest.fail("No real unique-key lock wait was observed; concurrency is not qualified")


@pytest.fixture(params=["postgresql", "mysql"])
def database(request: pytest.FixtureRequest) -> Iterator[JobDatabase]:
    configured = [os.environ.get(variable) for variable in VARIABLES]
    if any(configured) and not all(configured):
        pytest.fail("Production Job behavior requires both isolated PG and InnoDB DSNs")
    if not all(configured):
        pytest.skip("Configure both isolated database DSNs; skip is not qualification evidence")
    dialect = request.param
    url = cast(str, configured[0 if dialect == "postgresql" else 1])
    suffix = uuid.uuid4().hex[:12]
    schema, role = "sn_jobs_" + suffix, "sn_jobs_role_" + suffix
    pool: JobPool | None = None
    if dialect == "postgresql":
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
        admin: Any = psycopg.connect(url, row_factory=dict_row, autocommit=True)
        admin.execute(f'SET search_path TO "{schema}", pg_catalog')
        admin.execute(f"CREATE ROLE \"{role}\" LOGIN PASSWORD 'synthetic_job_test'")
        for grant in job_runtime_grants(schema, role):
            admin.execute(grant)
        pool = job_pool(make_conninfo(url, user=role, password="synthetic_job_test"), schema=schema)
        pool.open()
        port: JobTransactions = PostgresJobTransactions(pool)
    else:
        parsed = urlparse(url)
        admin = pymysql.connect(
            host=parsed.hostname or "127.0.0.1",
            port=parsed.port or 3306,
            user=unquote(parsed.username or ""),
            password=unquote(parsed.password or ""),
            charset="utf8mb4",
            cursorclass=DictCursor,
            autocommit=True,
        )
        with admin.cursor() as cursor:
            cursor.execute(
                f"CREATE DATABASE `{schema}` CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_bin"
            )
            cursor.execute(f"USE `{schema}`")
            for ddl in JOB_DDL:
                cursor.execute(ddl.replace('COLLATE "C"', "COLLATE utf8mb4_0900_bin"))
            cursor.execute(f"CREATE USER '{role}'@'%' IDENTIFIED BY 'synthetic_job_test'")
            for table, columns in (
                (
                    "coordination_jobs",
                    "job_id,operation_id,attempt_id,initial_event_id,"
                    "initial_event_digest,request_digest",
                ),
                ("coordination_outbox", "event_id,job_id,event_digest"),
            ):
                cursor.execute(
                    f"GRANT SELECT, INSERT ({columns}) ON `{schema}`.{table} TO '{role}'@'%'"
                )
        port = MySQLJobTransactions(url, schema, role, "synthetic_job_test")
    database = JobDatabase(dialect, url, schema, role, admin, port, pool)
    try:
        yield database
    finally:
        if pool is not None:
            pool.close()
        with admin.cursor() as cursor:
            if dialect == "postgresql":
                cursor.execute(f'DROP SCHEMA "{schema}" CASCADE')
                cursor.execute(f'DROP ROLE "{role}"')
            else:
                cursor.execute(f"DROP DATABASE `{schema}`")
                cursor.execute(f"DROP USER '{role}'@'%'")
        admin.close()


def test_creation_replay_and_read_only_recovery(database: JobDatabase) -> None:
    result = database.port.create_job(REQUEST)
    assert result.outcome == "committed" and result.fact is not None
    assert database.counts() == (1, 1)
    assert database.port.create_job(REQUEST).outcome == "replayed"
    assert database.port.recover_create_job(REQUEST).outcome == "replayed"
    assert database.counts() == (1, 1)
    version = database.query("SELECT claim_version, claim_owner_id FROM coordination_jobs")[0]
    assert version == {"claim_version": 0, "claim_owner_id": None}


@pytest.mark.parametrize(
    "field,value",
    [
        ("request_digest", "sha256:" + "b" * 64),
        ("event_digest", "sha256:" + "b" * 64),
        ("event_id", "event-other"),
        ("operation_id", "operation-other"),
        ("attempt_id", "attempt-other"),
        ("job_id", "job-other"),
    ],
)
def test_full_immutable_identity_conflicts(database: JobDatabase, field: str, value: str) -> None:
    database.port.create_job(REQUEST)
    changed = replace(REQUEST, **{field: value})
    assert database.port.create_job(changed).outcome == "conflict"
    assert database.port.recover_create_job(changed).outcome == "conflict"
    assert database.counts() == (1, 1)


def test_unknown_absence_never_creates_or_dispatches(database: JobDatabase) -> None:
    result = database.port.recover_create_job(REQUEST)
    assert result.outcome == "unknown" and result.recovery == "query_original"
    assert database.counts() == (0, 0)


def test_job_id_and_business_key_cannot_alias_two_different_records(database: JobDatabase) -> None:
    database.port.create_job(REQUEST)
    other = replace(
        REQUEST,
        job_id="job-other",
        operation_id="operation-other",
        attempt_id="attempt-other",
        event_id="event-other",
    )
    assert database.port.create_job(other).outcome == "committed"
    aliased = replace(other, job_id=REQUEST.job_id)
    assert database.port.create_job(aliased).outcome == "conflict"
    assert database.port.recover_create_job(aliased).outcome == "conflict"
    assert database.counts() == (2, 2)


def test_initial_event_identity_conflict_rejects_the_new_job(database: JobDatabase) -> None:
    database.port.create_job(REQUEST)
    other = replace(REQUEST, job_id="job-other", operation_id="operation-other")
    assert database.port.create_job(other).outcome == "conflict"
    assert database.counts() == (1, 1)


def test_outbox_key_collision_rolls_back_the_already_inserted_job(database: JobDatabase) -> None:
    database.port.create_job(REQUEST)
    database.query(
        "INSERT INTO coordination_outbox (event_id,job_id,event_digest) VALUES (%s,%s,%s)",
        ("event-progress", REQUEST.job_id, DIGEST),
    )
    other = replace(
        REQUEST, job_id="job-other", operation_id="operation-other", event_id="event-progress"
    )
    assert database.port.create_job(other).outcome == "conflict"
    assert database.counts() == (1, 2)


def test_insert_failure_rolls_back_both_facts(database: JobDatabase) -> None:
    database.query(
        "ALTER TABLE coordination_outbox ADD CONSTRAINT injected_failure "
        "CHECK (event_digest <> '" + DIGEST + "')"
    )
    result = database.port.create_job(REQUEST)
    assert result.outcome == "rejected"
    assert database.counts() == (0, 0)


def test_missing_initial_outbox_is_reconciled_without_repair(database: JobDatabase) -> None:
    database.port.create_job(REQUEST)
    database.query("DELETE FROM coordination_outbox")  # Corruption by the fixture owner only.
    result = database.port.recover_create_job(REQUEST)
    assert result.outcome == "rejected" and result.recovery == "reconcile"
    assert database.counts() == (1, 0)


@pytest.mark.parametrize("different", [False, True])
def test_real_insertion_wait_resolves_one_committed_identity(
    database: JobDatabase, different: bool
) -> None:
    blocker = database.blocker()
    try:
        with blocker.cursor() as cursor:
            assert create_in_transaction(cast(JobCursor, cursor), REQUEST).outcome == "committed"
        with ThreadPoolExecutor(max_workers=1) as executor:
            waiting = (
                replace(REQUEST, request_digest="sha256:" + "b" * 64) if different else REQUEST
            )
            future = executor.submit(database.port.create_job, waiting)
            try:
                database.await_waiter()
                # An uncommitted fact remains unknown to the read-only recovery path.
                assert database.port.recover_create_job(REQUEST).outcome == "unknown"
            finally:
                blocker.commit()
            assert future.result(timeout=5).outcome == ("conflict" if different else "replayed")
        assert database.counts() == (1, 1)
    finally:
        blocker.close()


def test_boundary_returns_the_complete_contract_snapshot(database: JobDatabase) -> None:
    boundary = JobTransactionBoundary(CreateJobService(database.port))
    wire = {
        "job": {
            "job_id": REQUEST.job_id,
            "event_id": REQUEST.event_id,
            "event_digest": REQUEST.event_digest,
        },
        "identity": {"operation_id": REQUEST.operation_id, "attempt_id": REQUEST.attempt_id},
        "request_digest": REQUEST.request_digest,
    }
    result = boundary.create_job(wire)
    assert result["value"] == {
        "kind": "fact",
        "fact_id": REQUEST.job_id,
        "request_digest": REQUEST.request_digest,
    }
    assert boundary.recover_create_job(wire)["outcome"] == "replayed"


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE coordination_jobs SET request_digest = 'sha256:" + "b" * 64 + "'",
        "UPDATE coordination_jobs SET claim_version = 1",
        "UPDATE coordination_outbox SET delivered_at_us = 1",
        "DELETE FROM coordination_jobs",
        "DELETE FROM coordination_outbox",
        "CREATE TABLE forbidden_table (id INTEGER)",
        "INSERT INTO coordination_jobs (job_id,operation_id,attempt_id,initial_event_id,"
        "initial_event_digest,request_digest,claim_version) VALUES "
        "('job-extra','operation-extra','attempt-extra','event-extra',"
        "'" + DIGEST + "','" + DIGEST + "',10)",
    ],
)
def test_fresh_runtime_role_cannot_mutate_evidence_or_run_ddl(
    database: JobDatabase, statement: str
) -> None:
    database.port.create_job(REQUEST)
    if database.pool is not None:
        with database.pool.connection() as connection:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                connection.execute(statement)
            connection.rollback()
    else:
        runtime = MySQLJobTransactions(
            database.url, database.schema, database.role, "synthetic_job_test"
        )
        with pytest.raises(pymysql.OperationalError) as caught, runtime.transaction() as cursor:
            cursor.execute(statement)
        assert caught.value.args[0] in (1044, 1142, 1143)
    assert database.counts() == (1, 1)


class FaultConnection(psycopg.Connection[dict[str, Any]]):
    """Test-only real server termination / simulated lost committed reply."""

    fault: str | None = None
    admin_url: str = ""

    def commit(self) -> None:
        fault, self.fault = self.fault, None
        if fault == "before":
            with psycopg.connect(self.admin_url, autocommit=True) as killer:
                killer.execute("SELECT pg_terminate_backend(%s)", (self.info.backend_pid,))
        super().commit()
        if fault == "after":
            raise psycopg.OperationalError("Synthetic lost commit acknowledgment")


@pytest.mark.parametrize("database", ["postgresql"], indirect=True)
@pytest.mark.parametrize("fault,counts", [("before", (0, 0)), ("after", (1, 1))])
def test_pg_commit_uncertainty_requires_read_only_recovery(
    database: JobDatabase, fault: str, counts: tuple[int, int]
) -> None:
    def configure(connection: FaultConnection) -> None:
        connection.execute(f'SET search_path TO "{database.schema}", pg_catalog')
        connection.commit()

    with ConnectionPool[FaultConnection](
        make_conninfo(database.url, user=database.role, password="synthetic_job_test"),
        connection_class=FaultConnection,
        kwargs={"row_factory": dict_row},
        configure=configure,
        min_size=1,
        max_size=1,
        timeout=3,
    ) as pool:
        with pool.connection() as connection:
            armed = connection
        armed.admin_url, armed.fault = database.url, fault
        transactions = PostgresJobTransactions(cast(JobPool, pool))
        result = transactions.create_job(REQUEST)
        assert result.outcome == "unknown" and result.recovery == "query_original"
        assert database.counts() == counts
        recovered = transactions.recover_create_job(REQUEST)
        assert recovered.outcome == ("unknown" if fault == "before" else "replayed")
        assert database.counts() == counts, "Recovery may not insert the missing Job/outbox"


@pytest.mark.parametrize("database", ["postgresql"], indirect=True)
def test_versioned_migration_cli_is_idempotent_and_downgrade_preserves_history(
    database: JobDatabase,
) -> None:
    database.port.create_job(REQUEST)
    subprocess.run(
        [
            sys.executable,
            "-m",
            "speechnexus_backend.speech.infrastructure.migrate_jobs",
            "--schema",
            database.schema,
        ],
        env={**os.environ, "SPEECHNEXUS_MIGRATION_DATABASE_URL": database.url},
        check=True,
        capture_output=True,
    )
    engine = create_engine(
        database.url.replace("postgresql://", "postgresql+psycopg://", 1),
        poolclass=NullPool,
        hide_parameters=True,
    )
    config = Config(str(ROOT / "alembic.ini"))
    config.attributes["schema"] = database.schema
    try:
        with engine.connect() as connection:
            config.attributes["connection"] = connection
            with pytest.raises(RuntimeError, match="Destructive runtime intent downgrade"):
                command.downgrade(config, "base")
    finally:
        engine.dispose()
    assert database.query("SELECT version_num FROM alembic_version") == [
        {"version_num": "0005_runtime_intents"}
    ]
    assert database.counts() == (1, 1)
