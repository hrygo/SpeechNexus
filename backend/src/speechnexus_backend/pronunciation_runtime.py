"""Independent fixed-revision reader; verifies existing authority without granting it."""

from __future__ import annotations

import logging
import threading
import uuid

from speechnexus_backend.settings import Settings
from speechnexus_backend.speech.application.pronunciation_drafts import PronunciationReader
from speechnexus_backend.speech.infrastructure.postgres_pool import (
    SpeechConnection,
    SpeechPool,
    speech_pool,
)
from speechnexus_backend.speech.infrastructure.pronunciation_drafts_postgres import (
    PostgresPronunciationDrafts,
)


class _PoolDiagnostics(logging.Filter):
    def __init__(self, pool_name: str) -> None:
        super().__init__()
        self._pool_name = pool_name

    def filter(self, record: logging.LogRecord) -> bool:
        if self._pool_name in record.getMessage():
            record.msg, record.args = "Pronunciation database connection unavailable", ()
            record.exc_info = record.exc_text = record.stack_info = None
        return True


class PronunciationRuntime:
    def __init__(self, settings: Settings) -> None:
        if settings.pronunciation_reader_database_url is None or settings.process_role != "api":
            raise ValueError("Configure an explicit API pronunciation reader identity")
        self._settings = settings
        self._pool: SpeechPool | None = None
        self.provider: PronunciationReader | None = None
        self._lifetime = threading.Lock()
        self._diagnostics: _PoolDiagnostics | None = None

    def open(self) -> None:
        with self._lifetime:
            if self.provider is not None:
                raise ValueError("Pronunciation reader is already open")
            secret = self._settings.pronunciation_reader_database_url
            assert secret is not None
            name = "speech-pronunciation-" + uuid.uuid4().hex
            diagnostic = _PoolDiagnostics(name)
            logger = logging.getLogger("psycopg.pool")
            logger.addFilter(diagnostic)
            self._diagnostics = diagnostic
            self._pool = None
            try:
                pool = speech_pool(
                    secret.get_secret_value(),
                    schema=self._settings.database_schema,
                    max_size=self._settings.pronunciation_read_concurrency,
                    name=name,
                )
                self._pool = pool
                pool.open()
                with pool.connection(timeout=3) as connection:
                    connection.execute("SET LOCAL statement_timeout='3s'")
                    identities = connection.execute(
                        "SELECT SESSION_USER AS login, CURRENT_USER AS effective"
                    ).fetchone()
                    if identities is None:
                        raise ValueError("Pronunciation reader identity unavailable")
                    for role in dict.fromkeys((identities["login"], identities["effective"])):
                        self._verify_role(connection, role)
                self.provider = PostgresPronunciationDrafts(
                    pool, timeout_seconds=self._settings.pronunciation_read_timeout_seconds
                )
            except BaseException as error:
                try:
                    if self._pool is not None:
                        self._pool.close()
                finally:
                    logger.removeFilter(diagnostic)
                    self._diagnostics = None
                if not isinstance(error, Exception):
                    raise
                raise ValueError(
                    "Pronunciation reader startup rejected; verify migration and minimum role"
                ) from None

    def _verify_role(self, connection: SpeechConnection, role: str) -> None:
        schema = self._settings.database_schema
        forbidden = connection.execute(
            "SELECT r.rolsuper OR r.rolcreaterole OR r.rolcreatedb OR r.rolreplication "
            "OR r.rolbypassrls OR pg_catalog.has_schema_privilege(%s::name,%s,'CREATE') "
            "OR pg_catalog.pg_has_role(%s::name,n.nspowner,'MEMBER') "
            "OR pg_catalog.pg_has_role(%s::name,d.datdba,'MEMBER') OR EXISTS ("
            "SELECT 1 FROM pg_catalog.pg_class c WHERE c.relnamespace=n.oid "
            "AND pg_catalog.pg_has_role(%s::name,c.relowner,'MEMBER')) AS forbidden "
            "FROM pg_catalog.pg_roles r CROSS JOIN pg_catalog.pg_database d "
            "CROSS JOIN pg_catalog.pg_namespace n WHERE r.rolname=%s "
            "AND n.nspname=%s AND d.datname=CURRENT_DATABASE()",
            (role, schema, role, role, role, role, schema),
        ).fetchone()
        if forbidden is None or forbidden["forbidden"]:
            raise ValueError("Pronunciation reader requires a non-owner identity")
        extra = connection.execute(
            "SELECT EXISTS (SELECT 1 FROM pg_catalog.pg_class c "
            "JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace "
            "WHERE n.nspname=%s AND c.relkind IN ('r','p','v','m','f') AND ("
            "(c.relname<>'speech_pronunciation_revisions' AND ("
            "pg_catalog.has_table_privilege(%s::name,c.oid,'SELECT') OR "
            "pg_catalog.has_any_column_privilege(%s::name,c.oid,'SELECT'))) OR "
            "pg_catalog.has_table_privilege(%s::name,c.oid,"
            "'INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER') OR "
            "pg_catalog.has_any_column_privilege(%s::name,c.oid,"
            "'INSERT,UPDATE,REFERENCES'))) AS forbidden",
            (schema, role, role, role, role),
        ).fetchone()
        permitted = connection.execute(
            "SELECT pg_catalog.has_table_privilege(%s::name,%s,'SELECT') AS permitted",
            (role, f'"{schema}".speech_pronunciation_revisions'),
        ).fetchone()
        if extra is None or extra["forbidden"] or permitted is None or not permitted["permitted"]:
            raise ValueError("Pronunciation reader requires only revision SELECT")

    def close(self) -> None:
        with self._lifetime:
            self.provider = None
            try:
                if self._pool is not None:
                    self._pool.close()
            finally:
                if self._diagnostics is not None:
                    logging.getLogger("psycopg.pool").removeFilter(self._diagnostics)
                    self._diagnostics = None
