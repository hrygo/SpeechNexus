"""Real commit loss then native read pressure retains original admission facts."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any, cast

import psycopg
import pymysql
import pytest
from psycopg.conninfo import make_conninfo
from tests.integration.test_production_admission import AdmissionDatabase
from tests.integration.test_production_admission import admission_database as admission_database
from tests.integration.test_production_admission import database as database
from tests.integration.test_production_admission import foundation as foundation
from tests.integration.test_production_admission import runtime_database as runtime_database
from tests.support import admission_database as mysql_adapter
from tests.support.admission_database import MySQLAdmissionTransactions
from tests.support.job_database import MySQLJobTransactions

from speechnexus_backend.speech.application.admission import AdmissionTransactions
from speechnexus_backend.speech.domain.admission import unknown
from speechnexus_backend.speech.infrastructure import admission_postgres as pg_adapter
from speechnexus_backend.speech.infrastructure.admission_postgres import (
    PostgresAdmissionTransactions,
)
from speechnexus_backend.speech.infrastructure.admission_sql import admit_in_transaction
from speechnexus_backend.speech.infrastructure.postgres_pool import SpeechPool, speech_pool

pytestmark = pytest.mark.integration


def test_committed_admission_lost_reply_then_native_query_pressure_only_recovers_original(
    admission_database: AdmissionDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    admission = admission_database
    db = admission.runtime.foundation.database
    writes: list[str] = []
    original_admit = admit_in_transaction

    def admit(*args: Any, **kwargs: Any) -> Any:
        writes.append("admit")
        return original_admit(*args, **kwargs)

    monkeypatch.setattr(pg_adapter, "admit_in_transaction", admit)
    monkeypatch.setattr(mysql_adapter, "admit_in_transaction", admit)
    pool: SpeechPool | None = None
    native_mysql_errors: list[int] = []
    lost: list[str] = []
    if db.dialect == "postgresql":
        pool = speech_pool(
            make_conninfo(
                db.url,
                user=admission.runtime.foundation.speech_role,
                password="synthetic_foundation_test",
            ),
            schema=db.schema,
            max_size=1,
        )
        pool.open()
        base_pool = pool

        class CommitLostPool:
            @contextmanager
            def connection(self, *, timeout: float) -> Iterator[Any]:
                with base_pool.connection(timeout=timeout) as connection:
                    yield connection
                if not lost:
                    lost.append("committed")
                    raise psycopg.OperationalError("Synthetic lost reply after actual commit")

        wrapper = cast(SpeechPool, CommitLostPool())
        writer: AdmissionTransactions = PostgresAdmissionTransactions(
            wrapper, max_command_bytes=1048576
        )
        reader: AdmissionTransactions = PostgresAdmissionTransactions(
            wrapper, max_command_bytes=1048576, timeout_seconds=0.5
        )
    else:
        assert isinstance(admission.port, MySQLAdmissionTransactions)
        driver = admission.port.driver

        class CommitLostConnection:
            def __init__(self, connection: Any) -> None:
                self.connection = connection

            @contextmanager
            def cursor(self) -> Iterator[Any]:
                with self.connection.cursor() as cursor:
                    try:
                        yield cursor
                    except pymysql.OperationalError as error:
                        native_mysql_errors.append(error.args[0])
                        raise

            def commit(self) -> None:
                self.connection.commit()
                if not lost:
                    lost.append("committed")
                    raise pymysql.OperationalError(2013, "Synthetic lost reply after actual commit")

            def rollback(self) -> None:
                self.connection.rollback()

            def close(self) -> None:
                self.connection.close()

        class CommitLostDriver:
            def connect(self) -> Any:
                connection = driver.connect()
                with connection.cursor() as cursor:
                    cursor.execute("SET SESSION lock_wait_timeout=1")
                return CommitLostConnection(connection)

        writer = reader = MySQLAdmissionTransactions(
            cast(MySQLJobTransactions, CommitLostDriver()), max_command_bytes=1048576
        )
    try:
        result = writer.admit_recipe(admission.command)
        assert result == unknown(admission.command.request.identity)
        assert lost == ["committed"] and writes == ["admit"]
        committed = admission.snapshot()
        assert committed["admission_operations"] and committed["admission_leases"]
        if pool is not None:
            # Occupy the sole real pool slot: the original query cannot read
            # anything and genuinely times out waiting for its connection.
            with pool.connection():
                pressured = reader.query_admission(admission.command.request.identity)
            assert pool.get_stats()["requests_errors"] == 1
        else:
            # Native MySQL metadata lock timeout on the first original read;
            # this is not a simulated query result or a product MySQL pool.
            blocker = db.blocker()
            try:
                with blocker.cursor() as cursor:
                    cursor.execute("LOCK TABLES admission_attempts WRITE")
                pressured = reader.query_admission(admission.command.request.identity)
            finally:
                with blocker.cursor() as cursor:
                    cursor.execute("UNLOCK TABLES")
                blocker.close()
            assert native_mysql_errors == [1205]
        assert pressured == unknown(admission.command.request.identity)
        assert writes == ["admit"] and admission.snapshot() == committed
        recovered = reader.query_admission(admission.command.request.identity)
        assert recovered.outcome == "replayed" and recovered.fact is not None
        assert recovered.fact.identity == admission.command.request.identity
        assert writes == ["admit"] and lost == ["committed"]
        assert admission.snapshot() == committed
    finally:
        if pool is not None:
            pool.close()
