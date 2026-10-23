"""Test-only isolated InnoDB adapter for the production Job SQL/domain rules.

This supplies DBAPI transaction/error plumbing only. Production uses PG; there
is no product MySQL driver or generic database-plugin selection mechanism.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, cast
from urllib.parse import unquote, urlparse

import pymysql
from pymysql.cursors import DictCursor

from speechnexus_backend.speech.domain.jobs import CreateJob, JobResult, unknown_result
from speechnexus_backend.speech.infrastructure.jobs_sql import (
    JobCursor,
    create_in_transaction,
    recover_in_transaction,
)


@dataclass(frozen=True)
class MySQLJobTransactions:
    url: str = field(repr=False)
    schema: str
    user: str | None = None
    password: str | None = field(default=None, repr=False)

    def connect(self) -> pymysql.Connection[DictCursor]:
        parsed = urlparse(self.url)
        connection = pymysql.connect(
            host=parsed.hostname or "127.0.0.1",
            port=parsed.port or 3306,
            user=self.user or unquote(parsed.username or ""),
            password=self.password if self.password is not None else unquote(parsed.password or ""),
            database=self.schema,
            charset="utf8mb4",
            cursorclass=DictCursor,
            connect_timeout=3,
            read_timeout=5,
            write_timeout=5,
            autocommit=False,
        )
        with connection.cursor() as cursor:
            cursor.execute("SET time_zone = '+00:00'")
            cursor.execute("SET SESSION TRANSACTION ISOLATION LEVEL READ COMMITTED")
            cursor.execute("SET SESSION innodb_lock_wait_timeout = 3")
        return connection

    @contextmanager
    def transaction(self) -> Iterator[Any]:
        connection = self.connect()
        try:
            with connection.cursor() as cursor:
                yield cursor
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def create_job(self, request: CreateJob) -> JobResult:
        for attempt in range(3):
            try:
                with self.transaction() as cursor:
                    return create_in_transaction(cast(JobCursor, cursor), request)
            except pymysql.IntegrityError as error:
                if error.args[0] == 1062:
                    return self._recover(request, conflict_if_absent=True)
                return JobResult(
                    "rejected",
                    request.job_id,
                    error_code="reconciliation_required",
                    recovery="reconcile",
                )
            except pymysql.OperationalError as error:
                if error.args[0] == 1213 and attempt < 2:
                    continue  # InnoDB has rolled the whole deadlocked transaction back.
                if error.args[0] == 3819:
                    return JobResult(
                        "rejected",
                        request.job_id,
                        error_code="reconciliation_required",
                        recovery="reconcile",
                    )
                return unknown_result(request)
        return unknown_result(request)

    def _recover(self, request: CreateJob, *, conflict_if_absent: bool = False) -> JobResult:
        try:
            with self.transaction() as cursor:
                cursor.execute("SET TRANSACTION READ ONLY")
                return recover_in_transaction(
                    cast(JobCursor, cursor), request, conflict_if_absent=conflict_if_absent
                )
        except pymysql.Error:
            return unknown_result(request)

    def recover_create_job(self, request: CreateJob) -> JobResult:
        return self._recover(request)
