"""Column-only privileges for the independent gateway execution recovery reader."""

from speechnexus_backend.speech.infrastructure.coordination_keyed_lock_schema import (
    coordination_keyed_lock_grants,
)
from speechnexus_backend.speech.infrastructure.job_schema import checked_sql_name

CANCELLATION_FENCE_TABLE = "speech_job_cancellation_fences"
CANCELLATION_FENCE_GATEWAY_COLUMNS = ("job_id",)
CANCELLATION_FENCE_WORKER_COLUMNS = ("job_id", "accepted_at_us", "request_id")

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
        "dispatch_outcome",
        "dispatch_attempts",
    ),
    "speech_publication_installation": ("singleton", "installation_id"),
    CANCELLATION_FENCE_TABLE: CANCELLATION_FENCE_GATEWAY_COLUMNS,
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


def gateway_keyed_lock_grants(schema: str, role: str) -> tuple[str, ...]:
    """The gateway's half of the shared catalog mutex: the coordination lock row.

    The mutex is only a mutex if *both* sides reach the same row, and the gateway is
    a separate role in the ``speech`` schema, so the lock table and its grants live
    where both roles already have a relationship instead of in either side's own
    namespace. The privilege surface itself is declared once
    (``coordination_keyed_lock_schema.coordination_keyed_lock_grants``) and the
    backend role calls the same function: this wrapper only says *which* identity
    holds it, so adding a column to the lock table cannot leave one side behind and
    silently split the mutex in two.

    Separate from :func:`gateway_query_grants` on purpose: that reader is verified
    by an exact role closure (``gateway/infrastructure/reader_authority.py``, which
    refuses any INSERT/UPDATE anywhere), so granting the lock there would make the
    reader refuse to start. This function targets the execution gateway identity
    instead, which is already a writer in this schema.
    """

    return coordination_keyed_lock_grants(schema, role)
