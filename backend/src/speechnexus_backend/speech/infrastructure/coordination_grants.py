"""Explicit minimum PostgreSQL DML for fresh, non-owner coordination roles.

Run reviewed output as the migration owner, never during application startup.
Roles must have no inherited/global privileges. This does not provision logins.
Application transactions enforce cross-record rules; grants do not replace them.
"""

from __future__ import annotations

from speechnexus_backend.speech.infrastructure.job_schema import checked_sql_name

DIMENSIONS = (
    "unified_physical_memory_bytes",
    "device_memory_bytes",
    "host_memory_bytes",
    "cpu_decode_millicores",
    "host_pcm_buffer_bytes",
    "scratch_disk_bytes",
)
SPEECH_UPDATES = {
    "resource_pools": "pool_state owner_id fencing_token owner_expires_at_us owner_state version",
    "workspace_quotas": "held_attempts quarantined_attempts version",
    "host_nodes": "node_state version "
    + " ".join(
        f"{prefix}_{dimension}" for prefix in ("held", "quarantined") for dimension in DIMENSIONS
    ),
    "engine_instances": (
        "instance_state held_slots quarantined_slots base_budget_kind ready_at_us "
        "stopped_at_us quarantine_reason last_fencing_token version"
    ),
    "runtime_instance_heads": (
        "current_generation generation_high_water "
        "current_prepare_intent_id current_stop_intent_id version"
    ),
    "runtime_instance_intents": "intent_state stop_evidence_document error_code version",
    "admission_attempts": "quota_state version",
    "admission_leases": (
        "lease_state cleanup_state budget_released_at_us "
        "quarantine_observed_at_us quarantine_reason_code version"
    ),
    "coordination_jobs": "claim_owner_id claim_expires_at_us claim_version job_state",
    "coordination_outbox": "delivered_at_us",
}
GATEWAY_UPDATES = {
    "gateway_fencing_barriers": "min_fencing_token sealed_evidence_digest version",
    "submit_intents": (
        "execution_state cleanup_state engine_request_id dispatched_at_us "
        "staging_output_object_key terminated_at_us generated_pcm_samples error_code "
        "request_document command_redacted_at_us command_deletion_job_id "
        "command_deletion_evidence_digest updated_at_us version"
    ),
    "execution_stop_evidence": "",
    "output_stream_heads": "version",
    "output_sample_intervals": "",
}
CONTROL_PLANE_TABLES = frozenset({"resource_pools", "workspace_quotas", "host_nodes"})
INSERT_COLUMNS = {
    "coordination_jobs": (
        "job_id operation_id attempt_id initial_event_id initial_event_digest request_digest"
    ),
    "coordination_outbox": "event_id job_id event_digest",
}
SPEECH_NARROW_SELECT = {
    "submit_intents": (
        "submit_intent_id lease_id operation_id attempt_id execution_state cleanup_state version"
    )
}
GATEWAY_NARROW_SELECT = {
    "runtime_instance_intents": (
        "target_node_id intent_id instance_id instance_generation action request_digest "
        "intent_state error_code version"
    )
}


def coordination_runtime_grants(
    schema: str, *, speech_role: str, gateway_role: str
) -> tuple[str, ...]:
    """No DDL, history deletion, foreign private bodies or config inserts/updates.

    Only version-column UPDATE crosses ownership, to permit PostgreSQL row locks.
    Request-body UPDATE is the gateway's explicit deletion-tombstone exception.
    """
    schema, speech_role, gateway_role = map(checked_sql_name, (schema, speech_role, gateway_role))
    if speech_role == gateway_role:
        raise ValueError("Coordination owners require separate runtime roles")
    grants: list[str] = []
    all_updates = {**SPEECH_UPDATES, **GATEWAY_UPDATES}
    for role, owned, narrow in (
        (speech_role, SPEECH_UPDATES, SPEECH_NARROW_SELECT),
        (gateway_role, GATEWAY_UPDATES, GATEWAY_NARROW_SELECT),
    ):
        grants.append(f'GRANT USAGE ON SCHEMA "{schema}" TO "{role}"')
        for table, updates in all_updates.items():
            target = f'"{schema}"."{table}"'
            columns = ", ".join(narrow.get(table, "").split())
            readable = f"SELECT ({columns})" if columns else "SELECT"
            grants.append(f'GRANT {readable} ON {target} TO "{role}"')
            if table in owned:
                if table not in CONTROL_PLANE_TABLES:
                    columns = ", ".join(INSERT_COLUMNS.get(table, "").split())
                    insert = f"INSERT ({columns})" if columns else "INSERT"
                    grants.append(f'GRANT {insert} ON {target} TO "{role}"')
                if updates:
                    columns = ", ".join(updates.split())
                    grants.append(f'GRANT UPDATE ({columns}) ON {target} TO "{role}"')
            elif "version" in updates.split():
                grants.append(f'GRANT UPDATE (version) ON {target} TO "{role}"')
    return tuple(grants)
