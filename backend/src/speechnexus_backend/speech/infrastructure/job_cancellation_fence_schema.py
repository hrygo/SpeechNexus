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
    CANCELLATION_FENCE_GATEWAY_COLUMNS,
    CANCELLATION_FENCE_TABLE,
    CANCELLATION_FENCE_WORKER_COLUMNS,
)
from speechnexus_backend.speech.infrastructure.job_schema import checked_sql_name
from speechnexus_backend.speech.infrastructure.job_writer_columns import (
    CANCELLATION_FENCE_INSERT_COLUMNS,
    CANCELLATION_NOT_SUBMITTED_INSERT_COLUMNS,
    CANCELLATION_RECONCILIATION_INSERT_COLUMNS,
)

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
        f'GRANT SELECT ON "{schema}".{TABLE} TO "{role}"',
        f"GRANT INSERT ({', '.join(CANCELLATION_FENCE_INSERT_COLUMNS)}) "
        f'ON "{schema}".{TABLE} TO "{role}"',
    )


def cancellation_not_submitted_grants(schema: str, role: str) -> tuple[str, ...]:
    """Dispatcher: only append the observed no-submit fact; never settle."""
    schema, role = checked_sql_name(schema), checked_sql_name(role)
    return (
        f'GRANT USAGE ON SCHEMA "{schema}" TO "{role}"',
        f"GRANT INSERT ({', '.join(CANCELLATION_NOT_SUBMITTED_INSERT_COLUMNS)}) "
        f'ON "{schema}".{NOT_SUBMITTED_TABLE} TO "{role}"',
    )


def cancellation_reconciliation_grants(schema: str, role: str) -> tuple[str, ...]:
    """Worker: read the complete fence projection, ledger, and observation."""
    schema, role = checked_sql_name(schema), checked_sql_name(role)
    return (
        f'GRANT USAGE ON SCHEMA "{schema}" TO "{role}"',
        f"GRANT SELECT ({', '.join(CANCELLATION_FENCE_WORKER_COLUMNS)}) "
        f'ON "{schema}".{TABLE} TO "{role}"',
        f'GRANT SELECT ON "{schema}".{RECONCILIATION_TABLE} TO "{role}"',
        f"GRANT INSERT ({', '.join(CANCELLATION_RECONCILIATION_INSERT_COLUMNS)}) "
        f'ON "{schema}".{RECONCILIATION_TABLE} TO "{role}"',
        f"GRANT UPDATE (attempts, next_check_at_us, last_outcome, settled_at_us, updated_at_us) "
        f'ON "{schema}".{RECONCILIATION_TABLE} TO "{role}"',
        f'GRANT SELECT ON "{schema}".{NOT_SUBMITTED_TABLE} TO "{role}"',
    )


def cancellation_debt_reader_grants(schema: str, role: str) -> tuple[str, ...]:
    """Optional read-only identity for both readiness debt diagnostics.

    Serves ``cancellation_debt`` (accepted cancellations that cannot return
    capacity) and its sibling ``stop_evidence_debt`` (``running`` Jobs whose gateway
    reported failed/aborted without a verified stop, #917 review M4). They read the
    same tables and are deliberately reported under different prefixes because their
    dispositions differ, so one identity covers both.

    Not granted to any runtime identity by default: the API identity must not be
    able to read the cancellation ledger, the speech owner's leases or the
    gateway's submit intents just to fill a readiness detail (#852 review P1). A
    deployment that wants the full ``Job -> lease -> states`` detail provisions
    this separate role and points ``SPEECHNEXUS_READINESS_CANCELLATION_DSN`` at
    it; without it the probes report ``unavailable (not configured)``.

    Column-level on purpose: no canonical request document, no reconciliation
    writes, no capacity accounting beyond the lease state the detail prints. The
    dispatch-debt probe additionally needs the row's own dispatch proof
    (``cleanup_state``, ``dispatched_at_us``, ``engine_request_id``) to tell "the
    engine was never asked" from "the engine was asked and nothing proved it
    stopped" -- the two shapes the dispatch gate settles and holds respectively.

    The cancellation-debt probe reads the ledger's ``last_outcome`` and
    ``next_check_at_us`` as well: they are what tell "permanently unclaimable"
    (``exhausted``) and "claimable right now" from "never claimed". Those two
    columns are schedule state, not request content, so the identity stays the
    read-only diagnostic it was.
    """
    schema, role = checked_sql_name(schema), checked_sql_name(role)
    return (
        f'GRANT USAGE ON SCHEMA "{schema}" TO "{role}"',
        f'GRANT SELECT (job_id, accepted_at_us) ON "{schema}".{TABLE} TO "{role}"',
        f"GRANT SELECT (job_id, settled_at_us, last_outcome, next_check_at_us) "
        f'ON "{schema}".{RECONCILIATION_TABLE} TO "{role}"',
        f'GRANT SELECT (job_id, state, updated_at_us) ON "{schema}".speech_jobs TO "{role}"',
        f"GRANT SELECT (lease_id, operation_id, lease_state) "
        f'ON "{schema}".admission_leases TO "{role}"',
        # ``submit_intent_id`` is the join key to the terminal fact, not the
        # canonical command: it names the intent the gateway answered about.
        f"GRANT SELECT (submit_intent_id, lease_id, execution_state, cleanup_state, "
        f"dispatched_at_us, engine_request_id) "
        f'ON "{schema}".submit_intents TO "{role}"',
        # The subclass label needs the gateway's terminal fact: without it a
        # ``succeeded`` execution that never recorded ``engine_terminal_response``
        # would be labelled with an exit that always refuses it (#338's shape).
        f"GRANT SELECT (submit_intent_id, evidence_kind, execution_stopped, "
        f'resources_released) ON "{schema}".execution_stop_evidence TO "{role}"',
    )


def gateway_cancellation_fence_grants(schema: str, role: str) -> tuple[str, ...]:
    """The gateway only needs the existence key, not acceptance metadata."""
    schema, role = checked_sql_name(schema), checked_sql_name(role)
    return (
        f'GRANT USAGE ON SCHEMA "{schema}" TO "{role}"',
        f"GRANT SELECT ({', '.join(CANCELLATION_FENCE_GATEWAY_COLUMNS)}) "
        f'ON "{schema}".{TABLE} TO "{role}"',
    )


__all__ = [
    "JOB_CANCELLATION_FENCE_DDL",
    "NOT_SUBMITTED_TABLE",
    "RECONCILIATION_TABLE",
    "TABLE",
    "cancellation_debt_reader_grants",
    "cancellation_not_submitted_grants",
    "cancellation_reconciliation_grants",
    "gateway_cancellation_fence_grants",
    "job_cancellation_fence_grants",
]
