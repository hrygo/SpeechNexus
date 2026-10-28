"""Gateway-owned abort receipt and dispatch uncertainty, with no content payload."""

from speechnexus_backend.speech.infrastructure.job_schema import checked_sql_name

ABORT_INTENT_TABLE = "gateway_abort_intents"
ABORT_INTENT_DDL = f"""CREATE TABLE {ABORT_INTENT_TABLE} (
    submit_intent_id VARCHAR(128) COLLATE "C" PRIMARY KEY
        REFERENCES submit_intents (submit_intent_id),
    operation_id VARCHAR(128) COLLATE "C" NOT NULL,
    attempt_id VARCHAR(128) COLLATE "C" NOT NULL,
    fencing_token BIGINT NOT NULL CHECK (fencing_token > 0),
    reason VARCHAR(64) COLLATE "C" NOT NULL,
    wait_timeout_ms INTEGER NOT NULL CHECK (wait_timeout_ms BETWEEN 100 AND 60000),
    received_at_us BIGINT NOT NULL CHECK (received_at_us > 0),
    dispatched_at_us BIGINT CHECK (dispatched_at_us >= received_at_us)
)"""


def gateway_abort_grants(schema: str, role: str) -> tuple[str, ...]:
    schema, role = checked_sql_name(schema), checked_sql_name(role)
    return (
        f'GRANT USAGE ON SCHEMA "{schema}" TO "{role}"',
        f'GRANT SELECT, INSERT ON "{schema}".{ABORT_INTENT_TABLE} TO "{role}"',
        f'GRANT UPDATE (dispatched_at_us) ON "{schema}".{ABORT_INTENT_TABLE} TO "{role}"',
    )


def gateway_binding_barrier_grants(schema: str, role: str) -> tuple[str, ...]:
    """Give gateway only the fixed binding identity and retirement mode."""
    schema, role = checked_sql_name(schema), checked_sql_name(role)
    return (
        f'GRANT USAGE ON SCHEMA "{schema}" TO "{role}"',
        f"GRANT SELECT (binding_id, revision, revision_id) "
        f'ON "{schema}".speech_binding_revisions TO "{role}"',
        f"GRANT SELECT (kind, object_id, revision, revision_id, mode) "
        f'ON "{schema}".speech_publication_retirements TO "{role}"',
    )
