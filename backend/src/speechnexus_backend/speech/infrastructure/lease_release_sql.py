"""Lease settlement reads B's immutable facts and updates A's projections only."""

import json
from collections.abc import Callable

from speechnexus_backend.speech.domain.lease_release import (
    LeaseStop,
    ReleaseLease,
    SubmitReleaseFacts,
    release,
    release_already_recorded,
    require_complete_stages,
)
from speechnexus_backend.speech.domain.leases import (
    LeaseAccounting,
    LeaseRejectedError,
    LeaseResult,
)
from speechnexus_backend.speech.infrastructure.coordination_grants import DIMENSIONS
from speechnexus_backend.speech.infrastructure.leases_sql import (
    LeaseCursor,
    _one,
    _record,
    _rows,
    _update,
    _vector,
)


def release_in_transaction(
    cursor: LeaseCursor,
    request: ReleaseLease,
    *,
    clock: Callable[[], int],
) -> LeaseResult:
    hint = _one(cursor, "SELECT * FROM admission_leases WHERE lease_id = %s", (request.lease_id,))
    identity = (hint["operation_id"], hint["attempt_id"])
    attempt_hint = _one(
        cursor,
        "SELECT * FROM admission_attempts WHERE operation_id = %s AND attempt_id = %s",
        identity,
    )
    _one(
        cursor,
        "SELECT pool_id FROM resource_pools WHERE pool_id = %s FOR UPDATE",
        (hint["pool_id"],),
    )
    quota = _one(
        cursor,
        "SELECT * FROM workspace_quotas WHERE workspace_id = %s FOR UPDATE",
        (attempt_hint["workspace_id"],),
    )
    host = _one(
        cursor,
        "SELECT * FROM host_nodes WHERE host_node_id = %s FOR UPDATE",
        (hint["host_node_id"],),
    )
    instance_key = (hint["instance_id"], hint["instance_generation"])
    instance = _one(
        cursor,
        "SELECT * FROM engine_instances WHERE instance_id = %s "
        "AND instance_generation = %s FOR UPDATE",
        instance_key,
    )
    attempt = _one(
        cursor,
        "SELECT * FROM admission_attempts WHERE operation_id = %s AND attempt_id = %s FOR UPDATE",
        identity,
    )
    lease = _one(
        cursor, "SELECT * FROM admission_leases WHERE lease_id = %s FOR UPDATE", (request.lease_id,)
    )
    frozen = (
        "lease_id",
        "operation_id",
        "attempt_id",
        "stage_id",
        "pool_id",
        "host_node_id",
        "instance_id",
        "instance_generation",
        "gateway_id",
        "fencing_token",
        "reserved_slots",
        *("reserved_" + name for name in DIMENSIONS),
    )
    if (
        any(hint[key] != lease[key] for key in frozen)
        or attempt["workspace_id"] != attempt_hint["workspace_id"]
        or any(instance[key] != lease[key] for key in ("host_node_id", "pool_id", "gateway_id"))
    ):
        raise LeaseRejectedError("reconciliation_required")
    fact = _record(lease)
    if fact.lease_state == "released":
        release_already_recorded(fact, request, clock())
        return LeaseResult("replayed", request.lease_id, fact)

    submits = _rows(
        cursor,
        "SELECT submit_intent_id, lease_id, operation_id, attempt_id, execution_state, "
        "cleanup_state, version FROM submit_intents WHERE lease_id = %s "
        "ORDER BY submit_intent_id FOR UPDATE",
        (request.lease_id,),
    )
    if len(submits) > 1 or any(
        row[key] != lease[key]
        for row in submits
        for key in ("lease_id", "operation_id", "attempt_id")
    ):
        raise LeaseRejectedError("reconciliation_required")
    execution: SubmitReleaseFacts | None = None
    if submits:
        submit = submits[0]
        # Immutable evidence needs SELECT only. B takes the already-held pool/
        # submit locks before appending it, and neither runtime role can delete it.
        stops = _rows(
            cursor,
            "SELECT submit_intent_id, evidence_kind, evidence_digest, observed_at_us, "
            "execution_stopped, resources_released FROM execution_stop_evidence "
            "WHERE submit_intent_id = %s ORDER BY evidence_id",
            (submit["submit_intent_id"],),
        )
        if len(stops) > 1:
            raise LeaseRejectedError("reconciliation_required")
        stop = None
        if stops:
            row = stops[0]
            if row["execution_stopped"] != 1 or row["resources_released"] != 1:
                raise LeaseRejectedError("reconciliation_required")
            stop = LeaseStop(
                row["submit_intent_id"],
                row["evidence_kind"],
                row["evidence_digest"],
                row["observed_at_us"],
            )
        execution = SubmitReleaseFacts(
            submit["submit_intent_id"], submit["execution_state"], submit["cleanup_state"], stop
        )
    now_us = clock()  # Database time after the last possible submit-lock wait.
    # The attempt lock serializes every A lease mutation/admission. Siblings
    # need no inverted cross-host/pool lease locks; B cannot modify their state.
    siblings = _rows(
        cursor,
        "SELECT * FROM admission_leases WHERE operation_id = %s "
        "AND attempt_id = %s ORDER BY lease_id",
        identity,
    )
    required = json.loads(attempt["required_stage_ids"])
    if not isinstance(required, list) or not all(isinstance(item, str) for item in required):
        raise LeaseRejectedError("reconciliation_required")
    require_complete_stages(tuple(required), tuple(row["stage_id"] for row in siblings))
    if sum(row["lease_id"] == request.lease_id for row in siblings) != 1:
        raise LeaseRejectedError("reconciliation_required")
    others = []
    for row in siblings:
        record = _record(row)
        if row["lease_id"] == request.lease_id:
            if record != fact:
                raise LeaseRejectedError("reconciliation_required")
        else:
            if record.lease_state == "released":
                release_already_recorded(
                    record, ReleaseLease(record.lease_id, record.version), now_us
                )
            others.append(record)
    accounting = LeaseAccounting(
        host["memory_architecture"],
        _vector(host, "allocatable_"),
        _vector(host, "held_"),
        _vector(host, "quarantined_"),
        host["node_state"],
        host["quarantine_threshold_ppm"],
        instance["held_slots"],
        instance["quarantined_slots"],
        instance["max_slots"],
        quota["held_attempts"],
        quota["quarantined_attempts"],
        quota["max_attempts"],
        attempt["quota_state"],
        host["version"],
        instance["version"],
        attempt["version"],
        quota["version"],
    )
    decision = release(
        fact,
        accounting,
        request,
        execution=execution,
        other_leases_released=all(item.lease_state == "released" for item in others),
        now_us=now_us,
    )
    updated, released = decision.accounting, decision.lease
    if updated.quota_version != accounting.quota_version:
        _update(
            cursor,
            "UPDATE workspace_quotas SET held_attempts = %s, quarantined_attempts = %s, "
            "version = %s WHERE workspace_id = %s AND version = %s",
            (
                updated.held_attempts,
                updated.quarantined_attempts,
                updated.quota_version,
                quota["workspace_id"],
                accounting.quota_version,
            ),
        )
    assignments = ", ".join(
        prefix + name + " = %s" for prefix in ("held_", "quarantined_") for name in DIMENSIONS
    )
    _update(
        cursor,
        f"UPDATE host_nodes SET {assignments}, version = %s "
        "WHERE host_node_id = %s AND version = %s",
        (
            *updated.held.values,
            *updated.quarantined.values,
            updated.host_version,
            host["host_node_id"],
            accounting.host_version,
        ),
    )
    _update(
        cursor,
        "UPDATE engine_instances SET held_slots = %s, quarantined_slots = %s, "
        "version = %s WHERE instance_id = %s AND instance_generation = %s AND version = %s",
        (
            updated.held_slots,
            updated.quarantined_slots,
            updated.instance_version,
            *instance_key,
            accounting.instance_version,
        ),
    )
    if updated.attempt_version != accounting.attempt_version:
        _update(
            cursor,
            "UPDATE admission_attempts SET quota_state = %s, version = %s "
            "WHERE operation_id = %s AND attempt_id = %s AND version = %s",
            (updated.quota_state, updated.attempt_version, *identity, accounting.attempt_version),
        )
    _update(
        cursor,
        "UPDATE admission_leases SET lease_state = 'released', cleanup_state = 'complete', "
        "budget_released_at_us = %s, version = %s WHERE lease_id = %s AND version = %s",
        (released.budget_released_at_us, released.version, request.lease_id, fact.version),
    )
    return LeaseResult("committed", request.lease_id, released)
