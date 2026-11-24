"""Explicit minimum PostgreSQL DML for fresh, non-owner coordination roles.

Run reviewed output as the migration owner, never during application startup.
Roles must have no inherited/global privileges. This does not provision logins.
Application transactions enforce cross-record rules; grants do not replace them.
"""

from __future__ import annotations

from speechnexus_backend.speech.infrastructure.claim_bucket_schema import (
    CLAIM_BUCKET_TABLE,
    CLAIM_BUCKET_UPDATE_COLUMNS,
)
from speechnexus_backend.speech.infrastructure.coordination_keyed_lock_schema import (
    coordination_keyed_lock_grants,
)
from speechnexus_backend.speech.infrastructure.execution_release_schema import release_guard_grants
from speechnexus_backend.speech.infrastructure.job_schema import checked_sql_name
from speechnexus_backend.speech.infrastructure.job_writer_columns import (
    COORDINATION_JOB_INSERT_COLUMNS,
)

DIMENSIONS = (
    "unified_physical_memory_bytes",
    "device_memory_bytes",
    "host_memory_bytes",
    "cpu_decode_millicores",
    "host_pcm_buffer_bytes",
    "scratch_disk_bytes",
)
SPEECH_UPDATES = {
    "resource_pools": "pool_state owner_id fencing_token owner_expires_at_us owner_state version",
    "workspace_quotas": "held_attempts quarantined_attempts version",
    "host_nodes": "node_state version "
    + " ".join(
        f"{prefix}_{dimension}" for prefix in ("held", "quarantined") for dimension in DIMENSIONS
    ),
    "engine_instances": (
        "instance_state held_slots quarantined_slots base_budget_kind ready_at_us "
        "stopped_at_us quarantine_reason last_fencing_token version"
    ),
    "runtime_instance_heads": (
        "current_generation generation_high_water "
        "current_prepare_intent_id current_stop_intent_id version"
    ),
    "runtime_instance_intents": (
        "intent_state stop_evidence_document error_code updated_at_us observation_document "
        "readiness_document version"
    ),
    "admission_attempts": "quota_state version",
    "admission_operations": (
        "current_attempt_id request_document command_redacted_at_us command_deletion_job_id "
        "command_deletion_evidence_digest version"
    ),
    "admission_requests": "",
    "admission_leases": (
        "lease_state cleanup_state budget_released_at_us "
        "quarantine_observed_at_us quarantine_reason_code version"
    ),
    # 派发改用 canonical 租约列:认领/续租/终止都写 `claim_expires_at`(0064 的
    # TIMESTAMP(6)),`job_state` 由 `pending` 推成 `claimed`。草案 epoch 列仍在列里,
    # 因为草案列退役前仍是协调行的可写面(写入者的领取重放路径仍用它)。
    "coordination_jobs": (
        "claim_owner_id claim_expires_at claim_expires_at_us claim_version job_state"
    ),
    "coordination_outbox": "delivered_at_us",
    "runtime_intent_deliveries": "",
    CLAIM_BUCKET_TABLE: " ".join(CLAIM_BUCKET_UPDATE_COLUMNS),
}
GATEWAY_UPDATES = {
    "gateway_fencing_barriers": "min_fencing_token sealed_evidence_digest version",
    "submit_intents": (
        "execution_state cleanup_state engine_request_id dispatched_at_us "
        "staging_output_object_key staging_result_document terminated_at_us "
        "generated_pcm_samples error_code "
        "request_document command_redacted_at_us command_deletion_job_id "
        "command_deletion_evidence_digest updated_at_us version"
    ),
    "execution_stop_evidence": "",
    "output_stream_heads": "version",
    "output_sample_intervals": "",
}
CONTROL_PLANE_TABLES = frozenset({"resource_pools", "workspace_quotas", "host_nodes"})
PRIVATE_SPEECH_TABLES = frozenset(
    {"admission_operations", "admission_requests", "runtime_intent_deliveries"}
)
INSERT_COLUMNS: dict[str, tuple[str, ...]] = {
    # The Job writer's own insertion surface, from the one declaration (#1181): a
    # column added there reaches this role's GRANT without a second edit here.
    "coordination_jobs": COORDINATION_JOB_INSERT_COLUMNS,
    # Not the writer's surface, and deliberately wider: the runtime role appends
    # runtime-intent events with the three columns ``runtime_intent_schema`` adds
    # (``event_kind``, ``target_node_id``, ``intent_id``), while a Job event is
    # exactly the writer's three columns.
    "coordination_outbox": (
        "event_id",
        "job_id",
        "event_digest",
        "event_kind",
        "target_node_id",
        "intent_id",
    ),
    "runtime_intent_deliveries": ("event_id", "delivery_phase", "created_at_us", "updated_at_us"),
}
SPEECH_NARROW_SELECT = {
    "submit_intents": (
        "submit_intent_id lease_id operation_id attempt_id execution_state cleanup_state version"
    )
}
GATEWAY_NARROW_SELECT = {
    "runtime_instance_intents": (
        "target_node_id intent_id instance_id instance_generation action request_digest "
        "intent_state error_code version"
    )
}
#: Rows are created by the schema revision that seeds ``BUCKETS`` buckets, never by a
#: runtime identity: the bucket set is fixed, so the owner role gets no ``INSERT``.
#: The bucket fence is speech-side coordination state the gateway never reads, so the
#: gateway role gets no privilege on this table at all -- not even ``SELECT`` or the
#: ``UPDATE (version)`` row-lock allowance every other coordination table gives it.
SEEDED_SPEECH_TABLES = frozenset({CLAIM_BUCKET_TABLE})


def coordination_runtime_grants(
    schema: str, *, speech_role: str, gateway_role: str
) -> tuple[str, ...]:
    """No DDL, history deletion, foreign private bodies or config inserts/updates.

    Only version-column UPDATE crosses ownership, to permit PostgreSQL row locks.
    Request-body UPDATE is only an owner's deletion-tombstone exception.
    """
    schema, speech_role, gateway_role = map(checked_sql_name, (schema, speech_role, gateway_role))
    if speech_role == gateway_role:
        raise ValueError("Coordination owners require separate runtime roles")
    grants: list[str] = []
    all_updates = {**SPEECH_UPDATES, **GATEWAY_UPDATES}
    for role, owned, narrow in (
        (speech_role, SPEECH_UPDATES, SPEECH_NARROW_SELECT),
        (gateway_role, GATEWAY_UPDATES, GATEWAY_NARROW_SELECT),
    ):
        grants.append(f'GRANT USAGE ON SCHEMA "{schema}" TO "{role}"')
        for table, updates in all_updates.items():
            if table in PRIVATE_SPEECH_TABLES and table not in owned:
                # No gateway read or UPDATE(version) grant: this is A's private
                # original business command and admission identity authority.
                continue
            if table in SEEDED_SPEECH_TABLES and table not in owned:
                # Seeded rows, speech-owned state: the gateway has nothing to read.
                continue
            target = f'"{schema}"."{table}"'
            columns = ", ".join(narrow.get(table, "").split())
            readable = f"SELECT ({columns})" if columns else "SELECT"
            grants.append(f'GRANT {readable} ON {target} TO "{role}"')
            if table in owned:
                if table not in CONTROL_PLANE_TABLES and table not in SEEDED_SPEECH_TABLES:
                    columns = ", ".join(INSERT_COLUMNS.get(table, ()))
                    insert = f"INSERT ({columns})" if columns else "INSERT"
                    grants.append(f'GRANT {insert} ON {target} TO "{role}"')
                if updates:
                    columns = ", ".join(updates.split())
                    grants.append(f'GRANT UPDATE ({columns}) ON {target} TO "{role}"')
            elif "version" in updates.split():
                grants.append(f'GRANT UPDATE (version) ON {target} TO "{role}"')
    # The gateway takes the canonical Job row lock before it reads the cancellation
    # fence (that is what orders submit against acceptance), and PostgreSQL requires
    # UPDATE privilege on some column of a row it locks with `FOR UPDATE`. The
    # fencing counter is the row-lock allowance for this table, exactly like the
    # `version` columns above: the gateway never writes it.
    grants.append(
        f'GRANT UPDATE (claim_version) ON "{schema}".coordination_jobs TO "{gateway_role}"'
    )
    # Both identities of this pair take the binding row lock: the API on the synchronous
    # admission path (``PostgresAdmissionTransactions`` opens by locking the binding) and
    # the gateway before it creates a submit intent. The privileges themselves are
    # declared once in ``coordination_keyed_lock_schema``; provisioning both roles here
    # means one call covers the pair, and ``gateway_keyed_lock_grants`` remains the
    # gateway-only entry for deployments that grant the two sides separately.
    return (
        *grants,
        *coordination_keyed_lock_grants(schema, speech_role),
        *coordination_keyed_lock_grants(schema, gateway_role),
        *release_guard_grants(schema, speech_role),
    )
