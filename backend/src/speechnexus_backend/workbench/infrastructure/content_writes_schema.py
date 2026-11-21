"""Private content-write identities survive project erasure; no runtime DDL."""

from psycopg import sql

from speechnexus_backend.workbench.infrastructure.version_schema import schema_identifier


def content_writes_migration_sql(schema: str) -> sql.Composed:
    return sql.SQL("""
        CREATE TABLE {s}.content_write_intents (
            id text PRIMARY KEY,
            project_id text NOT NULL,
            resource_kind text NOT NULL CHECK (resource_kind IN ('saved_asset', 'book_export')),
            resource_id text NOT NULL,
            token text NOT NULL,
            key_digest text CHECK (key_digest ~ '^[0-9a-f]{{64}}$'),
            intent_digest text NOT NULL CHECK (intent_digest ~ '^[0-9a-f]{{64}}$'),
            intent jsonb CHECK (intent IS NULL OR
                (jsonb_typeof(intent)='object' AND octet_length(intent::text)<=8192)),
            state text NOT NULL CHECK (state IN ('reserved','writing','unknown','settled')),
            object_key text CHECK (length(object_key) BETWEEN 1 AND 512),
            sha256 text CHECK (sha256 ~ '^sha256:[0-9a-f]{{64}}$'),
            bytes numeric(19,0) CHECK (bytes BETWEEN 1 AND 9007199254740991),
            created_at timestamptz NOT NULL,
            updated_at timestamptz NOT NULL CHECK (updated_at >= created_at),
            CHECK ((object_key IS NULL)=(sha256 IS NULL) AND (object_key IS NULL)=(bytes IS NULL)),
            CHECK (state NOT IN ('writing','unknown') OR object_key IS NOT NULL),
            UNIQUE (project_id, resource_kind, key_digest)
        );
        CREATE INDEX content_writes_pending ON {s}.content_write_intents(project_id, id)
            WHERE state <> 'settled';
    """).format(s=schema_identifier(schema))


def grant_content_writes_runtime_sql(schema: str, role: str) -> sql.Composed:
    return sql.SQL("""
        GRANT SELECT, INSERT ON {s}.content_write_intents TO {r};
        GRANT UPDATE (token, intent, state, object_key, sha256, bytes, updated_at)
            ON {s}.content_write_intents TO {r};
    """).format(s=schema_identifier(schema), r=sql.Identifier(role))


def grant_content_writes_cleanup_sql(schema: str, role: str) -> sql.Composed:
    return sql.SQL("GRANT SELECT ON {s}.content_write_intents TO {r}").format(
        s=schema_identifier(schema), r=sql.Identifier(role)
    )
