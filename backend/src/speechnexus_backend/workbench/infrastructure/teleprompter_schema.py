"""Owner-only teleprompter fragment, after the versions/deletion/meetings fragments.

Position is orchestration state, not audiovisual content: it stores no manuscript text,
no hypothesis and no stream epoch. Project deletion cascades both tables so a tombstoned
project never leaves a readable position behind.
"""

from psycopg import sql

from speechnexus_backend.workbench.infrastructure.version_schema import schema_identifier


def teleprompter_migration_sql(schema: str) -> sql.Composed:
    return sql.SQL(_DDL).format(s=schema_identifier(schema))


def grant_teleprompter_runtime_sql(schema: str, role: str) -> sql.Composed:
    return sql.SQL("""
    GRANT SELECT, INSERT ON {s}.teleprompter_states, {s}.teleprompter_receipts TO {r};
    GRANT UPDATE (script_revision,visible_position,manual_hold,updated_at)
        ON {s}.teleprompter_states TO {r};
    """).format(s=schema_identifier(schema), r=sql.Identifier(role))


_DDL = """
CREATE TABLE {s}.teleprompter_states (
    project_id text PRIMARY KEY REFERENCES {s}.projects(id) ON DELETE CASCADE,
    script_revision numeric(19,0) NOT NULL CHECK (
        script_revision BETWEEN 1 AND 9999999999999999999),
    visible_position numeric(19,0) NOT NULL CHECK (
        visible_position BETWEEN 0 AND 9007199254740991),
    manual_hold boolean NOT NULL,
    updated_at timestamptz NOT NULL
);
CREATE TABLE {s}.teleprompter_receipts (
    project_id text NOT NULL REFERENCES {s}.projects(id) ON DELETE CASCADE,
    key_digest text NOT NULL CHECK (key_digest ~ '^[0-9a-f]{{64}}$'),
    intent_digest text NOT NULL CHECK (intent_digest ~ '^[0-9a-f]{{64}}$'),
    script_revision numeric(19,0) NOT NULL CHECK (
        script_revision BETWEEN 1 AND 9999999999999999999),
    visible_position numeric(19,0) NOT NULL CHECK (
        visible_position BETWEEN 0 AND 9007199254740991),
    manual_hold boolean NOT NULL,
    updated_at timestamptz NOT NULL,
    PRIMARY KEY (project_id, key_digest)
);
"""
