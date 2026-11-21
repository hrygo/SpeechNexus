"""Read-only audit event listing; this identity never records or rewrites history.

The reader may read ``speech_publication_lifecycle_events`` and nothing else. The
lifecycle writer grants SELECT, INSERT on that table; using the writer identity
here would let the audit read path append or replay evidence, so this runtime
opens a dedicated login and verifies the granted surface at startup instead of
trusting the configured DSN name.
"""

from __future__ import annotations

import logging
import threading
import uuid

from speechnexus_backend.settings import Settings
from speechnexus_backend.speech.application.audit_events import AuditEventService
from speechnexus_backend.speech.infrastructure.audit_events_postgres import (
    PostgresAuditEventReads,
)
from speechnexus_backend.speech.infrastructure.postgres_pool import (
    SpeechConnection,
    SpeechPool,
    speech_pool,
)
from speechnexus_backend.speech.infrastructure.publication_lifecycle_schema import EVENT_TABLE
from speechnexus_backend.startup_privileges import (
    refuse_column_privileges,
    refuse_foreign_relation_privileges,
    refuse_schema_create,
    refuse_table_privileges,
    require_schema_usage,
)

SUBJECT = "Audit reader"
READABLE = (EVENT_TABLE,)
WRITE_PRIVILEGES = ("INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER")
COLUMN_WRITE_PRIVILEGES = ("INSERT", "UPDATE", "REFERENCES")


class _PoolDiagnostics(logging.Filter):
    def __init__(self, pool_name: str) -> None:
        super().__init__()
        self._pool_name = pool_name

    def filter(self, record: logging.LogRecord) -> bool:
        if self._pool_name in record.getMessage():
            record.msg, record.args = "Audit database connection unavailable", ()
            record.exc_info = record.exc_text = record.stack_info = None
        return True


class AuditReaderRuntime:
    def __init__(self, settings: Settings) -> None:
        if settings.audit_reader_database_url is None or settings.process_role != "api":
            raise ValueError("Configure an explicit API audit reader identity")
        self._settings = settings
        self._pool: SpeechPool | None = None
        self.service: AuditEventService | None = None
        self._lifetime = threading.Lock()
        self._diagnostics: _PoolDiagnostics | None = None

    def open(self) -> None:
        with self._lifetime:
            if self.service is not None:
                raise ValueError("Audit reader is already open")
            secret = self._settings.audit_reader_database_url
            assert secret is not None
            name = "speech-audit-read-" + uuid.uuid4().hex
            diagnostic = _PoolDiagnostics(name)
            logger = logging.getLogger("psycopg.pool")
            logger.addFilter(diagnostic)
            self._diagnostics = diagnostic
            try:
                pool = speech_pool(
                    secret.get_secret_value(),
                    schema=self._settings.database_schema,
                    max_size=self._settings.audit_read_concurrency,
                    name=name,
                )
                self._pool = pool
                pool.open()
                with pool.connection(timeout=3) as connection:
                    connection.execute("SET LOCAL statement_timeout='3s'")
                    self._verify_read_only(connection)
                self.service = AuditEventService(
                    PostgresAuditEventReads(
                        pool, timeout_seconds=self._settings.audit_read_timeout_seconds
                    )
                )
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
                    "Audit reader startup rejected; verify migration and reader role"
                ) from None

    def _verify_read_only(self, connection: SpeechConnection) -> None:
        schema = self._settings.database_schema
        require_schema_usage(connection, None, schema, subject=SUBJECT)
        refuse_schema_create(connection, None, schema, subject=SUBJECT)
        refuse_foreign_relation_privileges(
            connection,
            None,
            schema,
            READABLE,
            subject=SUBJECT,
            declared_hint="READABLE",
        )
        for table in READABLE:
            qualified = f'"{schema}"."{table}"'
            refuse_table_privileges(
                connection, None, qualified, table, WRITE_PRIVILEGES, subject=SUBJECT
            )
            refuse_column_privileges(
                connection, None, qualified, table, COLUMN_WRITE_PRIVILEGES, subject=SUBJECT
            )
            held = connection.execute(
                "SELECT pg_catalog.has_table_privilege(CURRENT_USER, %s, 'SELECT') AS held",
                (qualified,),
            ).fetchone()
            if held is None or not held["held"]:
                raise ValueError(f"{SUBJECT} lacks SELECT on {table}; grant SELECT on that table")

    def close(self) -> None:
        with self._lifetime:
            self.service = None
            if self._pool is not None:
                self._pool.close()
                self._pool = None
            if self._diagnostics is not None:
                logging.getLogger("psycopg.pool").removeFilter(self._diagnostics)
                self._diagnostics = None
