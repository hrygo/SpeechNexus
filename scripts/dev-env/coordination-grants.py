"""Render minimum runtime grants for the ADR 0006 draft; never connect to a DB.

Use fresh non-owner runtime roles. Review/install the output as the migration
role. Existing inherited/global grants must be audited separately.

Runtime roles receive only the DML that controlled transactions need: their own
runtime projection columns, their own append-only rows, and row-lock access to
the other owner's `version`. Policy, identity and capacity configuration stay
with the control plane, so a runtime role cannot raise its own attempt quota,
replace a topology identity, widen allocatable capacity or lower the isolation
threshold. A role also never reads another owner's canonical request document;
`*_NARROW_SELECT` lists the identity and state columns it actually needs.
The MySQL output keeps `'role'@'%'`; narrow the host pattern to the approved
hosts or network ranges before installing it.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

SPEECH_UPDATES = {
    "resource_pools": "pool_state owner_id fencing_token owner_expires_at_us owner_state version",
    "workspace_quotas": "held_attempts quarantined_attempts version",
    "host_nodes": "node_state version",
    "engine_instances": (
        "instance_state held_slots quarantined_slots base_budget_kind ready_at_us "
        "stopped_at_us quarantine_reason last_fencing_token version"
    ),
    "runtime_instance_heads": (
        "current_generation generation_high_water "
        "current_prepare_intent_id current_stop_intent_id version"
    ),
    "runtime_instance_intents": "intent_state stop_evidence_document error_code version",
    "admission_attempts": "quota_state version",
    "admission_leases": (
        "lease_state cleanup_state budget_released_at_us "
        "quarantine_observed_at_us quarantine_reason_code version"
    ),
    "coordination_jobs": "claim_owner_id claim_expires_at_us claim_version job_state",
    "coordination_outbox": "delivered_at_us",
}
GATEWAY_UPDATES = {
    "gateway_fencing_barriers": "min_fencing_token sealed_evidence_digest version",
    "submit_intents": (
        "execution_state cleanup_state engine_request_id dispatched_at_us "
        "staging_output_object_key terminated_at_us generated_pcm_samples error_code "
        "request_document command_redacted_at_us command_deletion_job_id "
        "command_deletion_evidence_digest updated_at_us version"
    ),
    "execution_stop_evidence": "",
    "output_stream_heads": "version",
    "output_sample_intervals": "",
}
# Non-owner reads of tables holding a canonical private request document.
SPEECH_NARROW_SELECT = {
    "submit_intents": (
        "submit_intent_id lease_id operation_id attempt_id execution_state cleanup_state version"
    ),
}
GATEWAY_NARROW_SELECT = {
    "runtime_instance_intents": (
        "target_node_id intent_id instance_id instance_generation action request_digest "
        "intent_state error_code version"
    ),
}
_CONTRACT = (
    Path(__file__).resolve().parents[2] / "contracts/internal/coordination-transactions.schema.json"
)
DIMENSIONS: tuple[str, ...] = tuple(
    json.loads(_CONTRACT.read_text())["$defs"]["PhysicalReservation"]["properties"]
)


def statements(dialect: str, namespace: str, speech_role: str, gateway_role: str) -> list[str]:
    for value in (namespace, speech_role, gateway_role, *DIMENSIONS):
        if not re.fullmatch(r"[a-z][a-z0-9_]{2,62}", value):
            raise ValueError("namespace and role names must be safe lowercase SQL identifiers")
    if dialect not in ("postgresql", "mysql"):
        raise ValueError("unsupported dialect")
    for narrow, owned in (
        (SPEECH_NARROW_SELECT, SPEECH_UPDATES),
        (GATEWAY_NARROW_SELECT, GATEWAY_UPDATES),
    ):
        if set(narrow) & set(owned):
            raise ValueError("column-level reads are for non-owner roles only")
    updates = {**SPEECH_UPDATES, **GATEWAY_UPDATES}
    updates["host_nodes"] += " " + " ".join(
        f"{prefix}_{dimension}" for prefix in ("held", "quarantined") for dimension in DIMENSIONS
    )
    quote = '"' if dialect == "postgresql" else "`"
    grants = []
    for role, owned, narrow in (
        (speech_role, SPEECH_UPDATES, SPEECH_NARROW_SELECT),
        (gateway_role, GATEWAY_UPDATES, GATEWAY_NARROW_SELECT),
    ):
        recipient = f'"{role}"' if dialect == "postgresql" else f"'{role}'@'%'"
        if dialect == "postgresql":
            grants.append(f'GRANT USAGE ON SCHEMA "{namespace}" TO {recipient}')
        for table in updates:
            target = f"{quote}{namespace}{quote}.{quote}{table}{quote}"
            readable = narrow.get(table, "")
            if readable:
                columns = ", ".join(readable.split())
                grants.append(f"GRANT SELECT ({columns}) ON {target} TO {recipient}")
            else:
                grants.append(f"GRANT SELECT ON {target} TO {recipient}")
            if table in owned:
                grants.append(f"GRANT INSERT ON {target} TO {recipient}")
                if updates[table]:
                    columns = ", ".join(updates[table].split())
                    grants.append(f"GRANT UPDATE ({columns}) ON {target} TO {recipient}")
            elif "version" in updates[table].split():
                # SELECT FOR UPDATE requires a PostgreSQL UPDATE privilege.
                grants.append(f"GRANT UPDATE (version) ON {target} TO {recipient}")
    return grants


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", required=True, choices=("postgresql", "mysql"))
    parser.add_argument("--namespace", required=True)
    parser.add_argument("--speech-role", required=True)
    parser.add_argument("--gateway-role", required=True)
    args = parser.parse_args()
    print(
        ";\n".join(statements(args.database, args.namespace, args.speech_role, args.gateway_role))
        + ";"
    )


if __name__ == "__main__":
    main()
