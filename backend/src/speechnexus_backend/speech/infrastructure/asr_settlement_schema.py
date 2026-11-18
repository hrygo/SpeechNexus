"""Least privileges for ASR settlement roots and their independent worker."""

from speechnexus_backend.speech.infrastructure.job_schema import checked_sql_name


def asr_settlement_writer_grants(schema: str, role: str) -> tuple[str, ...]:
    """The admission identity writes one root and reads it back in the same transaction."""
    schema, role = checked_sql_name(schema), checked_sql_name(role)
    return (
        f"GRANT SELECT, INSERT (lease_id,operation_id,attempt_id,gateway_id,"
        f"host_node_id,next_check_at_us) ON "
        f'"{schema}".speech_asr_settlement TO "{role}"',
    )


def asr_settlement_worker_grants(schema: str, role: str) -> tuple[str, ...]:
    """The settlement identity only moves the due cursor and the settled marker."""
    schema, role = checked_sql_name(schema), checked_sql_name(role)
    return (
        f'GRANT USAGE ON SCHEMA "{schema}" TO "{role}"',
        f'GRANT SELECT ON "{schema}".speech_asr_settlement TO "{role}"',
        f"GRANT UPDATE (next_check_at_us,settled_at_us) ON "
        f'"{schema}".speech_asr_settlement TO "{role}"',
    )
