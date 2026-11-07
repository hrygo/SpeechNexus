"""Independent fixed-revision/list reader and draft writer identities.

The reader may only SELECT the two immutable fact tables and the draft writer may
only INSERT revisions plus advance the two A-owned pointers. Neither identity is
granted authority over any other speech table, and startup verifies the actual
role rather than trusting the DSN's name.
"""

from __future__ import annotations

import logging
import threading
import uuid
from collections.abc import Callable

from pydantic import SecretStr

from speechnexus_backend.settings import Settings
from speechnexus_backend.speech.infrastructure.postgres_pool import (
    SpeechConnection,
    SpeechPool,
    speech_pool,
)
from speechnexus_backend.speech.infrastructure.pronunciation_drafts_postgres import (
    PostgresPronunciationDrafts,
)

_FAMILIES = "speech_pronunciation_families"
_REVISIONS = "speech_pronunciation_revisions"
_COMMANDS = "speech_pronunciation_commands"
_PUBLICATIONS = "speech_publications"
_HEADS = "speech_publication_heads"
_CATALOG_STATE = "speech_catalog_state"
_WRITER_TABLES = (_FAMILIES, _REVISIONS, _COMMANDS)
_WRITER_COLUMN_UPDATES = frozenset(
    {
        (_FAMILIES, "latest_revision"),
        (_COMMANDS, "command_state"),
        (_COMMANDS, "pronunciation_id"),
        (_COMMANDS, "revision"),
        (_COMMANDS, "error_code"),
    }
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


def _require_non_owner(connection: SpeechConnection, schema: str, role: str) -> None:
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
        raise ValueError("Pronunciation identities require a non-owner role")


_READER_TABLES = (_FAMILIES, _REVISIONS, _PUBLICATIONS)


def verify_reader_role(connection: SpeechConnection, schema: str, role: str) -> None:
    """SELECT on the two fact tables and the publication ledger; no write anywhere.

    ``speech_publications`` is read-only here: it is what turns the immutable
    revision into a published fact. A column-level grant on any other relation is
    still a privilege, so the comparison uses ``has_any_column_privilege`` rather
    than trusting table-level checks under column grants (#549).
    """
    _require_non_owner(connection, schema, role)
    tables = list(_READER_TABLES)
    extra = connection.execute(
        "SELECT EXISTS (SELECT 1 FROM pg_catalog.pg_class c "
        "JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace "
        "WHERE n.nspname=%s AND c.relkind IN ('r','p','v','m','f') AND ("
        "(c.relname <> ALL(%s::text[]) AND ("
        "pg_catalog.has_table_privilege(%s::name,c.oid,'SELECT') OR "
        "pg_catalog.has_any_column_privilege(%s::name,c.oid,'SELECT') OR "
        "pg_catalog.has_table_privilege(%s::name,c.oid,"
        "'INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER') OR "
        "pg_catalog.has_any_column_privilege(%s::name,c.oid,"
        "'INSERT,UPDATE,REFERENCES'))) OR "
        "(c.relname = ANY(%s::text[]) AND ("
        "pg_catalog.has_table_privilege(%s::name,c.oid,"
        "'INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER') OR "
        "pg_catalog.has_any_column_privilege(%s::name,c.oid,"
        "'INSERT,UPDATE,REFERENCES'))))) AS forbidden",
        (schema, tables, role, role, role, role, tables, role, role),
    ).fetchone()
    permitted = connection.execute(
        "SELECT pg_catalog.has_table_privilege(%s::name,%s,'SELECT') AS revisions, "
        "pg_catalog.has_table_privilege(%s::name,%s,'SELECT') AS families, "
        "pg_catalog.has_table_privilege(%s::name,%s,'SELECT') AS publications",
        (
            role,
            f'"{schema}".{_REVISIONS}',
            role,
            f'"{schema}".{_FAMILIES}',
            role,
            f'"{schema}".{_PUBLICATIONS}',
        ),
    ).fetchone()
    if (
        extra is None
        or extra["forbidden"]
        or permitted is None
        or not permitted["revisions"]
        or not permitted["families"]
        or not permitted["publications"]
    ):
        raise ValueError(
            "Pronunciation reader requires exactly the fact-table and publication SELECTs"
        )


def verify_writer_role(connection: SpeechConnection, schema: str, role: str) -> None:
    """Exactly the three A-owned tables, with only the two declared pointer writes."""
    _require_non_owner(connection, schema, role)
    other = connection.execute(
        "SELECT EXISTS (SELECT 1 FROM pg_catalog.pg_class c "
        "JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace "
        "WHERE n.nspname=%s AND c.relkind IN ('r','p','v','m','f') "
        "AND c.relname <> ALL(%s::text[]) AND ("
        "pg_catalog.has_table_privilege(%s::name,c.oid,"
        "'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER') OR "
        "pg_catalog.has_any_column_privilege(%s::name,c.oid,"
        "'SELECT,INSERT,UPDATE,REFERENCES'))) AS forbidden",
        (schema, list(_WRITER_TABLES), role, role),
    ).fetchone()
    tables = connection.execute(
        "SELECT c.relname AS relname, "
        "pg_catalog.has_table_privilege(%s::name,c.oid,'SELECT') AS can_select, "
        "pg_catalog.has_table_privilege(%s::name,c.oid,'INSERT') AS can_insert, "
        "pg_catalog.has_table_privilege(%s::name,c.oid,'UPDATE') AS can_update, "
        "pg_catalog.has_table_privilege(%s::name,c.oid,"
        "'DELETE,TRUNCATE,REFERENCES,TRIGGER') AS forbidden_table "
        "FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace "
        "WHERE n.nspname=%s AND c.relkind IN ('r','p','v','m','f') "
        "AND c.relname = ANY(%s::text[]) ORDER BY c.relname",
        (role, role, role, role, schema, list(_WRITER_TABLES)),
    ).fetchall()
    columns = connection.execute(
        "SELECT c.relname AS relname, a.attname AS attname FROM pg_catalog.pg_class c "
        "JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace "
        "JOIN pg_catalog.pg_attribute a ON a.attrelid=c.oid AND a.attnum>0 "
        "AND NOT a.attisdropped WHERE n.nspname=%s AND c.relname = ANY(%s::text[]) "
        "AND pg_catalog.has_column_privilege(%s::name,c.oid,a.attname,'UPDATE')",
        (schema, list(_WRITER_TABLES), role),
    ).fetchall()
    observed = {(row["relname"], row["attname"]) for row in columns}
    if other is None or other["forbidden"] or len(tables) != len(_WRITER_TABLES):
        raise ValueError("Pronunciation writer requires exactly the three A-owned tables")
    for row in tables:
        if (
            not row["can_select"]
            or not row["can_insert"]
            or row["can_update"]
            or row["forbidden_table"]
        ):
            raise ValueError("Pronunciation writer requires only revision and pointer INSERTs")
    if observed != _WRITER_COLUMN_UPDATES:
        raise ValueError("Pronunciation writer column UPDATEs drifted")


_PUBLISHER_TABLES = (
    _FAMILIES,
    _REVISIONS,
    _COMMANDS,
    _PUBLICATIONS,
    _HEADS,
    _CATALOG_STATE,
)
_PUBLISHER_INSERT = frozenset({_FAMILIES, _REVISIONS, _COMMANDS, _PUBLICATIONS, _HEADS})
_PUBLISHER_COLUMN_UPDATES = {
    _FAMILIES: frozenset({"latest_revision"}),
    _REVISIONS: frozenset(),
    _COMMANDS: frozenset({"command_state", "pronunciation_id", "revision", "error_code"}),
    _PUBLICATIONS: frozenset(),
    _HEADS: frozenset({"revision", "revision_id", "updated_at_us"}),
    _CATALOG_STATE: frozenset({"revision", "generated_at_us"}),
}


def verify_publisher_role(connection: SpeechConnection, schema: str, role: str) -> None:
    """The atomic-publication identity: exact reads, appends and narrow column writes.

    Publication writes the revision, the ledger row and the selected head in one
    transaction, so this identity necessarily also writes the three A-owned
    tables. It never deletes history, never holds REFERENCES or TRIGGER, never
    receives table-level UPDATE, and cannot INSERT the catalogue counter. Column
    privileges are compared explicitly: ``has_table_privilege`` alone stays false
    under column grants and would turn the check into a no-op (#549).
    """
    _require_non_owner(connection, schema, role)
    tables = list(_PUBLISHER_TABLES)
    other = connection.execute(
        "SELECT EXISTS (SELECT 1 FROM pg_catalog.pg_class c "
        "JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace "
        "WHERE n.nspname=%s AND c.relkind IN ('r','p','v','m','f') "
        "AND c.relname <> ALL(%s::text[]) AND ("
        "pg_catalog.has_table_privilege(%s::name,c.oid,"
        "'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER') OR "
        "pg_catalog.has_any_column_privilege(%s::name,c.oid,"
        "'SELECT,INSERT,UPDATE,REFERENCES'))) AS forbidden",
        (schema, tables, role, role),
    ).fetchone()
    if other is None or other["forbidden"]:
        raise ValueError("Pronunciation publisher requires exactly its declared tables")
    table_rows = connection.execute(
        "SELECT c.relname AS relname, "
        "pg_catalog.has_table_privilege(%s::name,c.oid,'SELECT') AS can_select, "
        "pg_catalog.has_table_privilege(%s::name,c.oid,'INSERT') AS can_insert, "
        "pg_catalog.has_table_privilege(%s::name,c.oid,'UPDATE') AS can_update, "
        "pg_catalog.has_table_privilege(%s::name,c.oid,"
        "'DELETE,TRUNCATE,REFERENCES,TRIGGER') AS destructive, "
        "pg_catalog.has_any_column_privilege(%s::name,c.oid,'REFERENCES') AS column_reference "
        "FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace "
        "WHERE n.nspname=%s AND c.relkind IN ('r','p','v','m','f') "
        "AND c.relname = ANY(%s::text[]) ORDER BY c.relname",
        (role, role, role, role, role, schema, tables),
    ).fetchall()
    columns = connection.execute(
        "SELECT c.relname AS relname, a.attname AS attname FROM pg_catalog.pg_class c "
        "JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace "
        "JOIN pg_catalog.pg_attribute a ON a.attrelid=c.oid AND a.attnum>0 "
        "AND NOT a.attisdropped WHERE n.nspname=%s AND c.relname = ANY(%s::text[]) "
        "AND pg_catalog.has_column_privilege(%s::name,c.oid,a.attname,'UPDATE')",
        (schema, tables, role),
    ).fetchall()
    column_inserts = connection.execute(
        "SELECT c.relname AS relname FROM pg_catalog.pg_class c "
        "JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace "
        "WHERE n.nspname=%s AND c.relname = ANY(%s::text[]) "
        "AND pg_catalog.has_any_column_privilege(%s::name,c.oid,'INSERT') "
        "AND NOT pg_catalog.has_table_privilege(%s::name,c.oid,'INSERT')",
        (schema, tables, role, role),
    ).fetchall()
    observed = {(row["relname"], row["attname"]) for row in columns}
    expected = {
        (table, attribute)
        for table, attributes in _PUBLISHER_COLUMN_UPDATES.items()
        for attribute in attributes
    }
    if len(table_rows) != len(_PUBLISHER_TABLES):
        raise ValueError("Pronunciation publisher table set drifted")
    for row in table_rows:
        table = row["relname"]
        if not row["can_select"]:
            raise ValueError(f"Pronunciation publisher lacks SELECT on {table}")
        if row["can_update"] or row["destructive"] or row["column_reference"]:
            raise ValueError(f"Pronunciation publisher holds a broad privilege on {table}")
        if (table in _PUBLISHER_INSERT) != bool(row["can_insert"]):
            raise ValueError(f"Pronunciation publisher INSERT drifted on {table}")
    if column_inserts:
        raise ValueError("Pronunciation publisher holds a column INSERT outside its tables")
    if observed != expected:
        raise ValueError("Pronunciation publisher column UPDATEs do not match the declared set")


class _PronunciationRuntime:
    """One bounded pool plus the exact role check for its declared identity."""

    _label = "pronunciation"
    _include_publication = False

    def __init__(
        self,
        settings: Settings,
        *,
        database_url: SecretStr | None,
        concurrency: int,
        timeout_seconds: float,
        verify: Callable[[SpeechConnection, str, str], None],
    ) -> None:
        if settings.process_role != "api":
            raise ValueError(f"Configure an explicit API {self._label} identity")
        if database_url is None:
            raise ValueError(f"Configure an explicit API {self._label} identity")
        self._settings = settings
        assert database_url is not None
        self._secret: SecretStr = database_url
        self._concurrency = concurrency
        self._timeout = timeout_seconds
        self._verify = verify
        self._pool: SpeechPool | None = None
        self.provider: PostgresPronunciationDrafts | None = None
        self._lifetime = threading.Lock()
        self._diagnostics: _PoolDiagnostics | None = None

    def open(self) -> None:
        with self._lifetime:
            if self.provider is not None:
                raise ValueError(f"{self._label} identity is already open")
            name = f"speech-{self._label}-" + uuid.uuid4().hex
            diagnostic = _PoolDiagnostics(name)
            logger = logging.getLogger("psycopg.pool")
            logger.addFilter(diagnostic)
            self._diagnostics = diagnostic
            self._pool = None
            try:
                pool = speech_pool(
                    self._secret.get_secret_value(),
                    schema=self._settings.database_schema,
                    max_size=self._concurrency,
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
                        raise ValueError("Pronunciation identity unavailable")
                    for role in dict.fromkeys((identities["login"], identities["effective"])):
                        self._verify(connection, self._settings.database_schema, role)
                self.provider = PostgresPronunciationDrafts(
                    pool,
                    timeout_seconds=self._timeout,
                    include_publication=self._include_publication,
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
                    f"Pronunciation {self._label} startup rejected; verify migration and "
                    "minimum role"
                ) from None

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


class PronunciationRuntime(_PronunciationRuntime):
    _label = "reader"
    _include_publication = True

    def __init__(self, settings: Settings) -> None:
        super().__init__(
            settings,
            database_url=settings.pronunciation_reader_database_url,
            concurrency=settings.pronunciation_read_concurrency,
            timeout_seconds=settings.pronunciation_read_timeout_seconds,
            verify=verify_reader_role,
        )


class PronunciationWriterRuntime(_PronunciationRuntime):
    _label = "writer"

    def __init__(self, settings: Settings) -> None:
        super().__init__(
            settings,
            database_url=settings.pronunciation_writer_database_url,
            concurrency=settings.pronunciation_write_concurrency,
            timeout_seconds=settings.pronunciation_transaction_timeout_seconds,
            verify=verify_writer_role,
        )


class PronunciationPublisherRuntime(_PronunciationRuntime):
    _label = "publisher"

    def __init__(self, settings: Settings) -> None:
        super().__init__(
            settings,
            database_url=settings.pronunciation_publisher_database_url,
            concurrency=settings.pronunciation_write_concurrency,
            timeout_seconds=settings.pronunciation_transaction_timeout_seconds,
            verify=verify_publisher_role,
        )
