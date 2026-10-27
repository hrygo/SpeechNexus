"""Read-only model catalogue identity; it may read two tables and nothing else.

The catalogue is public, so its reader must not be able to reach drafts, commands,
coordination history or any write path. Startup verifies the granted surface
instead of trusting configuration.
"""

from __future__ import annotations

import logging
import threading
import uuid
from typing import Any

from speechnexus_backend.settings import Settings
from speechnexus_backend.speech.infrastructure.model_catalog_postgres import PostgresModelCatalog
from speechnexus_backend.speech.infrastructure.postgres_pool import (
    SpeechConnection,
    SpeechPool,
    speech_pool,
)

READABLE = frozenset({"speech_publications", "speech_binding_revisions"})


class _PoolDiagnostics(logging.Filter):
    def __init__(self, pool_name: str) -> None:
        super().__init__()
        self._pool_name = pool_name

    def filter(self, record: logging.LogRecord) -> bool:
        if self._pool_name in record.getMessage():
            record.msg, record.args = "Model catalogue connection unavailable", ()
            record.exc_info = record.exc_text = record.stack_info = None
        return True


class ModelCatalogRuntime:
    def __init__(self, settings: Settings) -> None:
        if settings.model_catalog_reader_database_url is None or settings.process_role != "api":
            raise ValueError("Configure an explicit API model catalogue reader identity")
        self._settings = settings
        self._pool: SpeechPool | None = None
        self.catalogue: PostgresModelCatalog | None = None
        self._lifetime = threading.Lock()
        self._diagnostics: _PoolDiagnostics | None = None

    def open(self) -> None:
        with self._lifetime:
            if self.catalogue is not None:
                raise ValueError("Model catalogue reader is already open")
            secret = self._settings.model_catalog_reader_database_url
            assert secret is not None
            name = "speech-catalogue-" + uuid.uuid4().hex
            diagnostic = _PoolDiagnostics(name)
            logger = logging.getLogger("psycopg.pool")
            logger.addFilter(diagnostic)
            self._diagnostics = diagnostic
            try:
                pool = speech_pool(
                    secret.get_secret_value(),
                    schema=self._settings.database_schema,
                    max_size=self._settings.model_catalog_read_concurrency,
                    name=name,
                )
                self._pool = pool
                pool.open()
                with pool.connection(timeout=3) as connection:
                    connection.execute("SET LOCAL statement_timeout='3s'")
                    self._verify_read_only(connection)
                self.catalogue = PostgresModelCatalog(
                    pool, timeout_seconds=self._settings.model_catalog_read_timeout_seconds
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
                    "Model catalogue reader startup rejected; verify migration and reader role"
                ) from None

    def _verify_read_only(self, connection: SpeechConnection) -> None:
        schema = self._settings.database_schema
        identities = connection.execute(
            "SELECT SESSION_USER AS login, CURRENT_USER AS effective"
        ).fetchone()
        if identities is None:
            raise ValueError("Model catalogue reader identity unavailable")
        for role in dict.fromkeys((identities["login"], identities["effective"])):
            privileged = connection.execute(
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
            if privileged is None or privileged["forbidden"]:
                raise ValueError("Model catalogue reader requires a non-owner identity")
            rows: list[dict[str, Any]] = connection.execute(
                "SELECT c.relname, "
                "pg_catalog.has_table_privilege(%s::name,c.oid,'SELECT') AS can_select, "
                "pg_catalog.has_table_privilege(%s::name,c.oid,"
                "'INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER') AS can_write "
                "FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace "
                "WHERE n.nspname=%s AND c.relkind IN ('r','p','v','m','f')",
                (role, role, schema),
            ).fetchall()
            for row in rows:
                if row["can_write"]:
                    raise ValueError("Model catalogue reader must not write any table")
                if row["can_select"] and row["relname"] not in READABLE:
                    raise ValueError("Model catalogue reader reads beyond its two tables")
            missing = READABLE - {row["relname"] for row in rows if row["can_select"]}
            if missing:
                raise ValueError(
                    "Model catalogue reader lacks SELECT on " + ",".join(sorted(missing))
                )

    def close(self) -> None:
        with self._lifetime:
            self.catalogue = None
            if self._pool is not None:
                self._pool.close()
                self._pool = None
            if self._diagnostics is not None:
                logging.getLogger("psycopg.pool").removeFilter(self._diagnostics)
                self._diagnostics = None
