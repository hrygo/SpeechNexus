"""Owner-only book/chapter fragment, after the saved-asset fragment.

An audiobook keeps its own source and chapter bodies; the speech base only ever
receives a bounded text unit Job. Book and chapter rows are immutable, project
deletion cascades books then chapters then their receipts, and a chapter position
is unique inside its book so composition cannot silently fork.
"""

from psycopg import sql

from speechnexus_backend.workbench.infrastructure.version_schema import schema_identifier


def books_migration_sql(schema: str) -> sql.Composed:
    return sql.SQL(_DDL).format(s=schema_identifier(schema))


def grant_books_runtime_sql(schema: str, role: str) -> sql.Composed:
    # Immutable rows: the runtime may insert and read, and may never update or delete.
    return sql.SQL("""
    GRANT SELECT, INSERT ON {s}.books, {s}.chapters, {s}.book_receipts TO {r};
    """).format(s=schema_identifier(schema), r=sql.Identifier(role))


_DDL = """
CREATE TABLE {s}.books (
    id text PRIMARY KEY CHECK (id ~ '^[a-z0-9][a-z0-9._:-]{{1,126}}[a-z0-9]$'),
    project_id text NOT NULL REFERENCES {s}.projects(id) ON DELETE CASCADE,
    title text NOT NULL CHECK (title <> '' AND length(title) <= 256),
    status text NOT NULL CHECK (status IN ('draft', 'ready', 'exporting')),
    casting_revision numeric(19,0) NOT NULL
        CHECK (casting_revision BETWEEN 1 AND 9999999999999999999),
    source_text text CHECK (source_text IS NULL OR length(source_text) <= 5000000),
    created_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL,
    CHECK (updated_at >= created_at),
    UNIQUE (id, project_id),
    UNIQUE (project_id, id)
);
CREATE INDEX books_project_idx ON {s}.books (project_id);
CREATE TABLE {s}.chapters (
    id text PRIMARY KEY CHECK (id ~ '^[a-z0-9][a-z0-9._:-]{{1,126}}[a-z0-9]$'),
    book_id text NOT NULL REFERENCES {s}.books(id) ON DELETE CASCADE,
    title text NOT NULL CHECK (title <> '' AND length(title) <= 256),
    ordinal numeric(19,0) NOT NULL CHECK (ordinal BETWEEN 0 AND 9999999999999999999),
    text text NOT NULL CHECK (text <> '' AND length(text) <= 5000000),
    text_revision numeric(19,0) NOT NULL CHECK (text_revision BETWEEN 1 AND 9999999999999999999),
    created_at timestamptz NOT NULL,
    UNIQUE (book_id, ordinal),
    UNIQUE (id, book_id),
    UNIQUE (book_id, id)
);
CREATE TABLE {s}.book_receipts (
    scope_kind text NOT NULL CHECK (scope_kind IN ('book', 'chapter')),
    scope_id text NOT NULL CHECK (scope_id ~ '^[a-z0-9][a-z0-9._:-]{{1,126}}[a-z0-9]$'),
    key_digest text NOT NULL CHECK (key_digest ~ '^[0-9a-f]{{64}}$'),
    intent_digest text NOT NULL CHECK (intent_digest ~ '^[0-9a-f]{{64}}$'),
    book_id text,
    chapter_id text,
    created_at timestamptz NOT NULL,
    PRIMARY KEY (scope_kind, scope_id, key_digest),
    -- A receipt names exactly one resource, and its scope column must be that
    -- resource's parent: a project for a book, a book for a chapter. A composite
    -- foreign key enforces the parent identity, and a NULL component disables the
    -- other one, so each receipt is bound to the scope that accepted its key.
    CHECK (
        (scope_kind = 'book' AND book_id IS NOT NULL AND chapter_id IS NULL)
        OR (scope_kind = 'chapter' AND chapter_id IS NOT NULL AND book_id IS NULL)
    ),
    FOREIGN KEY (scope_id, book_id) REFERENCES {s}.books (project_id, id) ON DELETE CASCADE,
    FOREIGN KEY (scope_id, chapter_id) REFERENCES {s}.chapters (book_id, id) ON DELETE CASCADE
);
"""
