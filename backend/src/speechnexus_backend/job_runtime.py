"""Owned public Job writer lifetime; startup verifies but never provisions grants."""

from __future__ import annotations

import logging
import threading
import uuid

from speechnexus_backend.settings import Settings
from speechnexus_backend.speech.application.public_jobs import PublicJobs
from speechnexus_backend.speech.infrastructure.postgres_pool import (
    SpeechConnection,
    SpeechPool,
    speech_pool,
)
from speechnexus_backend.speech.infrastructure.public_jobs_postgres import PostgresPublicJobs

REQUIRED_SELECT = (
    "speech_workspaces",
    "speech_publications",
    "speech_binding_revisions",
    "speech_assets",
    "speech_pronunciation_revisions",
    "speech_jobs",
    "speech_job_events",
    "speech_job_submissions",
)
REQUIRED_INSERT = ("speech_jobs", "speech_job_events", "speech_job_submissions")
REQUIRED_UPDATE_COLUMNS = {
    "speech_jobs": ("state", "status_version", "result_json", "error_code", "updated_at_us"),
}


class _PoolDiagnostics(logging.Filter):
    def __init__(self, pool_name: str) -> None:
        super().__init__()
        self._pool_name = pool_name

    def filter(self, record: logging.LogRecord) -> bool:
        if self._pool_name in record.getMessage():
            record.msg, record.args = "Public Job connection unavailable", ()
            record.exc_info = record.exc_text = record.stack_info = None
        return True


class JobRuntime:
    def __init__(self, settings: Settings) -> None:
        if settings.job_writer_database_url is None or settings.process_role != "api":
            raise ValueError("Configure an explicit API public Job writer identity")
        self._settings = settings
        self._pool: SpeechPool | None = None
        self.jobs: PublicJobs | None = None
        self._lifetime = threading.Lock()
        self._diagnostics: _PoolDiagnostics | None = None

    def open(self) -> None:
        with self._lifetime:
            if self.jobs is not None:
                raise ValueError("Public Job writer is already open")
            secret = self._settings.job_writer_database_url
            assert secret is not None
            name = "speech-jobs-" + uuid.uuid4().hex
            diagnostic = _PoolDiagnostics(name)
            logger = logging.getLogger("psycopg.pool")
            logger.addFilter(diagnostic)
            self._diagnostics = diagnostic
            try:
                pool = speech_pool(
                    secret.get_secret_value(),
                    schema=self._settings.database_schema,
                    max_size=4,
                    name=name,
                )
                self._pool = pool
                pool.open()
                with pool.connection(timeout=3) as connection:
                    connection.execute("SET LOCAL statement_timeout='3s'")
                    _verify_privileges(connection, self._settings.database_schema)
                adapter = PostgresPublicJobs(
                    pool, timeout_seconds=self._settings.job_transaction_timeout_seconds
                )
                self.jobs = PublicJobs(adapter, adapter)
            except BaseException as error:
                try:
                    if self._pool is not None:
                        self._pool.close()
                        self._pool = None
                finally:
                    logger.removeFilter(diagnostic)
                    self._diagnostics = None
                if not isinstance(error, Exception):
                    raise
                raise ValueError(
                    "Public Job writer startup rejected; verify migration and writer role"
                ) from None

    def close(self) -> None:
        with self._lifetime:
            self.jobs = None
            if self._pool is not None:
                self._pool.close()
                self._pool = None
            if self._diagnostics is not None:
                logging.getLogger("psycopg.pool").removeFilter(self._diagnostics)
                self._diagnostics = None


def _verify_privileges(connection: SpeechConnection, schema: str) -> None:
    rows = connection.execute(
        "SELECT c.relname AS name, c.relkind AS kind, "
        "pg_catalog.has_table_privilege(CURRENT_USER, c.oid, 'SELECT') AS can_select, "
        "pg_catalog.has_table_privilege(CURRENT_USER, c.oid, 'INSERT') AS can_insert, "
        "pg_catalog.has_table_privilege(CURRENT_USER, c.oid, 'DELETE,TRUNCATE') AS can_delete "
        "FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = %s AND c.relkind IN ('r', 'p', 'v', 'm', 'f')",
        (schema,),
    ).fetchall()
    privileges = {row["name"]: row for row in rows}
    for table in REQUIRED_SELECT:
        row = privileges.get(table)
        if row is None or not row["can_select"]:
            raise ValueError("Public Job writer lacks SELECT on " + table)
    for table in REQUIRED_INSERT:
        row = privileges.get(table)
        if row is None or not row["can_insert"]:
            raise ValueError("Public Job writer lacks INSERT on " + table)
    for table, columns in REQUIRED_UPDATE_COLUMNS.items():
        for column in columns:
            allowed = connection.execute(
                "SELECT pg_catalog.has_column_privilege(CURRENT_USER, %s, %s, 'UPDATE') AS ok",
                (f"{schema}.{table}", column),
            ).fetchone()
            if allowed is None or not allowed["ok"]:
                raise ValueError(f"Public Job writer lacks UPDATE on {table}.{column}")
    for table in REQUIRED_INSERT:
        row = privileges.get(table)
        if row is not None and row["can_delete"]:
            raise ValueError("Public Job writer must not delete Job history")
