"""Gateway-owned abort receipt, original origin and bounded dispatch attempts.

The receipt records the complete original execution identity once. The attempt
columns are the only mutable part: one lease at a time may authorize an external
abort, and its outcome decides whether the same receipt may be attempted again.
No column carries command content, engine addresses or release claims.
"""

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

#: The bounded dispatch attempt of one original cancellation receipt.
#:
#: ``instance_id``/``instance_generation`` complete the recorded origin so recovery
#: can re-verify the authoritative execution before any send. Exactly one attempt
#: may hold a live lease; a decided outcome is terminal for the receipt.
ABORT_DISPATCH_COLUMNS_DDL = (
    f'ALTER TABLE {ABORT_INTENT_TABLE} ADD COLUMN instance_id VARCHAR(128) COLLATE "C"',
    f"ALTER TABLE {ABORT_INTENT_TABLE} ADD COLUMN instance_generation BIGINT",
    f'ALTER TABLE {ABORT_INTENT_TABLE} ADD COLUMN dispatch_attempt_id VARCHAR(128) COLLATE "C"',
    f"ALTER TABLE {ABORT_INTENT_TABLE} ADD COLUMN dispatch_started_at_us BIGINT",
    f"ALTER TABLE {ABORT_INTENT_TABLE} ADD COLUMN dispatch_deadline_at_us BIGINT",
    f'ALTER TABLE {ABORT_INTENT_TABLE} ADD COLUMN dispatch_outcome VARCHAR(32) COLLATE "C"',
    f"ALTER TABLE {ABORT_INTENT_TABLE} ADD COLUMN dispatch_outcome_at_us BIGINT",
    f"ALTER TABLE {ABORT_INTENT_TABLE} ADD COLUMN dispatch_attempts INTEGER NOT NULL DEFAULT 0",
)

#: Existing receipts already name an execution, so their origin is backfillable;
#: a receipt that cannot resolve its lease cannot be preserved and fails the migration.
ABORT_DISPATCH_BACKFILL = (
    f"UPDATE {ABORT_INTENT_TABLE} AS a "
    "SET instance_id = l.instance_id, instance_generation = l.instance_generation "
    "FROM submit_intents AS s JOIN admission_leases AS l ON l.lease_id = s.lease_id "
    "WHERE s.submit_intent_id = a.submit_intent_id",
    f"ALTER TABLE {ABORT_INTENT_TABLE} ALTER COLUMN instance_id SET NOT NULL",
    f"ALTER TABLE {ABORT_INTENT_TABLE} ALTER COLUMN instance_generation SET NOT NULL",
    f"UPDATE {ABORT_INTENT_TABLE} SET dispatch_attempts = 1 WHERE dispatched_at_us IS NOT NULL",
)

ABORT_DISPATCH_CONSTRAINTS_DDL = (
    f"ALTER TABLE {ABORT_INTENT_TABLE} "
    "ADD CONSTRAINT gateway_abort_instance_generation_check "
    "CHECK (instance_generation >= 1)",
    f"ALTER TABLE {ABORT_INTENT_TABLE} "
    "ADD CONSTRAINT gateway_abort_dispatch_attempt_check CHECK ("
    "(dispatch_attempt_id IS NULL AND dispatch_started_at_us IS NULL "
    "AND dispatch_deadline_at_us IS NULL AND dispatch_outcome IS NULL "
    "AND dispatch_outcome_at_us IS NULL)"
    " OR (dispatch_attempt_id IS NOT NULL AND dispatch_started_at_us IS NOT NULL "
    "AND dispatch_deadline_at_us IS NOT NULL "
    "AND dispatch_deadline_at_us > dispatch_started_at_us "
    "AND ((dispatch_outcome IS NULL AND dispatch_outcome_at_us IS NULL) "
    "OR (dispatch_outcome IN ('acknowledged','unconfirmed','exhausted') "
    "AND dispatch_outcome_at_us IS NOT NULL))))",
    f"ALTER TABLE {ABORT_INTENT_TABLE} "
    "ADD CONSTRAINT gateway_abort_dispatch_attempts_check "
    "CHECK (dispatch_attempts >= 0)",
)


def gateway_abort_grants(schema: str, role: str) -> tuple[str, ...]:
    schema, role = checked_sql_name(schema), checked_sql_name(role)
    return (
        f'GRANT USAGE ON SCHEMA "{schema}" TO "{role}"',
        f'GRANT SELECT, INSERT ON "{schema}".{ABORT_INTENT_TABLE} TO "{role}"',
        "GRANT UPDATE (dispatched_at_us, next_check_at_us, dispatch_attempt_id, "
        "dispatch_started_at_us, dispatch_deadline_at_us, dispatch_outcome, "
        "dispatch_outcome_at_us, dispatch_attempts) "
        f'ON "{schema}".{ABORT_INTENT_TABLE} TO "{role}"',
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
