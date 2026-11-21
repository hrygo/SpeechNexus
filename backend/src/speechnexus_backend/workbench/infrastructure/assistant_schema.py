"""Owner-only assistant-turn fragment, after the project row it belongs to.

A turn pins the voice it speaks with and the text it was asked about; only its
progress, result and error are mutable afterwards. The LLM itself stays optional:
this fragment never records an endpoint or credential.
"""

from psycopg import sql

from speechnexus_backend.workbench.infrastructure.version_schema import schema_identifier

# The exact mutable column set, shared by the grant below and the startup check.
UPDATABLE_COLUMNS = frozenset(
    {
        "error",
        "output_text",
        "playback_generation",
        "revision",
        "speech_job_id",
        "status",
        "updated_at",
    }
)


def assistant_migration_sql(schema: str) -> sql.Composed:
    return sql.SQL(_DDL).format(s=schema_identifier(schema))


def grant_assistant_runtime_sql(schema: str, role: str) -> sql.Composed:
    columns = sql.SQL(", ").join(sql.Identifier(name) for name in sorted(UPDATABLE_COLUMNS))
    return sql.SQL("""
    GRANT SELECT, INSERT ON {s}.assistant_turns, {s}.assistant_turn_receipts TO {r};
    GRANT UPDATE ({columns}) ON {s}.assistant_turns TO {r};
    """).format(s=schema_identifier(schema), r=sql.Identifier(role), columns=columns)


_DDL = """
CREATE TABLE {s}.assistant_turns (
    id text PRIMARY KEY CHECK (char_length(id) BETWEEN 1 AND 128),
    project_id text NOT NULL REFERENCES {s}.projects(id) ON DELETE CASCADE,
    status text NOT NULL CHECK (status IN
        ('queued','running','succeeded','failed','cancelled')),
    revision numeric(19,0) NOT NULL CHECK (revision BETWEEN 1 AND 9999999999999999999),
    input_text text NOT NULL CHECK (char_length(input_text) BETWEEN 1 AND 20000),
    output_text text CHECK (char_length(output_text) BETWEEN 1 AND 20000),
    voice_id text NOT NULL CHECK (char_length(voice_id) BETWEEN 1 AND 128),
    voice_revision numeric(19,0) NOT NULL CHECK (voice_revision > 0),
    model text CHECK (char_length(model) BETWEEN 1 AND 128),
    profile_id text CHECK (char_length(profile_id) BETWEEN 1 AND 128),
    profile_revision numeric(19,0) CHECK (profile_revision > 0),
    speech_job_id text CHECK (char_length(speech_job_id) BETWEEN 1 AND 128),
    playback_generation integer NOT NULL CHECK (playback_generation >= 1),
    error jsonb CHECK (error IS NULL OR (jsonb_typeof(error) = 'object'
        AND octet_length(error::text) <= 4000)),
    created_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL CHECK (updated_at >= created_at),
    UNIQUE (project_id, id),
    CHECK ((profile_id IS NULL) = (profile_revision IS NULL))
);
CREATE TABLE {s}.assistant_turn_receipts (
    scope_id text NOT NULL,
    key_digest text NOT NULL CHECK (key_digest ~ '^[0-9a-f]{{64}}$'),
    intent_digest text NOT NULL CHECK (intent_digest ~ '^[0-9a-f]{{64}}$'),
    turn_id text NOT NULL,
    created_at timestamptz NOT NULL,
    PRIMARY KEY (scope_id, key_digest),
    FOREIGN KEY (scope_id, turn_id)
        REFERENCES {s}.assistant_turns(project_id, id) ON DELETE CASCADE
);
"""
