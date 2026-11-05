"""Durable cancellation fence and bounded reconciliation schedule.

The fence is the gateway-readable projection of an accepted cancellation. The
authoritative acceptance remains ``speech_job_cancellations``; the fence omits
operation/attempt/digest columns so the gateway role needs only job_id,
request_id and acceptance time. It is not stop evidence and never releases
capacity. The gateway reads it before creating an execution; acceptance writes
both rows in one transaction. The reconciliation row is backend-private retry
bookkeeping for replaying stop confirmation after a restart. The not-submitted
row records that a dispatcher observed the accepted cancellation before it
submitted anything; it is not proof by itself, so reconciliation still requires
the gateway to have no submit intent.
"""

from speechnexus_backend.speech.infrastructure.gateway_query_grants import (
    CANCELLATION_FENCE_READ_COLUMNS,
    CANCELLATION_FENCE_TABLE,
)
from speechnexus_backend.speech.infrastructure.job_schema import checked_sql_name

TABLE = CANCELLATION_FENCE_TABLE
RECONCILIATION_TABLE = "speech_job_cancellation_reconciliations"
NOT_SUBMITTED_TABLE = "speech_job_cancellation_not_submitted"

JOB_CANCELLATION_FENCE_DDL = (
    f"""CREATE TABLE {TABLE} (
        job_id VARCHAR(128) COLLATE "C" PRIMARY KEY
            REFERENCES speech_jobs (job_id) ON DELETE RESTRICT,
        accepted_at_us BIGINT NOT NULL
            CHECK (accepted_at_us BETWEEN 0 AND 253402300799999999),
        request_id VARCHAR(128) COLLATE "C" NOT NULL
            CHECK (length(request_id) BETWEEN 1 AND 128)
    )""",
    f"""CREATE TABLE {NOT_SUBMITTED_TABLE} (
        job_id VARCHAR(128) COLLATE "C" PRIMARY KEY
            REFERENCES {TABLE} (job_id) ON DELETE RESTRICT,
        observed_at_us BIGINT NOT NULL
            CHECK (observed_at_us BETWEEN 0 AND 253402300799999999),
        observer VARCHAR(128) COLLATE "C" NOT NULL
            CHECK (length(observer) BETWEEN 1 AND 128)
    )""",
    f"""CREATE TABLE {RECONCILIATION_TABLE} (
        job_id VARCHAR(128) COLLATE "C" PRIMARY KEY
            REFERENCES {TABLE} (job_id) ON DELETE RESTRICT,
        attempts INTEGER NOT NULL DEFAULT 0
            CHECK (attempts BETWEEN 0 AND 64),
        next_check_at_us BIGINT NOT NULL
            CHECK (next_check_at_us BETWEEN 0 AND 253402300799999999),
        last_outcome VARCHAR(24) COLLATE "C"
            CHECK (last_outcome IS NULL OR last_outcome IN (
                'settled', 'not_started', 'pending', 'stop_unconfirmed',
                'terminal_unconfirmed', 'exhausted', 'unavailable'
            )),
        settled_at_us BIGINT
            CHECK (settled_at_us IS NULL
                   OR settled_at_us BETWEEN 0 AND 253402300799999999),
        updated_at_us BIGINT NOT NULL
            CHECK (updated_at_us BETWEEN 0 AND 253402300799999999),
        CHECK (settled_at_us IS NULL OR last_outcome = 'settled')
    )""",
)


def job_cancellation_fence_grants(schema: str, role: str) -> tuple[str, ...]:
    """Acceptance writer: insert/read the projection; no reconciliation writes."""
    schema, role = checked_sql_name(schema), checked_sql_name(role)
    return (
        f'GRANT USAGE ON SCHEMA "{schema}" TO "{role}"',
        f'GRANT SELECT, INSERT ON "{schema}".{TABLE} TO "{role}"',
    )


def cancellation_not_submitted_grants(schema: str, role: str) -> tuple[str, ...]:
    """Dispatcher: only append the observed no-submit fact; never settle."""
    schema, role = checked_sql_name(schema), checked_sql_name(role)
    return (
        f'GRANT USAGE ON SCHEMA "{schema}" TO "{role}"',
        f'GRANT INSERT ON "{schema}".{NOT_SUBMITTED_TABLE} TO "{role}"',
    )


def cancellation_reconciliation_grants(schema: str, role: str) -> tuple[str, ...]:
    """Worker: read the fence/projection, own the ledger, read the observation."""
    schema, role = checked_sql_name(schema), checked_sql_name(role)
    return (
        f'GRANT USAGE ON SCHEMA "{schema}" TO "{role}"',
        f"GRANT SELECT ({', '.join(CANCELLATION_FENCE_READ_COLUMNS)}) "
        f'ON "{schema}".{TABLE} TO "{role}"',
        f'GRANT SELECT, INSERT ON "{schema}".{RECONCILIATION_TABLE} TO "{role}"',
        f"GRANT UPDATE (attempts, next_check_at_us, last_outcome, settled_at_us, updated_at_us) "
        f'ON "{schema}".{RECONCILIATION_TABLE} TO "{role}"',
        f'GRANT SELECT ON "{schema}".{NOT_SUBMITTED_TABLE} TO "{role}"',
    )


def gateway_cancellation_fence_grants(schema: str, role: str) -> tuple[str, ...]:
    """The gateway only needs the same fence columns as the query reader."""
    schema, role = checked_sql_name(schema), checked_sql_name(role)
    return (
        f'GRANT USAGE ON SCHEMA "{schema}" TO "{role}"',
        f"GRANT SELECT ({', '.join(CANCELLATION_FENCE_READ_COLUMNS)}) "
        f'ON "{schema}".{TABLE} TO "{role}"',
    )


__all__ = [
    "JOB_CANCELLATION_FENCE_DDL",
    "NOT_SUBMITTED_TABLE",
    "RECONCILIATION_TABLE",
    "TABLE",
    "cancellation_not_submitted_grants",
    "cancellation_reconciliation_grants",
    "gateway_cancellation_fence_grants",
    "job_cancellation_fence_grants",
]
