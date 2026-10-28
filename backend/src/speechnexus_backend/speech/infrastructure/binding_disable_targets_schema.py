"""Durable immutable execution targets for a binding-disable coordinator."""

from speechnexus_backend.speech.infrastructure.job_schema import checked_sql_name

TARGET_TABLE = "speech_binding_disable_targets"
ABORT_INTENT_TABLE = "speech_binding_disable_abort_intents"

TARGET_DDL = f"""CREATE TABLE {TARGET_TABLE} (
    event_id VARCHAR(128) COLLATE "C" NOT NULL
        REFERENCES speech_binding_disable_intents (event_id),
    submit_intent_id VARCHAR(128) COLLATE "C" NOT NULL REFERENCES submit_intents (submit_intent_id),
    operation_id VARCHAR(128) COLLATE "C" NOT NULL,
    attempt_id VARCHAR(128) COLLATE "C" NOT NULL,
    lease_id VARCHAR(128) COLLATE "C" NOT NULL,
    stage_id VARCHAR(128) COLLATE "C" NOT NULL,
    workspace_id VARCHAR(128) COLLATE "C" NOT NULL,
    pool_id VARCHAR(128) COLLATE "C" NOT NULL,
    gateway_id VARCHAR(128) COLLATE "C" NOT NULL,
    host_node_id VARCHAR(128) COLLATE "C" NOT NULL,
    instance_id VARCHAR(128) COLLATE "C" NOT NULL,
    instance_generation BIGINT NOT NULL CHECK (instance_generation >= 1),
    binding_revision_id VARCHAR(128) COLLATE "C" NOT NULL,
    policy_revision_id VARCHAR(128) COLLATE "C" NOT NULL,
    voice_revision_id VARCHAR(128) COLLATE "C",
    fencing_token BIGINT NOT NULL CHECK (fencing_token >= 1),
    command_digest VARCHAR(71) COLLATE "C" NOT NULL
        CHECK (command_digest ~ '^sha256:[0-9a-f]{{64}}$'),
    wait_timeout_ms INTEGER NOT NULL CHECK (wait_timeout_ms BETWEEN 100 AND 60000),
    PRIMARY KEY (event_id, submit_intent_id)
)"""

ABORT_INTENT_DDL = f"""CREATE TABLE {ABORT_INTENT_TABLE} (
    event_id VARCHAR(128) COLLATE "C" NOT NULL,
    submit_intent_id VARCHAR(128) COLLATE "C" NOT NULL,
    recorded_at_us BIGINT NOT NULL CHECK (recorded_at_us > 0),
    PRIMARY KEY (event_id, submit_intent_id),
    FOREIGN KEY (event_id, submit_intent_id)
        REFERENCES {TARGET_TABLE} (event_id, submit_intent_id)
)"""


def disable_target_grants(schema: str, role: str) -> tuple[str, ...]:
    """Grant only append/read of fixed targets; source reads are separate."""
    schema, role = checked_sql_name(schema), checked_sql_name(role)
    return (
        f'GRANT USAGE ON SCHEMA "{schema}" TO "{role}"',
        f'GRANT SELECT, INSERT ON "{schema}".{TARGET_TABLE} TO "{role}"',
        f'GRANT SELECT, INSERT ON "{schema}".{ABORT_INTENT_TABLE} TO "{role}"',
    )


def disable_coordinator_grants(schema: str, role: str) -> tuple[str, ...]:
    """Explicit least-privilege declaration; the application never executes grants."""
    schema, role = checked_sql_name(schema), checked_sql_name(role)
    target = f'"{role}"'
    statements = (
        ("speech_binding_disable_intents", "event_id"),
        ("speech_publication_lifecycle_events", "event_id, revision_id, action"),
        (
            "admission_attempts",
            "operation_id, attempt_id, workspace_id, binding_revision_id, policy_revision_id",
        ),
        (
            "admission_leases",
            "operation_id, attempt_id, lease_id, stage_id, pool_id, gateway_id, "
            "host_node_id, instance_id, instance_generation, fencing_token, lease_state",
        ),
        (
            "submit_intents",
            "submit_intent_id, operation_id, attempt_id, lease_id, voice_revision_id, "
            "command_digest, execution_state",
        ),
    )
    return (
        *disable_target_grants(schema, role),
        *(
            f'GRANT SELECT ({columns}) ON "{schema}".{table} TO {target}'
            for table, columns in statements
        ),
    )


def disable_settlement_grants(schema: str, role: str) -> tuple[str, ...]:
    """Provision only the lease-settlement transaction's reads and writes."""
    from speechnexus_backend.speech.infrastructure.coordination_grants import DIMENSIONS

    schema, role = checked_sql_name(schema), checked_sql_name(role)
    target = f'"{role}"'
    reads = (
        "resource_pools",
        "workspace_quotas",
        "host_nodes",
        "engine_instances",
        "admission_attempts",
        "admission_leases",
    )
    updates = {
        "resource_pools": "version",
        "workspace_quotas": "held_attempts, quarantined_attempts, version",
        "host_nodes": ", ".join(
            [
                f"{prefix}_{dimension}"
                for prefix in ("held", "quarantined")
                for dimension in DIMENSIONS
            ]
            + ["version"]
        ),
        "engine_instances": "held_slots, quarantined_slots, version",
        "admission_attempts": "quota_state, version",
        "admission_leases": "lease_state, cleanup_state, budget_released_at_us, version",
        "submit_intents": "version",
    }
    return (
        f'GRANT USAGE ON SCHEMA "{schema}" TO {target}',
        *(f'GRANT SELECT ON "{schema}".{table} TO {target}' for table in reads),
        f"GRANT SELECT (submit_intent_id, lease_id, operation_id, attempt_id, execution_state, "
        f'cleanup_state, version) ON "{schema}".submit_intents TO {target}',
        "GRANT SELECT (evidence_id, submit_intent_id, evidence_kind, evidence_digest, "
        "observed_at_us, "
        f'execution_stopped, resources_released) ON "{schema}".execution_stop_evidence TO {target}',
        *(
            f'GRANT UPDATE ({columns}) ON "{schema}".{table} TO {target}'
            for table, columns in updates.items()
        ),
    )
