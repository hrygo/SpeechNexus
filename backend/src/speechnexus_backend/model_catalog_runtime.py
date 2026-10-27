"""Read-only model catalogue identity; it may read published facts and nothing else.

The catalogue is public, so its reader must not be able to reach drafts, commands,
coordination history or any write path. It reads the publication table and the
published-only security-barrier view, never the draft revision table. Startup
verifies the granted surface instead of trusting configuration.
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

READABLE = frozenset({"speech_publications", "speech_published_binding_revisions"})


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
        identities = connection.execute(
            "SELECT SESSION_USER AS login, CURRENT_USER AS effective"
        ).fetchone()
        if identities is None:
            raise ValueError("Model catalogue reader identity unavailable")
        reached = connection.execute(
            "WITH RECURSIVE reachable(role_oid) AS ("
            "SELECT oid FROM pg_catalog.pg_roles WHERE rolname IN (%s,%s) UNION "
            "SELECT membership.roleid FROM pg_catalog.pg_auth_members AS membership "
            "JOIN reachable ON membership.member=reachable.role_oid"
            ") SELECT role.rolname FROM pg_catalog.pg_roles AS role "
            "JOIN reachable ON role.oid=reachable.role_oid ORDER BY role.rolname",
            (identities["login"], identities["effective"]),
        ).fetchall()
        if not reached:
            raise ValueError("Model catalogue reader has no auditable role")
        roles = {row["rolname"] for row in reached}
        # PostgreSQL reserves the pg_ prefix for built-in roles. Rejecting the
        # whole namespace is an allowlist: application roles remain auditable and
        # future predefined roles (monitor, server-file, signalling, ...) cannot
        # slip through a growing denylist.
        predefined = sorted(role for role in roles if role.startswith("pg_"))
        if predefined:
            raise ValueError(
                "Model catalogue reader inherits predefined PostgreSQL role(s) "
                + ",".join(predefined)
            )
        for role in sorted(roles):
            self._verify_role(connection, role, roles)

    def _verify_role(
        self, connection: SpeechConnection, role: str, reachable_roles: set[str]
    ) -> None:
        schema = self._settings.database_schema
        privileged = connection.execute(
            "SELECT r.rolsuper OR r.rolcreaterole OR r.rolcreatedb OR r.rolreplication "
            "OR r.rolbypassrls "
            "OR NOT COALESCE(pg_catalog.has_schema_privilege(%s::name,%s,'USAGE'),false) "
            "OR EXISTS (SELECT 1 FROM pg_catalog.pg_namespace n "
            "WHERE n.nspname !~ '^pg_' AND n.nspname <> 'information_schema' "
            "AND (pg_catalog.pg_has_role(%s::name,n.nspowner,'MEMBER') "
            "OR pg_catalog.has_schema_privilege(%s::name,n.oid,'CREATE'))) "
            "OR pg_catalog.pg_has_role(%s::name,d.datdba,'MEMBER') "
            "OR pg_catalog.has_database_privilege(%s::name,d.oid,'CREATE') OR EXISTS ("
            "SELECT 1 FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n "
            "ON n.oid=c.relnamespace WHERE n.nspname !~ '^pg_' "
            "AND n.nspname <> 'information_schema' "
            "AND pg_catalog.pg_has_role(%s::name,c.relowner,'MEMBER')) AS forbidden "
            "FROM pg_catalog.pg_roles r CROSS JOIN pg_catalog.pg_database d "
            "WHERE r.rolname=%s AND d.datname=CURRENT_DATABASE()",
            (role, schema, role, role, role, role, role, role),
        ).fetchone()
        if privileged is None or privileged["forbidden"]:
            raise ValueError("Model catalogue reader requires a non-owner, non-creator identity")

        temporary = connection.execute(
            "SELECT grantee.rolname AS grantee FROM pg_catalog.pg_database AS d "
            "CROSS JOIN LATERAL pg_catalog.aclexplode("
            "COALESCE(d.datacl, pg_catalog.acldefault('d', d.datdba))) AS acl "
            "JOIN pg_catalog.pg_roles AS grantee ON grantee.oid=acl.grantee "
            "WHERE d.datname=pg_catalog.current_database() AND acl.grantee <> 0 "
            "AND acl.privilege_type='TEMPORARY'"
        ).fetchall()
        if any(row["grantee"] in reachable_roles for row in temporary):
            raise ValueError("Model catalogue reader must not hold database TEMP privilege")

        rows: list[dict[str, Any]] = connection.execute(
            "SELECT n.nspname AS schema_name, c.relname, "
            "pg_catalog.has_table_privilege(%s::name,c.oid,'SELECT') AS can_select, "
            "pg_catalog.has_any_column_privilege(%s::name,c.oid,'SELECT') AS can_select_column, "
            "pg_catalog.has_table_privilege(%s::name,c.oid,"
            "'INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER') AS can_write, "
            "pg_catalog.has_any_column_privilege(%s::name,c.oid,"
            "'INSERT,UPDATE,REFERENCES') AS can_write_column "
            "FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace "
            "WHERE n.nspname !~ '^pg_' AND n.nspname <> 'information_schema' "
            "AND c.relkind IN ('r','p','v','m','f')",
            (role, role, role, role),
        ).fetchall()
        for row in rows:
            if row["can_write"] or row["can_write_column"]:
                raise ValueError("Model catalogue reader must not write any table")
            if (row["can_select"] or row["can_select_column"]) and (
                row["schema_name"] != schema or row["relname"] not in READABLE
            ):
                raise ValueError("Model catalogue reader reads beyond its published surface")
        missing = READABLE - {
            row["relname"] for row in rows if row["schema_name"] == schema and row["can_select"]
        }
        if missing:
            raise ValueError("Model catalogue reader lacks SELECT on " + ",".join(sorted(missing)))

        sequences: list[dict[str, Any]] = connection.execute(
            "SELECT n.nspname AS schema_name, c.relname "
            "FROM pg_catalog.pg_class AS c "
            "JOIN pg_catalog.pg_namespace AS n ON n.oid=c.relnamespace "
            "WHERE n.nspname !~ '^pg_' AND n.nspname <> 'information_schema' "
            "AND c.relkind='S' AND ("
            "pg_catalog.has_sequence_privilege(%s::name,c.oid,'SELECT') OR "
            "pg_catalog.has_sequence_privilege(%s::name,c.oid,'USAGE') OR "
            "pg_catalog.has_sequence_privilege(%s::name,c.oid,'UPDATE'))",
            (role, role, role),
        ).fetchall()
        if sequences:
            raise ValueError("Model catalogue reader must not use any sequence")

        definer = connection.execute(
            "SELECT p.oid::regprocedure::text AS signature FROM pg_catalog.pg_proc AS p "
            "JOIN pg_catalog.pg_namespace AS n ON n.oid=p.pronamespace "
            "WHERE n.nspname !~ '^pg_' AND n.nspname <> 'information_schema' AND p.prosecdef "
            "AND pg_catalog.has_function_privilege(%s::name,p.oid,'EXECUTE') LIMIT 1",
            (role,),
        ).fetchone()
        if definer is not None:
            raise ValueError("Model catalogue reader must not execute SECURITY DEFINER functions")

    def close(self) -> None:
        with self._lifetime:
            self.catalogue = None
            if self._pool is not None:
                self._pool.close()
                self._pool = None
            if self._diagnostics is not None:
                logging.getLogger("psycopg.pool").removeFilter(self._diagnostics)
                self._diagnostics = None
