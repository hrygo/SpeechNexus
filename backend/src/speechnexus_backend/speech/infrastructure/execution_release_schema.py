"""Read-only absence projection, never a producer of stop or release evidence."""

from speechnexus_backend.speech.infrastructure.job_schema import checked_sql_name

RELEASE_GUARD_VIEW = "execution_release_guards"
RELEASE_GUARD_DDL = f"""CREATE VIEW {RELEASE_GUARD_VIEW} AS
SELECT s.submit_intent_id,
    CASE WHEN s.engine_request_id IS NULL AND s.dispatched_at_us IS NULL
        AND s.staging_output_object_key IS NULL AND s.staging_result_document IS NULL
        AND s.generated_pcm_samples = 0
        AND NOT EXISTS (
            SELECT 1 FROM output_sample_intervals i
            WHERE i.operation_id = s.operation_id AND i.attempt_id = s.attempt_id
                AND i.output_stream_id = s.output_stream_id
                AND i.submit_intent_id = s.submit_intent_id
        ) THEN 1 ELSE 0 END AS no_dispatch_metadata_absent
FROM submit_intents s"""


def release_guard_grants(schema: str, role: str) -> tuple[str, ...]:
    """Expose presence only; no private request IDs, object keys or result bodies."""
    schema, role = checked_sql_name(schema), checked_sql_name(role)
    return (
        f"GRANT SELECT (submit_intent_id, no_dispatch_metadata_absent) "
        f'ON "{schema}".{RELEASE_GUARD_VIEW} TO "{role}"',
    )
