"""Owner-only render-unit fragment, after the book/chapter fragment.

A render unit freezes the provenance of one narration segment: the chapter text
revision it was derived from, the voice and model it pins, and the model binding the
deployment published. Pins are columns rather than parts of an opaque blob so a
partial redo can be planned from stored state alone. Project deletion cascades units
and their receipts, a unit position is unique inside its chapter, and the receipt's
composite key binds the key that was accepted to the chapter that accepted it.
"""

from psycopg import sql

from speechnexus_backend.workbench.infrastructure.version_schema import schema_identifier


def render_units_migration_sql(schema: str) -> sql.Composed:
    return sql.SQL(_DDL).format(s=schema_identifier(schema))


def grant_render_units_runtime_sql(schema: str, role: str) -> sql.Composed:
    # Units are revised in place (text, pins, status) but never deleted by the app:
    # removal follows project deletion, so no DELETE grant is issued.
    return sql.SQL("""
    GRANT SELECT, INSERT ON {s}.render_units, {s}.render_unit_receipts,
        {s}.render_unit_generations TO {r};
    GRANT UPDATE ({columns}) ON {s}.render_units TO {r};
    """).format(
        s=schema_identifier(schema),
        r=sql.Identifier(role),
        columns=sql.SQL(", ").join(sql.SQL(column) for column in sorted(UPDATABLE_COLUMNS)),
    )


# The runtime preflight asserts exact equality between these columns and the granted
# UPDATE column list, so this set is the single source of truth for both.
UPDATABLE_COLUMNS = frozenset(
    {
        "dependencies_sha256",
        "model",
        "model_binding_id",
        "model_binding_revision",
        "revision",
        "saved_asset_id",
        "source_artifact_id",
        "source_receipt_id",
        "speech_job_id",
        "status",
        "text",
        "updated_at",
        "voice_id",
        "voice_revision",
    }
)


_DDL = """
CREATE TABLE {s}.render_units (
    id text PRIMARY KEY CHECK (id ~ '^[a-z0-9][a-z0-9._:-]{{1,126}}[a-z0-9]$'),
    project_id text NOT NULL REFERENCES {s}.projects(id) ON DELETE CASCADE,
    book_id text NOT NULL,
    chapter_id text NOT NULL,
    ordinal numeric(19,0) NOT NULL CHECK (ordinal BETWEEN 0 AND 9999999999999999999),
    revision numeric(19,0) NOT NULL CHECK (revision BETWEEN 1 AND 9999999999999999999),
    status text NOT NULL CHECK (status IN ('pending', 'generating', 'ready', 'stale', 'failed')),
    text text NOT NULL CHECK (text <> '' AND length(text) <= 200000),
    voice_id text NOT NULL CHECK (voice_id ~ '^[a-z0-9][a-z0-9._:-]{{1,126}}[a-z0-9]$'),
    voice_revision numeric(19,0) NOT NULL CHECK (voice_revision BETWEEN 1 AND 9999999999999999999),
    model text NOT NULL CHECK (model <> '' AND length(model) <= 128),
    model_binding_id text NOT NULL
        CHECK (model_binding_id ~ '^[a-z0-9][a-z0-9._:-]{{1,126}}[a-z0-9]$'),
    model_binding_revision numeric(19,0) NOT NULL
        CHECK (model_binding_revision BETWEEN 1 AND 9999999999999999999),
    role_id text NOT NULL CHECK (role_id <> '' AND length(role_id) <= 128),
    text_plan_revision numeric(19,0) NOT NULL
        CHECK (text_plan_revision BETWEEN 1 AND 9999999999999999999),
    original_revision numeric(19,0) NOT NULL
        CHECK (original_revision BETWEEN 1 AND 9999999999999999999),
    working_revision numeric(19,0) NOT NULL
        CHECK (working_revision BETWEEN 1 AND 9999999999999999999),
    pronunciation_id text,
    pronunciation_revision numeric(19,0),
    profile_id text,
    profile_revision numeric(19,0),
    dependencies_sha256 text NOT NULL CHECK (dependencies_sha256 ~ '^sha256:[0-9a-f]{{64}}$'),
    speech_job_id text,
    saved_asset_id text,
    source_artifact_id text,
    source_receipt_id text,
    created_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL,
    CHECK (updated_at >= created_at),
    CHECK (
        (pronunciation_id IS NULL AND pronunciation_revision IS NULL)
        OR (pronunciation_id IS NOT NULL AND pronunciation_revision IS NOT NULL)
    ),
    CHECK (
        (profile_id IS NULL AND profile_revision IS NULL)
        OR (profile_id IS NOT NULL AND profile_revision IS NOT NULL)
    ),
    UNIQUE (id, project_id),
    UNIQUE (chapter_id, ordinal),
    UNIQUE (chapter_id, id),
    FOREIGN KEY (book_id, chapter_id) REFERENCES {s}.chapters (book_id, id) ON DELETE CASCADE
);
CREATE TABLE {s}.render_unit_generations (
    id text PRIMARY KEY CHECK (id ~ '^[a-z0-9][a-z0-9._:-]{{1,126}}[a-z0-9]$'),
    unit_id text NOT NULL REFERENCES {s}.render_units(id) ON DELETE CASCADE,
    job_id text NOT NULL CHECK (job_id <> '' AND length(job_id) <= 128),
    dependencies_sha256 text NOT NULL CHECK (dependencies_sha256 ~ '^sha256:[0-9a-f]{{64}}$'),
    revision numeric(19,0) NOT NULL CHECK (revision BETWEEN 1 AND 9999999999999999999),
    created_at timestamptz NOT NULL,
    -- A dispatch identity is retained after edits so it can never be reused for a
    -- different dependency set: both the unit-visible id and the revision are unique.
    UNIQUE (unit_id, id),
    UNIQUE (unit_id, revision)
);
CREATE INDEX render_unit_generations_unit_idx ON {s}.render_unit_generations (unit_id, revision);
CREATE INDEX render_units_project_idx ON {s}.render_units (project_id);
CREATE INDEX render_units_chapter_idx ON {s}.render_units (chapter_id, ordinal);
CREATE TABLE {s}.render_unit_receipts (
    scope_id text NOT NULL CHECK (scope_id ~ '^[a-z0-9][a-z0-9._:-]{{1,126}}[a-z0-9]$'),
    key_digest text NOT NULL CHECK (key_digest ~ '^[0-9a-f]{{64}}$'),
    intent_digest text NOT NULL CHECK (intent_digest ~ '^[0-9a-f]{{64}}$'),
    unit_id text NOT NULL,
    created_at timestamptz NOT NULL,
    PRIMARY KEY (scope_id, key_digest),
    -- The receipt names the unit its request created, and the composite key proves the
    -- unit really belongs to the chapter scope that accepted the idempotency key.
    FOREIGN KEY (scope_id, unit_id) REFERENCES {s}.render_units (chapter_id, id)
        ON DELETE CASCADE
);
"""
