"""Owned public Job writer lifetime; startup verifies but never provisions grants."""

from __future__ import annotations

import logging
import threading
import uuid

from speechnexus_backend.settings import Settings
from speechnexus_backend.speech.application.public_jobs import PublicJobs
from speechnexus_backend.speech.application.voice_drafts import VoiceDrafts
from speechnexus_backend.speech.application.voice_publications import VoicePublications
from speechnexus_backend.speech.infrastructure.postgres_pool import (
    SpeechConnection,
    SpeechPool,
    speech_pool,
)
from speechnexus_backend.speech.infrastructure.public_jobs_postgres import PostgresPublicJobs
from speechnexus_backend.speech.infrastructure.voice_drafts_postgres import PostgresVoiceDrafts
from speechnexus_backend.speech.infrastructure.voice_publications_postgres import (
    PostgresVoicePublications,
)
from speechnexus_backend.startup_privileges import (
    refuse_column_privileges,
    refuse_table_privileges,
)

_ADMISSION_RESERVATION_TABLES = ("admission_attempts", "admission_requests", "admission_leases")
_ADMISSION_REQUIRED_SELECT = (
    "workspace_quotas",
    "resource_pools",
    "host_nodes",
    "engine_instances",
    "runtime_instance_heads",
    "runtime_instance_intents",
    "admission_operations",
    "admission_leases",
    "speech_jobs",
    "speech_job_intents",
)

_PUBLICATION_READ_ONLY = (
    "speech_voice_capability_evidence",
    "speech_jobs",
    "speech_artifacts",
    "speech_assets",
    "speech_asset_objects",
    "speech_binding_revisions",
    "speech_voice_draft_revisions",
)
_PUBLICATION_APPEND_ONLY = (
    "speech_publications",
    "speech_voice_revisions",
    "speech_voice_publication_intents",
    "speech_asset_references",
)
_PUBLICATION_UPDATE_COLUMNS = {
    "speech_publication_heads": ("revision", "revision_id", "updated_at_us"),
    "speech_catalog_state": ("revision", "generated_at_us"),
    "speech_voice_drafts": ("status",),
}
_PUBLICATION_REQUIRED_SELECT = (
    *_PUBLICATION_READ_ONLY,
    *_PUBLICATION_APPEND_ONLY,
    *_PUBLICATION_UPDATE_COLUMNS,
)
_PUBLICATION_REQUIRED_INSERT = (*_PUBLICATION_APPEND_ONLY, "speech_publication_heads")

REQUIRED_SELECT = (
    "speech_workspaces",
    "speech_publications",
    "speech_publication_heads",
    "speech_catalog_state",
    "speech_publication_retirements",
    "speech_binding_revisions",
    "speech_assets",
    "speech_pronunciation_revisions",
    "speech_jobs",
    "speech_job_intents",
    "speech_job_events",
    "speech_job_submissions",
    "speech_job_cancellations",
    "speech_artifacts",
    "speech_voice_drafts",
    "speech_voice_draft_revisions",
    "speech_voice_metadata_commands",
)
REQUIRED_INSERT = (
    "speech_voice_drafts",
    "speech_voice_draft_revisions",
    "speech_voice_metadata_commands",
    "speech_jobs",
    "speech_job_intents",
    "speech_job_events",
    "speech_job_submissions",
    "speech_job_cancellations",
)
# Append-only rows: the writer may insert its own intent and read it back, but a
# changed or deleted intent must never be indistinguishable from the original.
IMMUTABLE_TABLES = ("speech_voice_draft_revisions", "speech_job_intents")
REQUIRED_INSERT_COLUMNS: dict[str, tuple[str, ...]] = {
    "speech_voice_draft_revisions": (
        "draft_id",
        "revision",
        "kind",
        "generation_job_id",
        "input_json",
        "binding_json",
        "created_at_us",
    ),
}
REQUIRED_UPDATE_COLUMNS: dict[str, tuple[str, ...]] = {
    "speech_voice_drafts": (
        "display_name",
        "metadata_revision",
        "updated_at_us",
        "head_revision",
        "generation_job_id",
    ),
    "speech_jobs": ("state", "status_version", "result_json", "error_code", "updated_at_us"),
}

_JOB_SUBJECT = "Public Job writer"
# The dispatcher appends artifact metadata with a column INSERT, so this table is
# column-writable even though it is read-only at the table level.
_JOB_COLUMN_WRITABLE = frozenset(("speech_artifacts",))
_PUBLICATION_SUBJECT = "Voice publisher"


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
        self._admission_pool: SpeechPool | None = None
        self._publisher_pool: SpeechPool | None = None
        self.jobs: PublicJobs | None = None
        self.voice_drafts: VoiceDrafts | None = None
        self.voice_publications: VoicePublications | None = None
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
                admission_pool = self._open_admission_pool()
                if admission_pool is not None and _database_identity(pool) != _database_identity(
                    admission_pool
                ):
                    raise ValueError(
                        "Public Job writer and admission roles must share one database"
                    )
                self._admission_pool = admission_pool
                publisher_pool = self._open_publisher_pool()
                if publisher_pool is not None and _database_identity(pool) != _database_identity(
                    publisher_pool
                ):
                    raise ValueError(
                        "Public Job writer and voice publisher must share one database"
                    )
                self._publisher_pool = publisher_pool
                adapter = PostgresPublicJobs(
                    pool,
                    admission_pool=admission_pool,
                    require_admission=True,
                    timeout_seconds=self._settings.job_transaction_timeout_seconds,
                    storage_bucket=self._settings.object_storage_bucket,
                )
                self.jobs = PublicJobs(adapter, adapter)
                self.voice_drafts = VoiceDrafts(self.jobs, PostgresVoiceDrafts(pool))
                self.voice_publications = (
                    VoicePublications(
                        PostgresVoicePublications(
                            publisher_pool,
                            timeout_seconds=self._settings.job_transaction_timeout_seconds,
                        )
                    )
                    if publisher_pool is not None
                    else None
                )
            except BaseException as error:
                try:
                    if self._publisher_pool is not None:
                        self._publisher_pool.close()
                        self._publisher_pool = None
                    if self._admission_pool is not None:
                        self._admission_pool.close()
                        self._admission_pool = None
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

    def _open_admission_pool(self) -> SpeechPool | None:
        secret = self._settings.job_admission_database_url
        if secret is None:
            return None
        name = "speech-job-admission-" + uuid.uuid4().hex
        pool = speech_pool(
            secret.get_secret_value(),
            schema=self._settings.database_schema,
            max_size=4,
            name=name,
        )
        pool.open()
        try:
            with pool.connection(timeout=3) as connection:
                connection.execute("SET LOCAL statement_timeout='3s'")
                _verify_admission_privileges(connection, self._settings.database_schema)
        except BaseException:
            pool.close()
            raise
        return pool

    def _open_publisher_pool(self) -> SpeechPool | None:
        secret = self._settings.voice_publisher_database_url
        if secret is None:
            return None
        name = "speech-voice-publisher-" + uuid.uuid4().hex
        pool = speech_pool(
            secret.get_secret_value(),
            schema=self._settings.database_schema,
            max_size=4,
            name=name,
        )
        pool.open()
        try:
            with pool.connection(timeout=3) as connection:
                connection.execute("SET LOCAL statement_timeout='3s'")
                _verify_publication_privileges(connection, self._settings.database_schema)
        except BaseException:
            pool.close()
            raise
        return pool

    def close(self) -> None:
        with self._lifetime:
            self.jobs = None
            self.voice_drafts = None
            self.voice_publications = None
            if self._publisher_pool is not None:
                self._publisher_pool.close()
                self._publisher_pool = None
            if self._admission_pool is not None:
                self._admission_pool.close()
                self._admission_pool = None
            if self._pool is not None:
                self._pool.close()
                self._pool = None
            if self._diagnostics is not None:
                logging.getLogger("psycopg.pool").removeFilter(self._diagnostics)
                self._diagnostics = None


def _database_identity(pool: SpeechPool) -> tuple[str, str, int]:
    with pool.connection(timeout=3) as connection:
        row = connection.execute(
            "SELECT current_database() AS database, "
            "COALESCE(host(inet_server_addr()), 'local') AS host, "
            "COALESCE(inet_server_port(), 0) AS port"
        ).fetchone()
    if row is None:
        raise ValueError("Public Job database identity is unavailable")
    return str(row["database"]), str(row["host"]), int(row["port"])


def _verify_privileges(connection: SpeechConnection, schema: str) -> None:
    rows = connection.execute(
        "SELECT c.relname AS name, c.relkind AS kind, "
        "pg_catalog.has_table_privilege(CURRENT_USER, c.oid, 'SELECT') AS can_select, "
        "pg_catalog.has_table_privilege(CURRENT_USER, c.oid, 'INSERT') AS can_insert "
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
    for table, columns in REQUIRED_INSERT_COLUMNS.items():
        for column in columns:
            allowed = connection.execute(
                "SELECT pg_catalog.has_column_privilege(CURRENT_USER, %s, %s, 'INSERT') AS ok",
                (f"{schema}.{table}", column),
            ).fetchone()
            if allowed is None or not allowed["ok"]:
                raise ValueError(f"Public Job writer lacks INSERT on {table}.{column}")
    for table, columns in REQUIRED_UPDATE_COLUMNS.items():
        for column in columns:
            allowed = connection.execute(
                "SELECT pg_catalog.has_column_privilege(CURRENT_USER, %s, %s, 'UPDATE') AS ok",
                (f"{schema}.{table}", column),
            ).fetchone()
            if allowed is None or not allowed["ok"]:
                raise ValueError(f"Public Job writer lacks UPDATE on {table}.{column}")
    for table in REQUIRED_INSERT:
        # A writable table still must not confer deletion, truncation or the two
        # whole-table authorities a writer never needs (#561).
        refuse_table_privileges(
            connection,
            None,
            f"{schema}.{table}",
            table,
            ("DELETE", "TRUNCATE", "REFERENCES", "TRIGGER"),
            subject=_JOB_SUBJECT,
        )
    # A table that is read-only at the table level can still be legitimately
    # column-writable: the dispatcher appends artifact metadata with a column
    # INSERT, so that table is judged by its declared column surface instead.
    for table in _JOB_COLUMN_WRITABLE:
        qualified = f"{schema}.{table}"
        refuse_table_privileges(
            connection,
            None,
            qualified,
            table,
            ("INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER"),
            subject=_JOB_SUBJECT,
        )
        refuse_column_privileges(
            connection,
            None,
            qualified,
            table,
            ("UPDATE", "REFERENCES"),
            subject=_JOB_SUBJECT,
        )
    # Read-only tables: table-level INSERT is judged on its own, never inferred
    # from a column grant. Column INSERT/UPDATE/REFERENCES are refused directly.
    read_only_tables = tuple(
        table
        for table in REQUIRED_SELECT
        if table not in REQUIRED_INSERT and table not in _JOB_COLUMN_WRITABLE
    )
    for table in read_only_tables:
        qualified = f"{schema}.{table}"
        refuse_table_privileges(
            connection,
            None,
            qualified,
            table,
            ("INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER"),
            subject=_JOB_SUBJECT,
        )
        refuse_column_privileges(
            connection,
            None,
            qualified,
            table,
            ("INSERT", "UPDATE", "REFERENCES"),
            subject=_JOB_SUBJECT,
        )
    for table in _ADMISSION_RESERVATION_TABLES:
        row = connection.execute(
            "SELECT pg_catalog.has_any_column_privilege(CURRENT_USER, %s, 'INSERT') AS ok",
            (f"{schema}.{table}",),
        ).fetchone()
        if row is None or row["ok"]:
            raise ValueError("Public Job writer must not insert admission reservations on " + table)
    for table in IMMUTABLE_TABLES:
        attribute_rows = connection.execute(
            "SELECT a.attname AS name FROM pg_catalog.pg_attribute AS a "
            "WHERE a.attrelid = %s::regclass AND a.attnum > 0 AND NOT a.attisdropped",
            (f"{schema}.{table}",),
        ).fetchall()
        for attribute in attribute_rows:
            allowed = connection.execute(
                "SELECT pg_catalog.has_column_privilege(CURRENT_USER, %s, %s, 'UPDATE') AS ok",
                (f"{schema}.{table}", attribute["name"]),
            ).fetchone()
            if allowed is not None and allowed["ok"]:
                raise ValueError(f"Public Job writer must not update {table}.{attribute['name']}")


def _verify_publication_surface(connection: SpeechConnection, schema: str) -> None:
    """The publisher's authority is exactly its declared tables (#549).

    A stray table grant, a column grant on a foreign relation or the ability to
    create schema objects is refused by name. This closes the gap where a table
    outside the declared set -- the retirement ledger, for one -- could be written
    while every declared-table assertion still held.
    """
    schema_row = connection.execute(
        "SELECT pg_catalog.has_schema_privilege(CURRENT_USER, %s, 'USAGE') AS usable, "
        "pg_catalog.has_schema_privilege(CURRENT_USER, %s, 'CREATE') AS creatable",
        (schema, schema),
    ).fetchone()
    if schema_row is None or not schema_row["usable"]:
        raise ValueError(f"Voice publisher lacks USAGE on schema {schema}")
    if schema_row["creatable"]:
        raise ValueError(f"Voice publisher must not hold CREATE on schema {schema}")
    stray = connection.execute(
        "SELECT c.relname AS name FROM pg_catalog.pg_class c "
        "JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace "
        "WHERE n.nspname=%s AND c.relkind IN ('r','p','v','m','f') "
        "AND c.relname <> ALL(%s::text[]) AND ("
        "pg_catalog.has_table_privilege(CURRENT_USER, c.oid, "
        "'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER') OR "
        "pg_catalog.has_any_column_privilege(CURRENT_USER, c.oid, "
        "'SELECT,INSERT,UPDATE,REFERENCES')) "
        "ORDER BY c.relname LIMIT 1",
        (schema, list(_PUBLICATION_REQUIRED_SELECT)),
    ).fetchone()
    if stray is not None:
        raise ValueError(
            f"Voice publisher must not hold privileges on undeclared table {stray['name']}; "
            "add it to _PUBLICATION_REQUIRED_SELECT with only its declared privileges, "
            "or revoke the stray grant"
        )


def _verify_publication_privileges(connection: SpeechConnection, schema: str) -> None:
    """The publisher may measure nothing and append only its own projection.

    The tables it may write still need REFERENCES and TRIGGER refused (#549): both
    are whole-table grants even when the row itself is appended or narrowed. Table
    and column authority are judged separately -- ``has_table_privilege`` stays
    false under a column-only grant, so every column decision uses the column
    helpers. Relations outside the declared set are refused as well.
    """
    _verify_publication_surface(connection, schema)
    for table in _PUBLICATION_REQUIRED_SELECT:
        allowed = connection.execute(
            "SELECT pg_catalog.has_table_privilege(CURRENT_USER, %s, 'SELECT') AS ok",
            (f"{schema}.{table}",),
        ).fetchone()
        if allowed is None or not allowed["ok"]:
            raise ValueError(f"Voice publisher lacks SELECT on {table}")
    for table in _PUBLICATION_REQUIRED_INSERT:
        allowed = connection.execute(
            "SELECT pg_catalog.has_table_privilege(CURRENT_USER, %s, 'INSERT') AS ok",
            (f"{schema}.{table}",),
        ).fetchone()
        if allowed is None or not allowed["ok"]:
            raise ValueError(f"Voice publisher lacks INSERT on {table}")
    for table in ("speech_voice_drafts", "speech_catalog_state"):
        append = connection.execute(
            "SELECT pg_catalog.has_table_privilege(CURRENT_USER, %s, 'INSERT') AS ok",
            (f"{schema}.{table}",),
        ).fetchone()
        if append is None or append["ok"]:
            raise ValueError(f"Voice publisher can append to {table}")
    for table, columns in _PUBLICATION_UPDATE_COLUMNS.items():
        qualified = f"{schema}.{table}"
        refuse_table_privileges(
            connection,
            None,
            qualified,
            table,
            ("UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER"),
            subject=_PUBLICATION_SUBJECT,
        )
        if table not in _PUBLICATION_REQUIRED_INSERT:
            refuse_column_privileges(
                connection,
                None,
                qualified,
                table,
                ("INSERT",),
                subject=_PUBLICATION_SUBJECT,
            )
        refuse_column_privileges(
            connection,
            None,
            qualified,
            table,
            ("REFERENCES",),
            subject=_PUBLICATION_SUBJECT,
        )
        column_rows = connection.execute(
            "SELECT column_name, pg_catalog.has_column_privilege(CURRENT_USER, %s, "
            "column_name, 'UPDATE') AS updatable FROM information_schema.columns "
            "WHERE table_schema=%s AND table_name=%s",
            (qualified, schema, table),
        ).fetchall()
        if {row["column_name"] for row in column_rows if row["updatable"]} != set(columns):
            raise ValueError(f"Voice publisher UPDATE columns do not match {table}")
    for table in _PUBLICATION_READ_ONLY:
        qualified = f"{schema}.{table}"
        refuse_table_privileges(
            connection,
            None,
            qualified,
            table,
            ("UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER"),
            subject=_PUBLICATION_SUBJECT,
        )
        append = connection.execute(
            "SELECT pg_catalog.has_table_privilege(CURRENT_USER, %s, 'INSERT') AS ok",
            (qualified,),
        ).fetchone()
        if append is None or append["ok"]:
            raise ValueError(f"Voice publisher can append to read-only table {table}")
        # A column INSERT is enough to forge a measured fact row even though the
        # table-level INSERT stays false (#549).
        refuse_column_privileges(
            connection,
            None,
            qualified,
            table,
            ("INSERT", "UPDATE", "REFERENCES"),
            subject=_PUBLICATION_SUBJECT,
        )
    for table in _PUBLICATION_APPEND_ONLY:
        qualified = f"{schema}.{table}"
        refuse_table_privileges(
            connection,
            None,
            qualified,
            table,
            ("DELETE", "TRUNCATE", "REFERENCES", "TRIGGER"),
            subject=_PUBLICATION_SUBJECT,
        )
        rewrite = connection.execute(
            "SELECT pg_catalog.has_table_privilege(CURRENT_USER, %s, 'UPDATE') AS ok",
            (qualified,),
        ).fetchone()
        if rewrite is None or rewrite["ok"]:
            raise ValueError(f"Voice publisher can rewrite append-only table {table}")
        refuse_column_privileges(
            connection,
            None,
            qualified,
            table,
            ("UPDATE", "REFERENCES"),
            subject=_PUBLICATION_SUBJECT,
        )


def _verify_admission_privileges(connection: SpeechConnection, schema: str) -> None:
    """The admission role must read every frozen fact and write every reservation."""
    for table in _ADMISSION_REQUIRED_SELECT:
        allowed = connection.execute(
            "SELECT pg_catalog.has_table_privilege(CURRENT_USER, %s, 'SELECT') AS ok",
            (f"{schema}.{table}",),
        ).fetchone()
        if allowed is None or not allowed["ok"]:
            raise ValueError("Public Job admission role lacks SELECT on " + table)
    for table, column in (
        ("admission_operations", "request_document"),
        ("admission_attempts", "operation_id"),
        ("admission_leases", "lease_id"),
        ("coordination_jobs", "job_id"),
        ("speech_jobs", "job_id"),
        ("speech_job_intents", "request_document"),
        ("speech_job_submissions", "job_id"),
        ("speech_job_events", "job_id"),
    ):
        allowed = connection.execute(
            "SELECT pg_catalog.has_column_privilege(CURRENT_USER, %s, %s, 'INSERT') AS ok",
            (f"{schema}.{table}", column),
        ).fetchone()
        if allowed is None or not allowed["ok"]:
            raise ValueError(f"Public Job admission role lacks INSERT on {table}.{column}")
    for table, column in (
        ("workspace_quotas", "held_attempts"),
        ("host_nodes", "held_unified_physical_memory_bytes"),
        ("engine_instances", "held_slots"),
    ):
        allowed = connection.execute(
            "SELECT pg_catalog.has_column_privilege(CURRENT_USER, %s, %s, 'UPDATE') AS ok",
            (f"{schema}.{table}", column),
        ).fetchone()
        if allowed is None or not allowed["ok"]:
            raise ValueError(f"Public Job admission role lacks UPDATE on {table}.{column}")
