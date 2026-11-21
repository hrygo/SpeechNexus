"""Owner-only export-job fragment, after the book whose manifest it pins.

An export names the casting revision it was accepted against, so a later casting
change can never silently alter what a running or finished export produced.
"""

from psycopg import sql

from speechnexus_backend.workbench.infrastructure.version_schema import schema_identifier

# The exact mutable column set, shared by the grant below and the startup check.
UPDATABLE_COLUMNS = frozenset(
    {"artifact", "error", "progress_completed", "progress_total", "status", "updated_at"}
)


def exports_migration_sql(schema: str) -> sql.Composed:
    return sql.SQL(_DDL).format(s=schema_identifier(schema))


def grant_exports_runtime_sql(schema: str, role: str) -> sql.Composed:
    columns = sql.SQL(", ").join(sql.Identifier(name) for name in sorted(UPDATABLE_COLUMNS))
    return sql.SQL("""
    GRANT SELECT, INSERT ON {s}.export_jobs, {s}.export_job_receipts TO {r};
    GRANT UPDATE ({columns}) ON {s}.export_jobs TO {r};
    """).format(s=schema_identifier(schema), r=sql.Identifier(role), columns=columns)


_DDL = """
CREATE TABLE {s}.export_jobs (
    id text PRIMARY KEY CHECK (char_length(id) BETWEEN 1 AND 128),
    project_id text NOT NULL REFERENCES {s}.projects(id) ON DELETE CASCADE,
    book_id text NOT NULL REFERENCES {s}.books(id) ON DELETE CASCADE,
    status text NOT NULL CHECK (status IN
        ('queued','running','cancelling','succeeded','failed','cancelled','expired')),
    export_format text NOT NULL CHECK (export_format IN ('wav','mp3','zip')),
    manifest_revision numeric(19,0) NOT NULL
        CHECK (manifest_revision BETWEEN 1 AND 9999999999999999999),
    progress_completed integer NOT NULL CHECK (progress_completed >= 0),
    progress_total integer NOT NULL CHECK (progress_total >= 1),
    unit_ids jsonb CHECK (unit_ids IS NULL OR (jsonb_typeof(unit_ids) = 'array'
        AND jsonb_array_length(unit_ids) >= 1 AND octet_length(unit_ids::text) <= 20000)),
    artifact jsonb CHECK (artifact IS NULL OR (jsonb_typeof(artifact) = 'object'
        AND octet_length(artifact::text) <= 4000)),
    error jsonb CHECK (error IS NULL OR (jsonb_typeof(error) = 'object'
        AND octet_length(error::text) <= 4000)),
    created_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL CHECK (updated_at >= created_at),
    UNIQUE (project_id, id),
    CHECK (progress_completed <= progress_total),
    CHECK ((status = 'failed') = (error IS NOT NULL)),
    CHECK ((status = 'succeeded') = (artifact IS NOT NULL))
);
CREATE TABLE {s}.export_job_receipts (
    scope_id text NOT NULL,
    key_digest text NOT NULL CHECK (key_digest ~ '^[0-9a-f]{{64}}$'),
    intent_digest text NOT NULL CHECK (intent_digest ~ '^[0-9a-f]{{64}}$'),
    export_id text NOT NULL,
    created_at timestamptz NOT NULL,
    PRIMARY KEY (scope_id, key_digest),
    FOREIGN KEY (scope_id, export_id)
        REFERENCES {s}.export_jobs(project_id, id) ON DELETE CASCADE
);
"""
