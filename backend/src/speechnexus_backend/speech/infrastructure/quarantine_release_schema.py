"""Operator-verified quarantine release evidence and its deployment-wide ledger.

One row records the single release event of one admission lease: the operator
evidence that justified it, the server-side release time, the closed submit
intents and the optional idempotency-key digest. The primary key is the lease,
so a lease can be released at most once; the append-only trigger makes "never
rewritten" a database guarantee instead of an application promise.

The table is owned by A's schema with the backend migration chain, like
``gateway_abort_intent_schema``: the release transaction writes B's stop
evidence and A's capacity counters, and only this dedicated non-owner role
holds the minimum column grants for that one transaction.
"""

from __future__ import annotations

from speechnexus_backend.speech.infrastructure.job_schema import checked_sql_name

TABLE = "admission_quarantine_releases"
TRIGGER = TABLE + "_append_only"
FUNCTION = TABLE + "_reject_mutation"
EVIDENCE_KIND = "operator_verified_instance_terminated"

QUARANTINE_RELEASE_DDL = f"""CREATE TABLE {TABLE} (
    lease_id VARCHAR(128) COLLATE "C" PRIMARY KEY
        REFERENCES admission_leases (lease_id),
    operation_id VARCHAR(128) COLLATE "C" NOT NULL,
    attempt_id VARCHAR(128) COLLATE "C" NOT NULL,
    stage_id VARCHAR(128) COLLATE "C" NOT NULL,
    host_node_id VARCHAR(128) COLLATE "C" NOT NULL,
    instance_id VARCHAR(128) COLLATE "C" NOT NULL,
    instance_generation BIGINT NOT NULL CHECK (instance_generation >= 1),
    fencing_token BIGINT NOT NULL CHECK (fencing_token >= 1),
    idempotency_key_digest VARCHAR(71) COLLATE "C"
        CHECK (idempotency_key_digest ~ '^sha256:[0-9a-f]{{64}}$'),
    request_digest VARCHAR(71) COLLATE "C" NOT NULL
        CHECK (request_digest ~ '^sha256:[0-9a-f]{{64}}$'),
    evidence_kind VARCHAR(64) COLLATE "C" NOT NULL
        CHECK (evidence_kind = '{EVIDENCE_KIND}'),
    evidence_digest VARCHAR(71) COLLATE "C" NOT NULL
        CHECK (evidence_digest ~ '^sha256:[0-9a-f]{{64}}$'),
    evidence_observed_at_us BIGINT NOT NULL CHECK (evidence_observed_at_us >= 0),
    reason VARCHAR(2000) COLLATE "C" NOT NULL
        CHECK (char_length(btrim(reason)) BETWEEN 1 AND 2000),
    budget_released_at_us BIGINT NOT NULL CHECK (budget_released_at_us >= 0),
    released_submit_intent_ids TEXT NOT NULL,
    CHECK (evidence_observed_at_us <= budget_released_at_us),
    CHECK (jsonb_typeof(released_submit_intent_ids::jsonb) = 'array'),
    FOREIGN KEY (lease_id, operation_id, attempt_id)
        REFERENCES admission_leases (lease_id, operation_id, attempt_id)
)"""

QUARANTINE_RELEASE_TRIGGER_DDL = (
    f"""CREATE OR REPLACE FUNCTION {FUNCTION}()
    RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      RAISE EXCEPTION 'quarantine release evidence is append-only'
        USING ERRCODE = '23514';
    END
    $$""",
    f"""CREATE TRIGGER {TRIGGER}
    BEFORE UPDATE OR DELETE ON {TABLE}
    FOR EACH ROW EXECUTE FUNCTION {FUNCTION}()""",
)

#: Tables this release role reads in full. ``resource_pools`` is deliberately
#: absent: the transaction only locks the parent row, so it selects ``pool_id``.
FULL_READ = frozenset(
    {
        "workspace_quotas",
        "host_nodes",
        "engine_instances",
        "admission_attempts",
        "admission_leases",
        "execution_stop_evidence",
        TABLE,
    }
)

#: The narrow reads: a lock target and B's immutable submission facts. The
#: release decision needs the recorded/dispatched times for the observed-at
#: bounds; command bodies, engine identities and staging keys stay unreadable.
READ_COLUMNS: dict[str, frozenset[str]] = {
    "resource_pools": frozenset({"pool_id"}),
    "submit_intents": frozenset(
        {
            "submit_intent_id",
            "lease_id",
            "operation_id",
            "attempt_id",
            "execution_state",
            "cleanup_state",
            "recorded_at_us",
            "dispatched_at_us",
            "terminated_at_us",
            "version",
        }
    ),
}

#: Tables this role inserts into in full: one operator stop row and one ledger row.
FULL_INSERT = frozenset({"execution_stop_evidence", TABLE})

#: Every column this role may update, per table. No control-plane configuration
#: column appears here: release never lowers a threshold, raises a ceiling or
#: returns a host to active.
UPDATE_COLUMNS: dict[str, frozenset[str]] = {
    "resource_pools": frozenset({"version"}),
    "workspace_quotas": frozenset({"held_attempts", "quarantined_attempts", "version"}),
    "host_nodes": frozenset(
        {
            f"{prefix}_{dimension}"
            for prefix in ("held", "quarantined")
            for dimension in (
                "unified_physical_memory_bytes",
                "device_memory_bytes",
                "host_memory_bytes",
                "cpu_decode_millicores",
                "host_pcm_buffer_bytes",
                "scratch_disk_bytes",
            )
        }
        | {"version"}
    ),
    "engine_instances": frozenset({"held_slots", "quarantined_slots", "version"}),
    "admission_attempts": frozenset({"quota_state", "version"}),
    "admission_leases": frozenset(
        {"lease_state", "cleanup_state", "budget_released_at_us", "version"}
    ),
    "submit_intents": frozenset(
        {"execution_state", "cleanup_state", "terminated_at_us", "updated_at_us", "version"}
    ),
}


def quarantine_release_grants(schema: str, role: str) -> tuple[str, ...]:
    """Reviewed minimum DML for the dedicated non-owner release role.

    Run as the migration owner, never during application startup. The grants do
    not provision a login, give schema ``CREATE`` or reach any table outside the
    release decision; startup verification compares these same declarations
    against the live catalog column by column.
    """
    schema, role = checked_sql_name(schema), checked_sql_name(role)
    statements: list[str] = [f'GRANT USAGE ON SCHEMA "{schema}" TO "{role}"']
    for table in sorted(FULL_READ | set(READ_COLUMNS) | FULL_INSERT | set(UPDATE_COLUMNS)):
        target = f'"{schema}"."{table}"'
        if table in FULL_READ:
            statements.append(f'GRANT SELECT ON {target} TO "{role}"')
        elif table in READ_COLUMNS:
            columns = ", ".join(sorted(READ_COLUMNS[table]))
            statements.append(f'GRANT SELECT ({columns}) ON {target} TO "{role}"')
        if table in FULL_INSERT:
            statements.append(f'GRANT INSERT ON {target} TO "{role}"')
        if table in UPDATE_COLUMNS:
            columns = ", ".join(sorted(UPDATE_COLUMNS[table]))
            statements.append(f'GRANT UPDATE ({columns}) ON {target} TO "{role}"')
    return tuple(statements)


__all__ = [
    "EVIDENCE_KIND",
    "FULL_INSERT",
    "FULL_READ",
    "QUARANTINE_RELEASE_DDL",
    "QUARANTINE_RELEASE_TRIGGER_DDL",
    "READ_COLUMNS",
    "TABLE",
    "TRIGGER",
    "UPDATE_COLUMNS",
    "quarantine_release_grants",
]
