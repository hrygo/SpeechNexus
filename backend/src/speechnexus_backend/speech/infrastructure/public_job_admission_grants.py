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
_INSERT_COLUMNS = {
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
    "admission_operations": (
        "operation_id workspace_id request_digest request_document initial_attempt_id "
        "current_attempt_id recorded_at_us"
    ),
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
    "coordination_jobs": (
        "job_id operation_id attempt_id initial_event_id initial_event_digest request_digest"
    ),
    "coordination_outbox": "event_id job_id event_digest",
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
    for table, columns in _INSERT_COLUMNS.items():
        declared = (
            ADMISSION_OPERATION_INSERT_COLUMNS
            if table == "admission_operations"
            else tuple(columns.split())
        )
        grants.append(f'GRANT INSERT ({", ".join(declared)}) ON "{schema}".{table} TO "{role}"')
    for table, columns in _UPDATE_COLUMNS.items():
        listed = ", ".join(columns.split())
        grants.append(f'GRANT UPDATE ({listed}) ON "{schema}".{table} TO "{role}"')
    return tuple(grants)


__all__ = ["public_job_admission_grants"]
