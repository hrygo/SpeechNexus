"""Workbench v2 forward fragment; only the deployment migration owner executes it.

Requires the unmodified v1 fragment and its PostgreSQL-generated constraint names.
Team A integrates this once in a globally serialized, explicit transaction before
using the sealing adapter. Missing/renamed constraints fail; this is neither an
idempotent runner nor a runtime auto-upgrade. v1 remains an immutable baseline.
"""

from psycopg import sql

from speechnexus_backend.workbench.infrastructure.version_schema import schema_identifier


def sealing_migration_sql(schema: str) -> sql.Composed:
    """Allow append-only sealed revisions and their separately scoped replay receipts."""
    return sql.SQL(_V2).format(s=schema_identifier(schema))


_V2 = """
ALTER TABLE {s}.transcript_revisions
    DROP CONSTRAINT transcript_revisions_check2,
    ADD CONSTRAINT transcript_revisions_status_source_v2 CHECK (
        (revision = 1 AND (
            (status = 'sealed' AND source_artifact_id IS NOT NULL) OR
            (status = 'draft' AND source_artifact_id IS NULL AND char_length(text) > 0)))
        OR (revision > 1 AND status IN ('draft', 'sealed')));

ALTER TABLE {s}.mutation_receipts
    DROP CONSTRAINT mutation_receipts_operation_check,
    DROP CONSTRAINT mutation_receipts_check,
    ADD CONSTRAINT mutation_receipts_operation_v2 CHECK (operation IN (
        'project.create', 'project.edit', 'transcript.create', 'transcript.revise',
        'transcript.seal')),
    ADD CONSTRAINT mutation_receipts_target_v2 CHECK (
        (operation IN ('project.create', 'project.edit') AND project_id IS NOT NULL
            AND project_revision IS NOT NULL AND transcript_id IS NULL
            AND transcript_revision IS NULL)
        OR (operation IN ('transcript.create', 'transcript.revise', 'transcript.seal')
            AND transcript_id IS NOT NULL AND transcript_revision IS NOT NULL
            AND project_id IS NULL AND project_revision IS NULL));
"""
