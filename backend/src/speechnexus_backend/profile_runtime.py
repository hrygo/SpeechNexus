"""Owned profile writer lifetime; startup verifies but never provisions authority."""

from __future__ import annotations

import logging
import threading
import uuid

from speechnexus_backend.settings import Settings
from speechnexus_backend.speech.infrastructure.postgres_pool import SpeechPool, speech_pool
from speechnexus_backend.speech.infrastructure.profile_drafts_postgres import PostgresProfileDrafts


class _PoolDiagnostics(logging.Filter):
    def __init__(self, name: str) -> None:
        super().__init__()
        self._pool_name = name

    def filter(self, record: logging.LogRecord) -> bool:
        if self._pool_name in record.getMessage():
            record.msg, record.args = "Profile database connection unavailable", ()
            record.exc_info = record.exc_text = record.stack_info = None
        return True


class ProfileRuntime:
    def __init__(self, settings: Settings) -> None:
        if settings.profile_writer_database_url is None or settings.process_role != "api":
            raise ValueError("Configure an explicit API profile writer identity")
        self._settings = settings
        self._pool: SpeechPool | None = None
        self.provider: PostgresProfileDrafts | None = None
        # Only this object's connection-pool lifetime is protected here. Profile
        # allocation/idempotency are coordinated by the existing PG transactions.
        self._lifetime = threading.Lock()
        self._diagnostics: _PoolDiagnostics | None = None

    def open(self) -> None:
        with self._lifetime:
            if self.provider is not None:
                raise ValueError("Profile writer is already open")
            secret = self._settings.profile_writer_database_url
            assert secret is not None
            pool_name = "speech-profile-" + uuid.uuid4().hex
            diagnostic = _PoolDiagnostics(pool_name)
            logger = logging.getLogger("psycopg.pool")
            logger.addFilter(diagnostic)
            self._pool, self._diagnostics = None, diagnostic
            try:
                pool = speech_pool(
                    secret.get_secret_value(),
                    schema=self._settings.database_schema,
                    max_size=self._settings.profile_receive_concurrency,
                    name=pool_name,
                )
                self._pool = pool
                pool.open()
                self._verify_principal(pool)
                self.provider = PostgresProfileDrafts(
                    pool,
                    timeout_seconds=self._settings.profile_transaction_timeout_seconds,
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
                    "Profile writer startup rejected; verify migration and minimum role"
                ) from None

    def _verify_principal(self, pool: SpeechPool) -> None:
        schema = self._settings.database_schema
        with pool.connection(timeout=3) as connection:
            connection.execute("SET LOCAL statement_timeout='3s'")
            row = connection.execute(
                "SELECT r.rolsuper OR pg_catalog.has_schema_privilege(%s,'CREATE') "
                "OR pg_catalog.pg_has_role(d.datdba,'MEMBER') OR EXISTS ("
                "SELECT 1 FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n "
                "ON n.oid=c.relnamespace WHERE n.nspname=%s "
                "AND pg_catalog.pg_has_role(c.relowner,'MEMBER')) AS forbidden "
                "FROM pg_catalog.pg_roles r CROSS JOIN pg_catalog.pg_database d "
                "WHERE r.rolname=CURRENT_USER AND d.datname=CURRENT_DATABASE()",
                (schema, schema),
            ).fetchone()
            if row is None or row["forbidden"]:
                raise ValueError("Profile writer requires a non-owner runtime identity")
            extra = connection.execute(
                "SELECT EXISTS (SELECT 1 FROM pg_catalog.pg_class c "
                "JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace "
                "WHERE n.nspname=%s AND c.relkind IN ('r','p','v','m','f') "
                "AND c.relname NOT IN ('speech_profile_families','speech_profile_revisions',"
                "'speech_profile_commands') AND ("
                "pg_catalog.has_table_privilege(c.oid,"
                "'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER') "
                "OR pg_catalog.has_any_column_privilege(c.oid,'SELECT,INSERT,UPDATE,REFERENCES'))) "
                "AS forbidden",
                (schema,),
            ).fetchone()
            if extra is None or extra["forbidden"]:
                raise ValueError("Profile writer cannot hold other speech table privileges")
            for table in (
                "speech_profile_families",
                "speech_profile_revisions",
                "speech_profile_commands",
            ):
                name = f'"{schema}".{table}'
                row = connection.execute(
                    "SELECT pg_catalog.has_table_privilege(%s,'SELECT') "
                    "AND pg_catalog.has_table_privilege(%s,'INSERT') "
                    "AND NOT pg_catalog.has_table_privilege(%s,'DELETE') "
                    "AND NOT pg_catalog.has_table_privilege(%s,'TRUNCATE,REFERENCES,TRIGGER') "
                    "AS permitted",
                    (name, name, name, name),
                ).fetchone()
                if row is None or not row["permitted"]:
                    raise ValueError("Profile writer privileges do not match the port")
                mutable = (
                    {"latest_revision"}
                    if table == "speech_profile_families"
                    else {"command_state", "revision", "error_code"}
                    if table == "speech_profile_commands"
                    else set()
                )
                columns = connection.execute(
                    "SELECT column_name, pg_catalog.has_column_privilege(%s,column_name,'UPDATE') "
                    "AS updatable FROM information_schema.columns "
                    "WHERE table_schema=%s AND table_name=%s",
                    (name, schema, table),
                ).fetchall()
                if not columns or {c["column_name"] for c in columns if c["updatable"]} != mutable:
                    raise ValueError("Profile immutable history cannot be updatable")

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
