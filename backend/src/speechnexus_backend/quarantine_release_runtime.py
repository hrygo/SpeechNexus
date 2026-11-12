"""Dedicated non-owner identity and pool for the operator quarantine release.

The release transaction writes B's stop evidence as well as A's capacity
counters, so the API's runtime role must not hold those grants. This runtime
opens its own login from an explicit DSN and proves at startup — column by
column against the live catalog — that the role holds exactly the declared
surface: a missing grant fails the release, and one extra grant fails the
process before it can answer a request.
"""

from __future__ import annotations

import logging
import threading
import uuid

from speechnexus_backend.settings import Settings
from speechnexus_backend.speech.application.quarantine_releases import (
    QuarantineReleaseService,
)
from speechnexus_backend.speech.infrastructure.postgres_pool import (
    SpeechConnection,
    SpeechPool,
    speech_pool,
)
from speechnexus_backend.speech.infrastructure.quarantine_release_postgres import (
    PostgresQuarantineReleases,
)
from speechnexus_backend.speech.infrastructure.quarantine_release_schema import (
    FULL_INSERT,
    FULL_READ,
    READ_COLUMNS,
    TABLE,
    TRIGGER,
    UPDATE_COLUMNS,
)
from speechnexus_backend.startup_privileges import (
    refuse_foreign_relation_privileges,
    refuse_schema_create,
    require_schema_usage,
)

SUBJECT = "Quarantine release"
#: Tables whose columns the exact-equality check covers even when no privilege
#: is declared: an undeclared grant is how a role boundary silently widens.
DECLARED_TABLES = tuple(sorted(FULL_READ | set(READ_COLUMNS) | FULL_INSERT | set(UPDATE_COLUMNS)))


class _PoolDiagnostics(logging.Filter):
    def __init__(self, pool_name: str) -> None:
        super().__init__()
        self._pool_name = pool_name

    def filter(self, record: logging.LogRecord) -> bool:
        if self._pool_name in record.getMessage():
            record.msg, record.args = "Quarantine release database unavailable", ()
            record.exc_info = record.exc_text = record.stack_info = None
        return True


def _verify_no_identity_escape(connection: SpeechConnection) -> None:
    from speechnexus_backend.binding_disable_worker import _verify_no_role_switch

    _verify_no_role_switch(connection, SUBJECT)


def _require_non_owner(connection: SpeechConnection, schema: str) -> None:
    row = connection.execute(
        "SELECT r.rolsuper OR pg_catalog.has_schema_privilege(CURRENT_USER,%s,'CREATE') "
        "OR pg_catalog.pg_has_role(CURRENT_USER,d.datdba,'MEMBER') OR EXISTS ("
        "SELECT 1 FROM pg_catalog.pg_class c "
        "JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace "
        "WHERE n.nspname=%s AND pg_catalog.pg_has_role(CURRENT_USER,c.relowner,'MEMBER')) "
        "AS forbidden FROM pg_catalog.pg_roles r CROSS JOIN pg_catalog.pg_database d "
        "WHERE r.rolname=CURRENT_USER AND d.datname=CURRENT_DATABASE()",
        (schema, schema),
    ).fetchone()
    if row is None or row["forbidden"]:
        raise ValueError(f"{SUBJECT} requires a restricted non-owner identity")
    version = connection.execute(
        "SELECT pg_catalog.current_setting('server_version_num')::integer AS version"
    ).fetchone()
    if version is None or version["version"] < 160000:
        raise ValueError(f"{SUBJECT} requires PostgreSQL 16 or newer")


def _require_minimum_surface(connection: SpeechConnection, schema: str) -> None:
    require_schema_usage(connection, None, schema, subject=SUBJECT)
    refuse_schema_create(connection, None, schema, subject=SUBJECT)
    refuse_foreign_relation_privileges(
        connection,
        None,
        schema,
        DECLARED_TABLES,
        subject=SUBJECT,
        declared_hint="FULL_READ/READ_COLUMNS/FULL_INSERT/UPDATE_COLUMNS",
    )
    rows = connection.execute(
        "SELECT c.relname AS table_name, a.attname AS column_name, "
        "pg_catalog.has_column_privilege(CURRENT_USER,c.oid,a.attnum,'SELECT') AS rd, "
        "pg_catalog.has_column_privilege(CURRENT_USER,c.oid,a.attnum,'UPDATE') AS upd, "
        "pg_catalog.has_column_privilege(CURRENT_USER,c.oid,a.attnum,'INSERT') AS ins, "
        "pg_catalog.has_column_privilege(CURRENT_USER,c.oid,a.attnum,'REFERENCES') AS ref, "
        "pg_catalog.has_table_privilege(CURRENT_USER,c.oid,'DELETE,TRUNCATE,TRIGGER') "
        "AS destructive "
        "FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace "
        "JOIN pg_catalog.pg_attribute a ON a.attrelid=c.oid AND a.attnum>0 "
        "AND NOT a.attisdropped WHERE n.nspname=%s AND c.relkind IN ('r','p','v','m')",
        (schema,),
    ).fetchall()
    seen: set[str] = set()
    for column in rows:
        table, name = column["table_name"], column["column_name"]
        can_read = table in FULL_READ or name in READ_COLUMNS.get(table, set())
        can_update = name in UPDATE_COLUMNS.get(table, set())
        can_insert = table in FULL_INSERT
        if (
            bool(column["rd"]) != can_read
            or bool(column["upd"]) != can_update
            or bool(column["ins"]) != can_insert
            or column["ref"]
            or column["destructive"]
        ):
            raise ValueError(
                f"{SUBJECT} privileges do not match the declared surface at {table}.{name}"
            )
        if table in DECLARED_TABLES:
            seen.add(table)
    if seen != set(DECLARED_TABLES):
        raise ValueError(f"{SUBJECT} schema is incomplete: {sorted(set(DECLARED_TABLES) - seen)}")
    # Two negative assertions keep the harness honest: private command bodies and
    # frozen resource vectors are never readable, and evidence is never mutable.
    for table, name, privilege in (
        ("submit_intents", "request_document", "SELECT"),
        ("admission_leases", "reserved_slots", "UPDATE"),
        ("execution_stop_evidence", "reason", "UPDATE"),
    ):
        held = connection.execute(
            "SELECT pg_catalog.has_column_privilege(CURRENT_USER,%s,%s,%s) AS held",
            (f'"{schema}"."{table}"', name, privilege),
        ).fetchone()
        if held is None or held["held"]:
            raise ValueError(f"{SUBJECT} must not hold {privilege} on {table}.{name}")


def _require_append_only_trigger(connection: SpeechConnection, schema: str) -> None:
    row = connection.execute(
        "SELECT t.tgenabled AS state FROM pg_catalog.pg_trigger t "
        "JOIN pg_catalog.pg_class c ON c.oid=t.tgrelid "
        "JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace "
        "WHERE n.nspname=%s AND c.relname=%s AND t.tgname=%s AND NOT t.tgisinternal",
        (schema, TABLE, TRIGGER),
    ).fetchone()
    if row is None or row["state"] not in ("O", "A"):
        # A missing or disabled trigger turns "never rewritten" into a promise.
        raise ValueError(f"{SUBJECT} requires enabled trigger {TRIGGER} on {TABLE}")


def verify_quarantine_release_role(pool: SpeechPool, schema: str) -> None:
    """Fail startup on any privilege mismatch before a route can answer."""
    with pool.connection(timeout=3) as connection:
        connection.execute("SET LOCAL statement_timeout='3s'")
        _verify_no_identity_escape(connection)
        _require_non_owner(connection, schema)
        _require_minimum_surface(connection, schema)
        _require_append_only_trigger(connection, schema)


class QuarantineReleaseRuntime:
    """Own one release-role pool and its startup verification for the API lifespan."""

    def __init__(self, settings: Settings) -> None:
        if settings.quarantine_release_database_url is None or settings.process_role != "api":
            raise ValueError("Configure an explicit API quarantine release identity")
        self._settings = settings
        self._pool: SpeechPool | None = None
        self.service: QuarantineReleaseService | None = None
        self._diagnostics: _PoolDiagnostics | None = None
        self._lifetime = threading.Lock()

    def open(self) -> None:
        with self._lifetime:
            if self.service is not None:
                raise ValueError("Quarantine release runtime is already open")
            secret = self._settings.quarantine_release_database_url
            assert secret is not None
            name = "speech-quarantine-release-" + uuid.uuid4().hex
            diagnostic = _PoolDiagnostics(name)
            logger = logging.getLogger("psycopg.pool")
            logger.addFilter(diagnostic)
            self._diagnostics = diagnostic
            try:
                pool = speech_pool(
                    secret.get_secret_value(),
                    schema=self._settings.database_schema,
                    max_size=self._settings.quarantine_release_concurrency,
                    name=name,
                )
                self._pool = pool
                pool.open()
                verify_quarantine_release_role(pool, self._settings.database_schema)
                self.service = QuarantineReleaseService(
                    PostgresQuarantineReleases(
                        pool,
                        timeout_seconds=self._settings.quarantine_release_transaction_timeout_seconds,
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
                    "Quarantine release startup rejected; verify migration and release role"
                ) from None

    def close(self) -> None:
        with self._lifetime:
            self.service = None
            if self._pool is not None:
                self._pool.close()
                self._pool = None
            if self._diagnostics is not None:
                logging.getLogger("psycopg.pool").removeFilter(self._diagnostics)
                self._diagnostics = None


__all__ = ("QuarantineReleaseRuntime", "verify_quarantine_release_role")
