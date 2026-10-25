"""Workbench v2 owner migration fragment; no runtime auto-migration."""

from __future__ import annotations

from psycopg import sql

from speechnexus_backend.workbench.infrastructure.version_schema import schema_identifier


def deletion_migration_sql(schema: str) -> sql.Composed:
    return sql.SQL(_V2).format(s=schema_identifier(schema))


def grant_deletion_runtime_sql(schema: str, role: str) -> sql.Composed:
    return sql.SQL("""
        GRANT SELECT ON {s}.deletion_plans, {s}.deletion_records, {s}.deletion_receipts,
            {s}.resource_tombstones, {s}.erased_mutation_receipts, {s}.deletion_ledger TO {r};
        GRANT INSERT ON {s}.deletion_plans, {s}.deletion_records, {s}.deletion_receipts,
            {s}.resource_tombstones, {s}.deletion_outbox TO {r};
    """).format(s=schema_identifier(schema), r=sql.Identifier(role))


def grant_cleanup_runtime_sql(schema: str, role: str) -> sql.Composed:
    """Separate worker may erase business rows, never tombstones or audit identities."""
    return sql.SQL("""
        GRANT USAGE ON SCHEMA {s} TO {r};
        GRANT SELECT ON {s}.projects, {s}.project_versions, {s}.transcripts,
            {s}.transcript_revisions, {s}.mutation_receipts, {s}.resource_tombstones,
            {s}.erased_mutation_receipts, {s}.deletion_plans, {s}.deletion_records,
            {s}.deletion_outbox, {s}.deletion_ledger TO {r};
        GRANT DELETE ON {s}.projects, {s}.project_versions, {s}.transcripts,
            {s}.transcript_revisions, {s}.mutation_receipts TO {r};
        GRANT INSERT ON {s}.erased_mutation_receipts, {s}.deletion_ledger TO {r};
        GRANT UPDATE (completed_at, objects_erased_at) ON {s}.deletion_outbox TO {r};
        GRANT UPDATE (id) ON {s}.projects, {s}.transcripts TO {r};
    """).format(s=schema_identifier(schema), r=sql.Identifier(role))


_V2 = """
CREATE TABLE {s}.deletion_plans (
    id text PRIMARY KEY CHECK (char_length(id) BETWEEN 1 AND 128),
    workspace_id text NOT NULL CHECK (char_length(workspace_id) BETWEEN 1 AND 128),
    target_kind text NOT NULL CHECK (target_kind IN ('project', 'transcript')),
    target_id text NOT NULL CHECK (char_length(target_id) BETWEEN 1 AND 128),
    target_revision numeric(19,0) NOT NULL
        CHECK (target_revision BETWEEN 1 AND 9999999999999999999),
    mode text NOT NULL CHECK (mode IN ('ordinary', 'cascade')),
    created_at timestamptz NOT NULL,
    expires_at timestamptz NOT NULL CHECK (expires_at = created_at + interval '300 seconds'),
    dependencies jsonb NOT NULL CHECK (jsonb_typeof(dependencies) = 'array'),
    plan_digest text NOT NULL CHECK (plan_digest ~ '^sha256:[0-9a-f]{{64}}$')
);
CREATE INDEX deletion_plans_workspace ON {s}.deletion_plans(workspace_id, id);

CREATE TABLE {s}.deletion_records (
    id text PRIMARY KEY CHECK (char_length(id) BETWEEN 1 AND 128),
    workspace_id text NOT NULL CHECK (char_length(workspace_id) BETWEEN 1 AND 128),
    plan_id text NOT NULL UNIQUE REFERENCES {s}.deletion_plans(id),
    plan_digest text NOT NULL CHECK (plan_digest ~ '^sha256:[0-9a-f]{{64}}$'),
    created_at timestamptz NOT NULL
);
CREATE INDEX deletion_records_workspace ON {s}.deletion_records(workspace_id, id);

CREATE TABLE {s}.resource_tombstones (
    resource_kind text NOT NULL CHECK (resource_kind IN ('project', 'transcript')),
    resource_id text NOT NULL CHECK (char_length(resource_id) BETWEEN 1 AND 128),
    resource_revision numeric(19,0) NOT NULL
        CHECK (resource_revision BETWEEN 1 AND 9999999999999999999),
    deletion_id text NOT NULL REFERENCES {s}.deletion_records(id),
    created_at timestamptz NOT NULL,
    PRIMARY KEY (resource_kind, resource_id)
);
CREATE INDEX tombstones_deletion ON {s}.resource_tombstones(deletion_id);

CREATE TABLE {s}.deletion_outbox (
    deletion_id text PRIMARY KEY REFERENCES {s}.deletion_records(id),
    created_at timestamptz NOT NULL,
    completed_at timestamptz CHECK (completed_at >= created_at),
    -- Row erasure commits before the object sweep (object I/O never holds a
    -- transaction), so the object half needs its own completion evidence: without
    -- it a transient zone failure would look like a finished deletion.
    objects_erased_at timestamptz CHECK (objects_erased_at >= created_at)
);

CREATE TABLE {s}.deletion_receipts (
    workspace_id text NOT NULL CHECK (char_length(workspace_id) BETWEEN 1 AND 128),
    family text NOT NULL CHECK (family IN ('plan', 'create')),
    key_digest text NOT NULL CHECK (key_digest ~ '^[0-9a-f]{{64}}$'),
    intent_digest text NOT NULL CHECK (intent_digest ~ '^[0-9a-f]{{64}}$'),
    plan_id text REFERENCES {s}.deletion_plans(id),
    deletion_id text REFERENCES {s}.deletion_records(id),
    PRIMARY KEY (workspace_id, family, key_digest),
    CHECK ((family = 'plan' AND plan_id IS NOT NULL AND deletion_id IS NULL) OR
        (family = 'create' AND plan_id IS NULL AND deletion_id IS NOT NULL))
);
CREATE INDEX deletion_receipts_plan ON {s}.deletion_receipts(plan_id);
CREATE INDEX deletion_receipts_record ON {s}.deletion_receipts(deletion_id);

CREATE TABLE {s}.erased_mutation_receipts (
    operation text NOT NULL,
    scope_id text NOT NULL,
    key_digest text NOT NULL CHECK (key_digest ~ '^[0-9a-f]{{64}}$'),
    intent_digest text NOT NULL CHECK (intent_digest ~ '^[0-9a-f]{{64}}$'),
    deletion_id text NOT NULL REFERENCES {s}.deletion_records(id),
    PRIMARY KEY (operation, scope_id, key_digest)
);
CREATE INDEX erased_receipts_deletion ON {s}.erased_mutation_receipts(deletion_id);

CREATE TABLE {s}.deletion_ledger (
    id text PRIMARY KEY CHECK (char_length(id) BETWEEN 1 AND 128),
    deletion_id text NOT NULL UNIQUE REFERENCES {s}.deletion_records(id),
    completed_at timestamptz NOT NULL,
    deleted jsonb NOT NULL CHECK (
        jsonb_typeof(deleted) = 'array' AND jsonb_array_length(deleted) > 0),
    retained_independent_copies jsonb NOT NULL
        CHECK (jsonb_typeof(retained_independent_copies) = 'array')
);
"""
