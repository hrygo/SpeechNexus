"""Append-only schema definition for private runtime transport recovery (0008)."""

from __future__ import annotations

from speechnexus_backend.speech.infrastructure.job_schema import checked_sql_name

RUNTIME_DELIVERY_DDL: tuple[str, ...] = (
    """CREATE TABLE runtime_intent_deliveries (
        event_id VARCHAR(128) COLLATE "C" PRIMARY KEY,
        delivery_phase VARCHAR(24) COLLATE "C" NOT NULL
            CHECK (delivery_phase IN ('unsent','unknown','acknowledged','blocked')),
        claim_holder_id VARCHAR(128) COLLATE "C",
        claim_expires_at_us BIGINT NOT NULL DEFAULT 0 CHECK (claim_expires_at_us >= 0),
        next_attempt_at_us BIGINT NOT NULL DEFAULT 0 CHECK (next_attempt_at_us >= 0),
        submission_started_at_us BIGINT,
        acknowledged_at_us BIGINT,
        last_failure_code VARCHAR(64) COLLATE "C",
        created_at_us BIGINT NOT NULL CHECK (created_at_us BETWEEN 1 AND 9007199254740991),
        updated_at_us BIGINT NOT NULL,
        version BIGINT NOT NULL DEFAULT 1 CHECK (version >= 1),
        FOREIGN KEY (event_id) REFERENCES coordination_outbox (event_id),
        CHECK (updated_at_us BETWEEN created_at_us AND 9007199254740991),
        CHECK ((claim_holder_id IS NULL AND claim_expires_at_us = 0)
            OR (claim_holder_id IS NOT NULL AND claim_expires_at_us > 0)),
        CHECK (submission_started_at_us IS NULL
            OR submission_started_at_us BETWEEN created_at_us AND 9007199254740991),
        CHECK ((delivery_phase = 'acknowledged' AND acknowledged_at_us IS NOT NULL
            AND acknowledged_at_us BETWEEN created_at_us AND 9007199254740991)
            OR (delivery_phase <> 'acknowledged' AND acknowledged_at_us IS NULL)),
        CHECK (delivery_phase <> 'unsent' OR submission_started_at_us IS NULL)
    )""",
    """CREATE INDEX idx_runtime_delivery_due
        ON runtime_intent_deliveries
            (delivery_phase, next_attempt_at_us, claim_expires_at_us, event_id)""",
)

# An old outbox row may already have reached its controller. Lack of a new
# delivery ledger is not proof of non-dispatch. Migration never calls a node.
ADOPT_RUNTIME_DELIVERIES_SQL = """INSERT INTO runtime_intent_deliveries
    (event_id, delivery_phase, created_at_us, updated_at_us, last_failure_code)
    SELECT o.event_id, 'unknown', stamp.now_us, stamp.now_us, 'legacy_adoption_required'
    FROM coordination_outbox AS o CROSS JOIN (SELECT %s AS now_us) AS stamp
    WHERE o.event_kind = 'runtime' AND o.delivered_at_us IS NULL"""


def runtime_delivery_grants(schema: str, role: str) -> tuple[str, ...]:
    """Dispatch role: no creation, DDL, history deletion or physical authority."""
    schema, role = checked_sql_name(schema), checked_sql_name(role)
    tables = {
        "coordination_outbox": (
            "event_id event_digest delivered_at_us event_kind target_node_id intent_id"
        ),
        "runtime_instance_intents": (
            "target_node_id intent_id instance_id instance_generation action request_digest "
            "request_document intent_state observation_document"
        ),
        "runtime_instance_heads": (
            "instance_id current_generation current_prepare_intent_id current_stop_intent_id"
        ),
        "engine_instances": "instance_id instance_generation instance_state base_budget_kind",
    }
    statements = [f'GRANT USAGE ON SCHEMA "{schema}" TO "{role}"']
    for table, fields in tables.items():
        columns = ", ".join(fields.split())
        statements.append(f'GRANT SELECT ({columns}) ON "{schema}"."{table}" TO "{role}"')
    statements.extend(
        (
            f'GRANT SELECT ON "{schema}".runtime_intent_deliveries TO "{role}"',
            "GRANT UPDATE (delivery_phase, claim_holder_id, claim_expires_at_us, "
            "next_attempt_at_us, submission_started_at_us, acknowledged_at_us, last_failure_code, "
            f'updated_at_us, version) ON "{schema}".runtime_intent_deliveries TO "{role}"',
            f'GRANT UPDATE (delivered_at_us) ON "{schema}".coordination_outbox TO "{role}"',
        )
    )
    return tuple(statements)
