"""Least-privilege DML for the role that owns one atomic public Job create.

The API writer role keeps reads, cancellation and progress. Only this role may
insert the admission operation/attempt/request/lease and the coordination job
that belongs to them, so a writer cannot fabricate an executable command in a
separate transaction. No DELETE, DDL, claim UPDATE or body UPDATE is granted.
"""

from __future__ import annotations

from speechnexus_backend.speech.infrastructure.coordination_grants import DIMENSIONS
from speechnexus_backend.speech.infrastructure.job_schema import checked_sql_name
from speechnexus_backend.speech.infrastructure.job_writer_columns import (
    ADMISSION_OPERATION_INSERT_COLUMNS,
    COORDINATION_JOB_INSERT_COLUMNS,
    COORDINATION_OUTBOX_INSERT_COLUMNS,
)

_READ_TABLES = (
    "speech_workspaces",
    "speech_publications",
    "speech_publication_heads",
    "speech_publication_retirements",
    "speech_binding_revisions",
    "speech_assets",
    "speech_artifacts",
    "speech_pronunciation_revisions",
    "speech_jobs",
    "speech_job_events",
    "speech_job_submissions",
    "speech_job_intents",
    "speech_voice_drafts",
    "speech_voice_draft_revisions",
    "speech_voice_revisions",
    "speech_builtin_voice_qualifications",
    # The Job builtin fallback reads the qualification's provenance to decide whether
    # the source has evidence: the qualification row alone would let a published but
    # unevidenced row reach the shared speaker (see qualification_source_evidence).
    "speech_builtin_voice_qualification_provenance",
    "speech_builtin_voice_operator_attestations",
    "workspace_quotas",
    "resource_pools",
    "host_nodes",
    "engine_instances",
    "runtime_instance_heads",
    "runtime_instance_intents",
    "admission_operations",
    "admission_attempts",
    "admission_requests",
    "admission_leases",
    "coordination_jobs",
    "coordination_outbox",
)
#: One INSERT surface: either a declaration tuple owned by ``job_writer_columns``
#: or the space-joined shorthand this module has always used for the rest.
_DeclaredSurface = str | tuple[str, ...]


def _declared_columns(surface: _DeclaredSurface) -> tuple[str, ...]:
    """A declaration passes through; the shorthand splits on spaces, as before."""
    return surface if isinstance(surface, tuple) else tuple(surface.split())


# The relations this role may insert, in the order the grants are emitted. The
# coordination pair and the admission operation are the declarations the writer
# and provisioning already share (#1181): a column added there reaches this GRANT
# without a second edit. The other entries stay as written here because this
# role's surface is not the writer's own -- `speech_job_events` is four columns
# rather than eight, `speech_jobs` orders `retry_of_job_id` before the state
# columns, and the admission attempt/request/lease relations have no writer
# declaration -- and the grant text is part of the contract. The remaining
# identical overlaps with a writer declaration are a smaller, separate cleanup.
_INSERT_COLUMNS: dict[str, _DeclaredSurface] = {
    "speech_jobs": (
        "job_id workspace_id kind model_id request_digest idempotency_key_digest "
        "retry_of_job_id state status_version result_json error_code created_at_us updated_at_us"
    ),
    "speech_job_events": "job_id event_seq state occurred_at_us",
    "speech_job_submissions": (
        "job_id request_id client_reference effective_configuration_json execution_revision "
        "progress_unit progress_total submitted_at_us"
    ),
    "speech_job_intents": "job_id request_digest request_document recorded_at_us",
    "speech_voice_drafts": (
        "draft_id generation_job_id kind status head_revision metadata_revision display_name "
        "input_json binding_json created_at_us updated_at_us"
    ),
    "speech_voice_draft_revisions": (
        "draft_id revision kind generation_job_id input_json binding_json created_at_us"
    ),
    "admission_operations": ADMISSION_OPERATION_INSERT_COLUMNS,
    "admission_attempts": (
        "operation_id attempt_id workspace_id binding_revision_id manifest_id recipe_digest "
        "policy_revision_id service_class request_digest required_stage_ids"
    ),
    "admission_requests": (
        "operation_id attempt_id request_digest request_document recorded_at_us"
    ),
    "admission_leases": (
        "lease_id operation_id attempt_id stage_id host_node_id instance_id instance_generation "
        "pool_id gateway_id fencing_token reserved_slots deadline_at_us "
        + " ".join("reserved_" + dimension for dimension in DIMENSIONS)
    ),
    "coordination_jobs": COORDINATION_JOB_INSERT_COLUMNS,
    "coordination_outbox": COORDINATION_OUTBOX_INSERT_COLUMNS,
}
_UPDATE_COLUMNS = {
    "workspace_quotas": "held_attempts version",
    "host_nodes": "version " + " ".join("held_" + dimension for dimension in DIMENSIONS),
    "engine_instances": "held_slots version",
    # PostgreSQL requires UPDATE privilege for SELECT ... FOR UPDATE row locks.
    # These version columns are never assigned; the grants make the lock legal.
    "resource_pools": "version",
    "runtime_instance_heads": "version",
    "runtime_instance_intents": "version",
    "admission_operations": "version",
    "admission_attempts": "version",
    "admission_leases": "version",
    "speech_voice_drafts": "updated_at_us head_revision generation_job_id",
}


def public_job_admission_grants(schema: str, role: str) -> tuple[str, ...]:
    """Apply as the migration owner to the dedicated admission role."""
    schema, role = checked_sql_name(schema), checked_sql_name(role)
    grants = [f'GRANT USAGE ON SCHEMA "{schema}" TO "{role}"']
    for table in _READ_TABLES:
        grants.append(f'GRANT SELECT ON "{schema}".{table} TO "{role}"')
    for table, surface in _INSERT_COLUMNS.items():
        declared = _declared_columns(surface)
        grants.append(f'GRANT INSERT ({", ".join(declared)}) ON "{schema}".{table} TO "{role}"')
    for table, columns in _UPDATE_COLUMNS.items():
        listed = ", ".join(columns.split())
        grants.append(f'GRANT UPDATE ({listed}) ON "{schema}".{table} TO "{role}"')
    return tuple(grants)


__all__ = ["public_job_admission_grants"]
