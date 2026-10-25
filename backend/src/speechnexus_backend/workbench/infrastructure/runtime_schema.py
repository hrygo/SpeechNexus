"""Fresh C schema baseline; only an owner migration transaction may execute it.

Team A assigns the global migration order. This composes unchanged C fragments;
it is not an idempotent runner, an upgrade of an existing schema, or runtime DDL.
"""

from psycopg import sql

from speechnexus_backend.workbench.infrastructure.assistant_schema import (
    assistant_migration_sql,
    grant_assistant_runtime_sql,
)
from speechnexus_backend.workbench.infrastructure.books_schema import (
    books_migration_sql,
    grant_books_runtime_sql,
)
from speechnexus_backend.workbench.infrastructure.deletion_schema import (
    deletion_migration_sql,
    grant_cleanup_runtime_sql,
    grant_deletion_runtime_sql,
)
from speechnexus_backend.workbench.infrastructure.exports_schema import (
    exports_migration_sql,
    grant_exports_runtime_sql,
)
from speechnexus_backend.workbench.infrastructure.meetings_schema import (
    grant_meetings_runtime_sql,
    meetings_migration_sql,
)
from speechnexus_backend.workbench.infrastructure.minutes_tasks_schema import (
    grant_minutes_runtime_sql,
    minutes_tasks_migration_sql,
)
from speechnexus_backend.workbench.infrastructure.render_units_schema import (
    grant_render_units_runtime_sql,
    render_units_migration_sql,
)
from speechnexus_backend.workbench.infrastructure.saved_assets_schema import (
    grant_saved_assets_cleanup_sql,
    grant_saved_assets_runtime_sql,
    saved_assets_migration_sql,
)
from speechnexus_backend.workbench.infrastructure.sealing_schema import sealing_migration_sql
from speechnexus_backend.workbench.infrastructure.teleprompter_schema import (
    grant_teleprompter_runtime_sql,
    teleprompter_migration_sql,
)
from speechnexus_backend.workbench.infrastructure.version_schema import (
    grant_runtime_sql,
    migration_sql,
    schema_identifier,
)

SCHEMA_REVISION = 10
TABLES = frozenset(
    {
        "projects",
        "project_versions",
        "transcripts",
        "transcript_revisions",
        "mutation_receipts",
        "deletion_plans",
        "deletion_records",
        "resource_tombstones",
        "deletion_outbox",
        "deletion_receipts",
        "erased_mutation_receipts",
        "deletion_ledger",
        "minutes_intents",
        "minutes_tasks",
        "minutes_attempts",
        "minutes_request_keys",
        "minutes_retry_keys",
        "meetings",
        "meeting_receipts",
        "teleprompter_states",
        "teleprompter_receipts",
        "saved_assets",
        "saved_asset_receipts",
        "books",
        "chapters",
        "book_receipts",
        "render_units",
        "render_unit_receipts",
        "render_unit_generations",
        "assistant_turns",
        "assistant_turn_receipts",
        "export_jobs",
        "export_job_receipts",
        "runtime_schema_revision",
    }
)


def _namespace(schema: str) -> sql.Identifier:
    identity = schema_identifier(schema)
    if schema == "speech":
        raise ValueError("workbench requires its own namespace")
    return identity


def bootstrap_sql(schema: str) -> sql.Composed:
    """Caller executes this exactly once, inside an owner transaction, on fresh schema."""
    marker = sql.SQL("""
        CREATE TABLE {s}.runtime_schema_revision (
            singleton boolean PRIMARY KEY CHECK (singleton),
            revision integer NOT NULL CHECK (revision >= 1)
        );
        INSERT INTO {s}.runtime_schema_revision VALUES (true, {revision});
    """).format(s=_namespace(schema), revision=sql.Literal(SCHEMA_REVISION))
    return sql.SQL(";\n").join(
        (
            migration_sql(schema),
            deletion_migration_sql(schema),
            sealing_migration_sql(schema),
            minutes_tasks_migration_sql(schema),
            meetings_migration_sql(schema),
            teleprompter_migration_sql(schema),
            saved_assets_migration_sql(schema),
            books_migration_sql(schema),
            render_units_migration_sql(schema),
            assistant_migration_sql(schema),
            exports_migration_sql(schema),
            marker,
        )
    )


def cleanup_grants_sql(schema: str, role: str) -> sql.Composed:
    """The non-owner erasing worker: ledger writes plus the reads its sweep needs."""
    return sql.SQL(";\n").join(
        (
            grant_cleanup_runtime_sql(schema, role),
            grant_saved_assets_cleanup_sql(schema, role),
        )
    )


def runtime_grants_sql(schema: str, role: str) -> sql.Composed:
    """Existing non-owner app role; no cleanup/DDL or schema marker mutation grant."""
    return sql.SQL(";\n").join(
        (
            grant_runtime_sql(schema, role),
            grant_deletion_runtime_sql(schema, role),
            grant_minutes_runtime_sql(schema, role),
            grant_meetings_runtime_sql(schema, role),
            grant_teleprompter_runtime_sql(schema, role),
            grant_saved_assets_runtime_sql(schema, role),
            grant_books_runtime_sql(schema, role),
            grant_render_units_runtime_sql(schema, role),
            grant_assistant_runtime_sql(schema, role),
            grant_exports_runtime_sql(schema, role),
            sql.SQL("GRANT SELECT ON {}.runtime_schema_revision TO {}").format(
                _namespace(schema), sql.Identifier(role)
            ),
        )
    )
