"""Workbench v1 migration fragment, executed only by the deployment migration owner.

This is not a migration runner. Team A integrates this fragment into its versioned,
globally serialized owner step. Runtime connections never call these functions.
"""

from __future__ import annotations

import re

from psycopg import sql


def schema_identifier(name: str) -> sql.Identifier:
    """Keep names explicit and avoid truncation/accidental cross-module namespaces."""
    if not re.fullmatch(r"[a-z][a-z0-9_]{0,62}", name):
        raise ValueError("invalid workbench schema name")
    if name in {"public", "information_schema"} or name.startswith("pg_"):
        raise ValueError("reserved workbench schema name")
    return sql.Identifier(name)


def migration_sql(schema: str) -> sql.Composed:
    """Fail on an existing namespace; never silently accept schema drift."""
    return sql.SQL(_V1).format(s=schema_identifier(schema))


def grant_runtime_sql(schema: str, role: str) -> sql.Composed:
    """An existing non-owner role may append history and advance only head columns."""
    return sql.SQL("""
        GRANT USAGE ON SCHEMA {s} TO {r};
        GRANT SELECT, INSERT ON {s}.projects, {s}.project_versions,
            {s}.transcripts, {s}.transcript_revisions, {s}.mutation_receipts TO {r};
        GRANT UPDATE (metadata_revision) ON {s}.projects TO {r};
        GRANT UPDATE (head_revision) ON {s}.transcripts TO {r};
    """).format(s=schema_identifier(schema), r=sql.Identifier(role))


_V1 = """
CREATE SCHEMA {s};
REVOKE ALL ON SCHEMA {s} FROM PUBLIC;

CREATE TABLE {s}.projects (
    id text PRIMARY KEY CHECK (char_length(id) BETWEEN 1 AND 128),
    workspace_id text NOT NULL CHECK (char_length(workspace_id) BETWEEN 1 AND 128),
    created_at timestamptz NOT NULL,
    metadata_revision numeric(19,0) NOT NULL CHECK (
        metadata_revision BETWEEN 1 AND 9999999999999999999)
);
CREATE INDEX projects_workspace ON {s}.projects(workspace_id, created_at, id);

CREATE TABLE {s}.project_versions (
    project_id text NOT NULL REFERENCES {s}.projects(id),
    revision numeric(19,0) NOT NULL CHECK (revision BETWEEN 1 AND 9999999999999999999),
    name text NOT NULL CHECK (char_length(name) BETWEEN 1 AND 128),
    description text CHECK (char_length(description) <= 2000),
    status text NOT NULL CHECK (status IN ('active', 'archived')),
    updated_at timestamptz NOT NULL,
    PRIMARY KEY (project_id, revision)
);
ALTER TABLE {s}.projects ADD CONSTRAINT projects_head
    FOREIGN KEY (id, metadata_revision) REFERENCES {s}.project_versions(project_id, revision)
    DEFERRABLE INITIALLY DEFERRED;

CREATE TABLE {s}.transcripts (
    id text PRIMARY KEY CHECK (char_length(id) BETWEEN 1 AND 128),
    project_id text NOT NULL REFERENCES {s}.projects(id),
    display_name text NOT NULL CHECK (char_length(display_name) BETWEEN 1 AND 128),
    created_at timestamptz NOT NULL,
    head_revision numeric(19,0) NOT NULL CHECK (head_revision BETWEEN 1 AND 9999999999999999999)
);
CREATE INDEX transcripts_project ON {s}.transcripts(project_id, created_at, id);

CREATE TABLE {s}.transcript_revisions (
    id text NOT NULL UNIQUE CHECK (char_length(id) BETWEEN 1 AND 128),
    transcript_id text NOT NULL REFERENCES {s}.transcripts(id),
    revision numeric(19,0) NOT NULL CHECK (revision BETWEEN 1 AND 9999999999999999999),
    status text NOT NULL CHECK (status IN ('draft', 'sealed')),
    text text NOT NULL CHECK (char_length(text) <= 1000000),
    source_revision numeric(19,0),
    source_artifact_id text CHECK (char_length(source_artifact_id) BETWEEN 1 AND 128),
    source_receipt_id text CHECK (char_length(source_receipt_id) BETWEEN 1 AND 128),
    created_at timestamptz NOT NULL,
    PRIMARY KEY (transcript_id, revision),
    FOREIGN KEY (transcript_id, source_revision)
        REFERENCES {s}.transcript_revisions(transcript_id, revision),
    CHECK ((source_artifact_id IS NULL) = (source_receipt_id IS NULL)),
    CHECK ((revision = 1 AND source_revision IS NULL) OR
        (revision > 1 AND source_revision IS NOT NULL
            AND source_revision >= 1 AND source_revision < revision)),
    CHECK ((status = 'sealed' AND revision = 1 AND source_artifact_id IS NOT NULL) OR
        (status = 'draft' AND (revision > 1 OR
            (source_artifact_id IS NULL AND char_length(text) > 0))))
);
CREATE INDEX transcript_source ON {s}.transcript_revisions(transcript_id, source_revision);
ALTER TABLE {s}.transcripts ADD CONSTRAINT transcripts_head
    FOREIGN KEY (id, head_revision) REFERENCES {s}.transcript_revisions(transcript_id, revision)
    DEFERRABLE INITIALLY DEFERRED;

CREATE TABLE {s}.mutation_receipts (
    operation text NOT NULL CHECK (operation IN (
        'project.create', 'project.edit', 'transcript.create', 'transcript.revise')),
    scope_id text NOT NULL CHECK (char_length(scope_id) BETWEEN 1 AND 128),
    key_digest text NOT NULL CHECK (key_digest ~ '^[0-9a-f]{{64}}$'),
    intent_digest text NOT NULL CHECK (intent_digest ~ '^[0-9a-f]{{64}}$'),
    project_id text,
    project_revision numeric(19,0),
    transcript_id text,
    transcript_revision numeric(19,0),
    PRIMARY KEY (operation, scope_id, key_digest),
    FOREIGN KEY (project_id, project_revision)
        REFERENCES {s}.project_versions(project_id, revision),
    FOREIGN KEY (transcript_id, transcript_revision)
        REFERENCES {s}.transcript_revisions(transcript_id, revision),
    CHECK ((operation IN ('project.create', 'project.edit') AND project_id IS NOT NULL
        AND project_revision IS NOT NULL AND transcript_id IS NULL AND transcript_revision IS NULL)
        OR (operation IN ('transcript.create', 'transcript.revise') AND transcript_id IS NOT NULL
        AND transcript_revision IS NOT NULL AND project_id IS NULL AND project_revision IS NULL))
);
CREATE INDEX receipts_project ON {s}.mutation_receipts(project_id, project_revision);
CREATE INDEX receipts_transcript ON {s}.mutation_receipts(transcript_id, transcript_revision);
"""
