"""Owned binding writer lifetime; startup verifies but never provisions authority."""

from __future__ import annotations

import logging
import threading
import uuid
from collections.abc import Callable

from pydantic import SecretStr

from speechnexus_backend.settings import Settings
from speechnexus_backend.speech.infrastructure.binding_defaults_postgres import (
    PostgresBindingDefaults,
)
from speechnexus_backend.speech.infrastructure.binding_defaults_schema import TABLE
from speechnexus_backend.speech.infrastructure.binding_drafts_postgres import PostgresBindingDrafts
from speechnexus_backend.speech.infrastructure.postgres_pool import (
    SpeechConnection,
    SpeechPool,
    speech_pool,
)


class _PoolDiagnostics(logging.Filter):
    def __init__(self, name: str) -> None:
        super().__init__()
        self._pool_name = name

    def filter(self, record: logging.LogRecord) -> bool:
        if self._pool_name in record.getMessage():
            record.msg, record.args = "Binding database connection unavailable", ()
            record.exc_info = record.exc_text = record.stack_info = None
        return True


_BINDING_TABLES = (
    "speech_binding_families",
    "speech_binding_revisions",
    "speech_binding_commands",
)


class _BindingRuntime[T]:
    def __init__(
        self,
        settings: Settings,
        writer_url: SecretStr | None,
        tables: tuple[str, ...],
        new_provider: Callable[[SpeechPool, float], T],
    ) -> None:
        if writer_url is None or settings.process_role != "api":
            raise ValueError("Configure an explicit API binding writer identity")
        self._settings = settings
        self._writer_url, self._tables, self._new_provider = writer_url, tables, new_provider
        self._pool: SpeechPool | None = None
        self.provider: T | None = None
        # Only this object's connection-pool lifetime is protected here. Binding
        # allocation/idempotency are coordinated by the existing PG transactions.
        self._lifetime = threading.Lock()
        self._diagnostics: _PoolDiagnostics | None = None

    def open(self) -> None:
        with self._lifetime:
            if self.provider is not None:
                raise ValueError("Binding writer is already open")
            secret = self._writer_url
            pool_name = "speech-binding-" + uuid.uuid4().hex
            diagnostic = _PoolDiagnostics(pool_name)
            logger = logging.getLogger("psycopg.pool")
            logger.addFilter(diagnostic)
            self._pool, self._diagnostics = None, diagnostic
            try:
                pool = speech_pool(
                    secret.get_secret_value(),
                    schema=self._settings.database_schema,
                    max_size=self._settings.binding_receive_concurrency,
                    name=pool_name,
                )
                self._pool = pool
                pool.open()
                self._verify_principal(pool)
                self.provider = self._new_provider(
                    pool, self._settings.binding_transaction_timeout_seconds
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
                    "Binding writer startup rejected; verify migration and minimum role"
                ) from None

    def _verify_principal(self, pool: SpeechPool) -> None:
        with pool.connection(timeout=3) as connection:
            connection.execute("SET LOCAL statement_timeout='3s'")
            identities = connection.execute(
                "SELECT SESSION_USER AS login, CURRENT_USER AS effective"
            ).fetchone()
            if identities is None:
                raise ValueError("Binding writer identity is unavailable")
            # A startup role can hide the login's authority. RESET ROLE restores
            # that startup role, not necessarily the login. Inspect both names
            # directly without changing any connection role or server grants.
            for role in dict.fromkeys((identities["login"], identities["effective"])):
                self._verify_role(connection, role)

    def _verify_role(self, connection: SpeechConnection, role: str) -> None:
        schema = self._settings.database_schema
        row = connection.execute(
            "SELECT r.rolsuper OR pg_catalog.has_schema_privilege(%s::name,%s,'CREATE') "
            "OR pg_catalog.pg_has_role(%s::name,d.datdba,'MEMBER') OR EXISTS ("
            "SELECT 1 FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n "
            "ON n.oid=c.relnamespace WHERE n.nspname=%s "
            "AND pg_catalog.pg_has_role(%s::name,c.relowner,'MEMBER')) AS forbidden "
            "FROM pg_catalog.pg_roles r CROSS JOIN pg_catalog.pg_database d "
            "WHERE r.rolname=%s AND d.datname=CURRENT_DATABASE()",
            (role, schema, role, schema, role, role),
        ).fetchone()
        if row is None or row["forbidden"]:
            raise ValueError("Binding writer requires a non-owner runtime identity")
        extra = connection.execute(
            "SELECT EXISTS (SELECT 1 FROM pg_catalog.pg_class c "
            "JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace "
            "WHERE n.nspname=%s AND c.relkind IN ('r','p','v','m','f') "
            "AND c.relname NOT IN (" + ",".join("%s" for _ in self._tables) + ") AND ("
            "pg_catalog.has_table_privilege(%s::name,c.oid,"
            "'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER') "
            "OR pg_catalog.has_any_column_privilege(%s::name,c.oid,"
            "'SELECT,INSERT,UPDATE,REFERENCES'))) AS forbidden",
            (schema, *self._tables, role, role),
        ).fetchone()
        if extra is None or extra["forbidden"]:
            raise ValueError("Binding writer cannot hold other speech table privileges")
        for table in self._tables:
            name = f'"{schema}".{table}'
            row = connection.execute(
                "SELECT pg_catalog.has_table_privilege(%s::name,%s,'SELECT') "
                "AND pg_catalog.has_table_privilege(%s::name,%s,'INSERT') "
                "AND NOT pg_catalog.has_table_privilege(%s::name,%s,'DELETE') "
                "AND NOT pg_catalog.has_table_privilege(%s::name,%s,'TRUNCATE,REFERENCES,TRIGGER') "
                "AS permitted",
                (role, name, role, name, role, name, role, name),
            ).fetchone()
            if row is None or not row["permitted"]:
                raise ValueError("Binding writer privileges do not match the port")
            mutable = {"latest_revision"} if table == "speech_binding_families" else set()
            columns = connection.execute(
                "SELECT column_name, pg_catalog.has_column_privilege(%s::name,%s,column_name,"
                "'UPDATE') AS updatable FROM information_schema.columns "
                "WHERE table_schema=%s AND table_name=%s",
                (role, name, schema, table),
            ).fetchall()
            if not columns or {c["column_name"] for c in columns if c["updatable"]} != mutable:
                raise ValueError("Binding immutable history cannot be updatable")

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


class BindingRuntime(_BindingRuntime[PostgresBindingDrafts]):
    def __init__(self, settings: Settings) -> None:
        super().__init__(
            settings,
            settings.binding_writer_database_url,
            _BINDING_TABLES,
            lambda pool, seconds: PostgresBindingDrafts(pool, timeout_seconds=seconds),
        )


class BindingDefaultsRuntime(_BindingRuntime[PostgresBindingDefaults]):
    def __init__(self, settings: Settings) -> None:
        super().__init__(
            settings,
            settings.binding_defaults_writer_database_url,
            (*_BINDING_TABLES, TABLE),
            lambda pool, seconds: PostgresBindingDefaults(pool, timeout_seconds=seconds),
        )
