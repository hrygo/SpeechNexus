"""Durable public Job cancellation acceptance, separate from stop evidence.

The row records that one cancellation intent was received and which original
Job/attempt it binds. It never proves engine stop, cleanup completion or
capacity release; those remain authoritative Job and execution facts.
"""

from speechnexus_backend.speech.infrastructure.job_schema import checked_sql_name

TABLE = "speech_job_cancellations"

PUBLIC_JOB_CANCELLATION_DDL = (
    f"""CREATE TABLE {TABLE} (
        job_id VARCHAR(128) COLLATE "C" PRIMARY KEY
            REFERENCES speech_jobs (job_id) ON DELETE RESTRICT,
        workspace_id VARCHAR(128) COLLATE "C" NOT NULL,
        operation_id VARCHAR(128) COLLATE "C" NOT NULL,
        attempt_id VARCHAR(128) COLLATE "C" NOT NULL,
        request_id VARCHAR(128) COLLATE "C" NOT NULL
            CHECK (length(request_id) BETWEEN 1 AND 128),
        request_digest VARCHAR(71) COLLATE "C" NOT NULL,
        idempotency_key_digest VARCHAR(71) COLLATE "C",
        accepted BOOLEAN NOT NULL,
        requested_at_us BIGINT NOT NULL
            CHECK (requested_at_us BETWEEN 0 AND 253402300799999999),
        UNIQUE (workspace_id, idempotency_key_digest)
    )""",
)


def public_job_cancellation_grants(schema: str, role: str) -> tuple[str, ...]:
    """Provisioning declaration; cancellation rows are append-only evidence."""
    schema, role = checked_sql_name(schema), checked_sql_name(role)
    return (
        f'GRANT USAGE ON SCHEMA "{schema}" TO "{role}"',
        f'GRANT SELECT, INSERT ON "{schema}".{TABLE} TO "{role}"',
    )
