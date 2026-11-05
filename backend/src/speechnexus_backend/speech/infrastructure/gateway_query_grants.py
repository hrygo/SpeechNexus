"""Column-only privileges for the independent gateway execution recovery reader."""

from speechnexus_backend.speech.infrastructure.job_schema import checked_sql_name

CANCELLATION_FENCE_TABLE = "speech_job_cancellation_fences"
CANCELLATION_FENCE_READ_COLUMNS = ("job_id", "accepted_at_us", "request_id")

# Columns used by read_submit_intent, read_original_request, and the abort receipt
# and tombstone reads. In particular, no writer or unrelated content table is granted.
_READ_COLUMNS: dict[str, tuple[str, ...]] = {
    "submit_intents": (
        "submit_intent_id",
        "lease_id",
        "operation_id",
        "attempt_id",
        "command_digest",
        "request_digest",
        "request_document",
        "command_redacted_at_us",
        "command_deletion_job_id",
        "command_deletion_evidence_digest",
        "voice_revision_id",
        "deadline_at_us",
        "recorded_at_us",
        "updated_at_us",
        "execution_state",
        "cleanup_state",
        "engine_request_id",
        "dispatched_at_us",
        "error_code",
        "output_stream_id",
        "sample_rate_hz",
        "hard_output_ceiling_samples",
        "generated_pcm_samples",
        "staging_output_object_key",
        "staging_result_document",
        "version",
        "terminated_at_us",
    ),
    "admission_leases": (
        "lease_id",
        "operation_id",
        "attempt_id",
        "stage_id",
        "pool_id",
        "host_node_id",
        "gateway_id",
        "instance_id",
        "instance_generation",
        "fencing_token",
        "lease_state",
    ),
    "admission_attempts": (
        "operation_id",
        "attempt_id",
        "workspace_id",
        "binding_revision_id",
        "policy_revision_id",
    ),
    "execution_stop_evidence": ("submit_intent_id", "evidence_kind", "evidence_digest"),
    "output_sample_intervals": (
        "operation_id",
        "attempt_id",
        "output_stream_id",
        "submit_intent_id",
    ),
    "gateway_abort_intents": (
        "submit_intent_id",
        "operation_id",
        "attempt_id",
        "fencing_token",
        "reason",
        "wait_timeout_ms",
        "received_at_us",
        "dispatched_at_us",
    ),
    "speech_publication_installation": ("singleton", "installation_id"),
    CANCELLATION_FENCE_TABLE: CANCELLATION_FENCE_READ_COLUMNS,
}


def gateway_query_grants(schema: str, role: str) -> tuple[str, ...]:
    """Provision an otherwise unprivileged reader; verify its complete role closure at startup."""
    schema, role = checked_sql_name(schema), checked_sql_name(role)
    statements = [f'GRANT USAGE ON SCHEMA "{schema}" TO "{role}"']
    statements.extend(
        f'GRANT SELECT ({", ".join(columns)}) ON "{schema}"."{table}" TO "{role}"'
        for table, columns in _READ_COLUMNS.items()
    )
    return tuple(statements)


def gateway_query_target_grants(schema: str, role: str) -> tuple[str, ...]:
    """Permit a gateway writer to compare its installation with the read-only target."""
    schema, role = checked_sql_name(schema), checked_sql_name(role)
    return (
        f'GRANT USAGE ON SCHEMA "{schema}" TO "{role}"',
        f'GRANT SELECT (singleton, installation_id) ON "{schema}".'
        f'"speech_publication_installation" TO "{role}"',
    )
