"""Owner-only saved-asset fragment, after the versions/deletion/meetings fragments.

A saved asset is the workbench's own committed copy of a base artifact: it stores the
pinned source identity, the verified digest and the private object key, never artifact
bytes, a public URL or a second copy of speech state. Project deletion cascades both
tables, and object bytes stay discoverable under the project's object prefix.
"""

from psycopg import sql

from speechnexus_backend.workbench.infrastructure.version_schema import schema_identifier


def saved_assets_migration_sql(schema: str) -> sql.Composed:
    return sql.SQL(_DDL).format(s=schema_identifier(schema))


def grant_saved_assets_runtime_sql(schema: str, role: str) -> sql.Composed:
    # Immutable rows: the runtime may insert and read, and may never update or delete.
    return sql.SQL("""
    GRANT SELECT, INSERT ON {s}.saved_assets, {s}.saved_asset_receipts TO {r};
    """).format(s=schema_identifier(schema), r=sql.Identifier(role))


def grant_saved_assets_cleanup_sql(schema: str, role: str) -> sql.Composed:
    # The erasing worker only reads which objects belong to a deleted project.
    return sql.SQL("""
    GRANT SELECT ON {s}.saved_assets TO {r};
    """).format(s=schema_identifier(schema), r=sql.Identifier(role))


_DDL = """
CREATE TABLE {s}.saved_assets (
    id text PRIMARY KEY CHECK (id ~ '^[a-z0-9][a-z0-9._:-]{{1,126}}[a-z0-9]$'),
    project_id text NOT NULL REFERENCES {s}.projects(id) ON DELETE CASCADE,
    media_type text NOT NULL CHECK (media_type <> '' AND length(media_type) <= 128),
    bytes numeric(19,0) NOT NULL CHECK (bytes BETWEEN 1 AND 9007199254740991),
    sha256 text NOT NULL CHECK (sha256 ~ '^sha256:[0-9a-f]{{64}}$'),
    source_artifact_id text NOT NULL CHECK (
        source_artifact_id ~ '^[a-z0-9][a-z0-9._:-]{{1,126}}[a-z0-9]$'),
    source_receipt_id text NOT NULL CHECK (
        source_receipt_id ~ '^[a-z0-9][a-z0-9._:-]{{1,126}}[a-z0-9]$'),
    display_name text CHECK (
        display_name IS NULL OR (display_name <> '' AND length(display_name) <= 128)),
    object_key text NOT NULL CHECK (length(object_key) BETWEEN 1 AND 256),
    created_at timestamptz NOT NULL,
    UNIQUE (id, project_id)
);
CREATE INDEX saved_assets_project_idx ON {s}.saved_assets (project_id);
CREATE TABLE {s}.saved_asset_receipts (
    project_id text NOT NULL REFERENCES {s}.projects(id) ON DELETE CASCADE,
    key_digest text NOT NULL CHECK (key_digest ~ '^[0-9a-f]{{64}}$'),
    intent_digest text NOT NULL CHECK (intent_digest ~ '^[0-9a-f]{{64}}$'),
    asset_id text NOT NULL REFERENCES {s}.saved_assets(id) ON DELETE CASCADE,
    created_at timestamptz NOT NULL,
    PRIMARY KEY (project_id, key_digest)
);
"""
