"""Owner-only meeting fragment, after versions/deletion/sealing/minutes task fragments."""

from psycopg import sql

from speechnexus_backend.workbench.infrastructure.version_schema import schema_identifier


def meetings_migration_sql(schema: str) -> sql.Composed:
    return sql.SQL(_DDL).format(s=schema_identifier(schema))


def grant_meetings_runtime_sql(schema: str, role: str) -> sql.Composed:
    return sql.SQL("""
    GRANT SELECT, INSERT ON {s}.meetings, {s}.meeting_receipts TO {r};
    GRANT UPDATE (version,updated_at,ended_at,sealed_revision,sealed_revision_id,
        source_sha256,minutes_id)
        ON {s}.meetings TO {r};
    """).format(s=schema_identifier(schema), r=sql.Identifier(role))


def grant_meetings_cleanup_sql(schema: str, role: str) -> sql.Composed:
    return sql.SQL(
        "GRANT SELECT, DELETE ON {s}.meetings TO {r}; GRANT UPDATE (id) ON {s}.meetings TO {r}"
    ).format(s=schema_identifier(schema), r=sql.Identifier(role))


_DDL = """
ALTER TABLE {s}.resource_tombstones DROP CONSTRAINT resource_tombstones_resource_kind_check,
    ADD CONSTRAINT tombstones_kind_meetings
    CHECK (resource_kind IN ('project','transcript','meeting','audiobook','saved_asset'));
CREATE TABLE {s}.meetings (
    id text PRIMARY KEY CHECK (char_length(id) BETWEEN 1 AND 128),
    workspace_id text NOT NULL CHECK (char_length(workspace_id) BETWEEN 1 AND 128),
    project_id text NOT NULL REFERENCES {s}.projects(id),
    transcript_id text NOT NULL REFERENCES {s}.transcripts(id) ON DELETE CASCADE,
    display_name text NOT NULL CHECK (char_length(display_name) BETWEEN 1 AND 128),
    save_audio boolean NOT NULL CHECK (save_audio = false),
    started_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL CHECK (updated_at >= started_at),
    version numeric(19,0) NOT NULL CHECK (version BETWEEN 1 AND 9999999999999999999),
    ended_at timestamptz CHECK (ended_at >= started_at AND ended_at <= updated_at),
    sealed_revision numeric(19,0),
    sealed_revision_id text,
    source_sha256 text CHECK (source_sha256 ~ '^sha256:[0-9a-f]{{64}}$'),
    minutes_id text REFERENCES {s}.minutes_tasks(id),
    FOREIGN KEY (transcript_id,sealed_revision)
        REFERENCES {s}.transcript_revisions(transcript_id,revision),
    CHECK ((sealed_revision IS NULL AND sealed_revision_id IS NULL AND source_sha256 IS NULL
        AND ended_at IS NULL AND minutes_id IS NULL) OR
        (sealed_revision IS NOT NULL AND sealed_revision_id IS NOT NULL
        AND source_sha256 IS NOT NULL
        AND ended_at IS NOT NULL))
);
CREATE INDEX meetings_project ON {s}.meetings(project_id,id);
CREATE INDEX meetings_source ON {s}.meetings(transcript_id,id);
CREATE INDEX meetings_minutes ON {s}.meetings(minutes_id);
CREATE TABLE {s}.meeting_receipts (
    workspace_id text NOT NULL,
    operation text NOT NULL CHECK (operation IN ('create','seal','minutes')),
    scope_id text NOT NULL,
    key_digest text NOT NULL CHECK (key_digest ~ '^[0-9a-f]{{64}}$'),
    intent_digest text NOT NULL CHECK (intent_digest ~ '^[0-9a-f]{{64}}$'),
    meeting_id text REFERENCES {s}.meetings(id) ON DELETE SET NULL,
    version numeric(19,0) NOT NULL CHECK (version BETWEEN 1 AND 9999999999999999999),
    updated_at timestamptz NOT NULL,
    ended_at timestamptz,
    sealed_revision numeric(19,0),
    sealed_revision_id text,
    source_sha256 text,
    minutes_id text,
    PRIMARY KEY (workspace_id,operation,scope_id,key_digest)
);
CREATE INDEX meeting_receipts_target ON {s}.meeting_receipts(meeting_id,minutes_id);
"""
