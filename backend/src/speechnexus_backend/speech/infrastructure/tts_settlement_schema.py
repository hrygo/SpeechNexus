"""Least privileges for atomic settlement roots and independent settlement workers."""

from speechnexus_backend.speech.infrastructure.job_schema import checked_sql_name


def tts_settlement_writer_grants(schema: str, role: str) -> tuple[str, ...]:
    schema, role = checked_sql_name(schema), checked_sql_name(role)
    return (
        f'GRANT SELECT ON "{schema}".speech_tts_cancellation TO "{role}"',
        f"GRANT SELECT, INSERT (lease_id,operation_id,attempt_id,submit_intent_id,"
        f'target_document,deadline_at_us,abort_reason) ON "{schema}".'
        f'speech_tts_settlement TO "{role}"',
    )


def tts_settlement_worker_grants(schema: str, role: str) -> tuple[str, ...]:
    schema, role = checked_sql_name(schema), checked_sql_name(role)
    return (
        f'GRANT USAGE ON SCHEMA "{schema}" TO "{role}"',
        f'GRANT SELECT, INSERT ON "{schema}".speech_tts_cancellation TO "{role}"',
        f'GRANT SELECT ON "{schema}".speech_tts_settlement TO "{role}"',
        f"GRANT UPDATE (abort_reason,next_check_at_us,settled_at_us) "
        f'ON "{schema}".speech_tts_settlement TO "{role}"',
    )
