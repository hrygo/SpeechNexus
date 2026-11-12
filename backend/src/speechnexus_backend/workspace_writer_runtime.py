"""Minimum workspace writer identity for the two administrative writes.

Reads keep using the existing asset runtime role, which may only increment
``metadata_revision`` and must not change a group's display name or state; the
asset role's negative tests assert exactly that. Accepting create/update under
it would silently widen that authority, so writes use a separately granted
non-owner login.

Startup verifies the granted surface instead of trusting the configured DSN
name: schema ``USAGE`` and no ``CREATE``, exactly the two declared relations, no
privilege on any other relation of the schema, column-level ``INSERT``/
``UPDATE`` sets equal to the declared ones (a table-level grant is refused
because it would silently widen when a later revision adds a column) and no
``DELETE``/``TRUNCATE``/``REFERENCES``/``TRIGGER``. A mis-scoped login fails the
process instead of failing a request, and this runtime never migrates or grants.
"""

from __future__ import annotations

import logging
import threading
import uuid

from speechnexus_backend.settings import Settings
from speechnexus_backend.speech.domain.workspaces import (
    WorkspaceCreateCommand,
    WorkspaceUnavailableError,
    WorkspaceUpdateCommand,
    WorkspaceWriteReceipt,
)
from speechnexus_backend.speech.infrastructure.postgres_pool import (
    SpeechConnection,
    SpeechPool,
    speech_pool,
)
from speechnexus_backend.speech.infrastructure.workspace_schema import (
    COMMAND_INSERT_COLUMNS,
    COMMAND_TABLE,
    WORKSPACE_INSERT_COLUMNS,
    WORKSPACE_TABLE,
    WORKSPACE_UPDATE_COLUMNS,
)
from speechnexus_backend.speech.infrastructure.workspaces_postgres import (
    PostgresWorkspaceWrites,
)
from speechnexus_backend.startup_privileges import (
    refuse_column_privileges,
    refuse_foreign_relation_privileges,
    refuse_schema_create,
    refuse_table_privileges,
    require_schema_usage,
)

SUBJECT = "Workspace writer"
DECLARED = (WORKSPACE_TABLE, COMMAND_TABLE)
INSERT_COLUMNS = {WORKSPACE_TABLE: WORKSPACE_INSERT_COLUMNS, COMMAND_TABLE: COMMAND_INSERT_COLUMNS}
UPDATE_COLUMNS = {WORKSPACE_TABLE: WORKSPACE_UPDATE_COLUMNS, COMMAND_TABLE: ()}
REFUSED_TABLE_PRIVILEGES = ("INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER")
TIMEOUT_SECONDS = 3.0


class _PoolDiagnostics(logging.Filter):
    def __init__(self, pool_name: str) -> None:
        super().__init__()
        self._pool_name = pool_name

    def filter(self, record: logging.LogRecord) -> bool:
        if self._pool_name in record.getMessage():
            record.msg, record.args = "Workspace writer database connection unavailable", ()
            record.exc_info = record.exc_text = record.stack_info = None
        return True


class WorkspaceWriterRuntime:
    """Own the writer pool; the same object is the service's write port.

    Before ``open`` and after ``close`` every command reports unavailable, so a
    shutdown never looks like an accepted write.
    """

    def __init__(self, settings: Settings) -> None:
        if settings.workspace_writer_database_url is None or settings.process_role != "api":
            raise ValueError("Configure an explicit API workspace writer identity")
        self._settings = settings
        self._pool: SpeechPool | None = None
        self.writes: PostgresWorkspaceWrites | None = None
        self._lifetime = threading.Lock()
        self._diagnostics: _PoolDiagnostics | None = None

    def open(self) -> None:
        with self._lifetime:
            if self.writes is not None:
                raise ValueError("Workspace writer is already open")
            secret = self._settings.workspace_writer_database_url
            assert secret is not None
            name = "speech-workspace-writer-" + uuid.uuid4().hex
            diagnostic = _PoolDiagnostics(name)
            logger = logging.getLogger("psycopg.pool")
            logger.addFilter(diagnostic)
            self._diagnostics = diagnostic
            try:
                pool = speech_pool(
                    secret.get_secret_value(),
                    schema=self._settings.database_schema,
                    max_size=2,
                    name=name,
                )
                self._pool = pool
                pool.open()
                with pool.connection(timeout=3) as connection:
                    connection.execute("SET LOCAL statement_timeout='3s'")
                    self._verify(connection)
                self.writes = PostgresWorkspaceWrites(pool, timeout_seconds=TIMEOUT_SECONDS)
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
                    "Workspace writer startup rejected; verify migration and writer role"
                ) from None

    def _verify(self, connection: SpeechConnection) -> None:
        schema = self._settings.database_schema
        require_schema_usage(connection, None, schema, subject=SUBJECT)
        refuse_schema_create(connection, None, schema, subject=SUBJECT)
        refuse_foreign_relation_privileges(
            connection,
            None,
            schema,
            DECLARED,
            subject=SUBJECT,
            declared_hint="DECLARED",
        )
        for table in DECLARED:
            qualified = f'"{schema}"."{table}"'
            held = connection.execute(
                "SELECT pg_catalog.has_table_privilege(CURRENT_USER, %s, 'SELECT') AS held",
                (qualified,),
            ).fetchone()
            if held is None or not held["held"]:
                raise ValueError(f"{SUBJECT} lacks SELECT on {table}; grant SELECT on that table")
            refuse_table_privileges(
                connection,
                None,
                qualified,
                table,
                REFUSED_TABLE_PRIVILEGES,
                subject=SUBJECT,
                repair=(
                    "revoke the table-level grant, then grant only the declared INSERT "
                    "and UPDATE columns"
                ),
            )
            refuse_column_privileges(
                connection, None, qualified, table, ("REFERENCES",), subject=SUBJECT
            )
            self._exact_columns(connection, schema, table, "INSERT", INSERT_COLUMNS[table])
            self._exact_columns(connection, schema, table, "UPDATE", UPDATE_COLUMNS[table])

    def _exact_columns(
        self,
        connection: SpeechConnection,
        schema: str,
        table: str,
        privilege: str,
        declared: tuple[str, ...],
    ) -> None:
        rows = connection.execute(
            "SELECT column_name, pg_catalog.has_column_privilege(CURRENT_USER, %s, "
            "column_name, %s) AS held FROM information_schema.columns "
            "WHERE table_schema=%s AND table_name=%s",
            (f'"{schema}"."{table}"', privilege, schema, table),
        ).fetchall()
        granted = {row["column_name"] for row in rows if row["held"]}
        if granted != set(declared):
            raise ValueError(
                f"{SUBJECT} {privilege} columns do not match the declared surface on "
                f"{table}; revoke the extra column grant or restore the declared column"
            )

    def create(
        self, command: WorkspaceCreateCommand, *, workspace_id: str
    ) -> WorkspaceWriteReceipt:
        writes = self.writes
        if writes is None:
            raise WorkspaceUnavailableError
        return writes.create(command, workspace_id=workspace_id)

    def update(self, command: WorkspaceUpdateCommand) -> WorkspaceWriteReceipt:
        writes = self.writes
        if writes is None:
            raise WorkspaceUnavailableError
        return writes.update(command)

    def close(self) -> None:
        with self._lifetime:
            self.writes = None
            if self._pool is not None:
                self._pool.close()
                self._pool = None
            if self._diagnostics is not None:
                logging.getLogger("psycopg.pool").removeFilter(self._diagnostics)
                self._diagnostics = None
