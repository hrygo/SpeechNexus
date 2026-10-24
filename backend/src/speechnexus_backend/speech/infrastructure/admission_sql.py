"""One complete admission on the caller's cursor; never own commit or perform I/O."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from speechnexus_backend.speech.domain.admission import (
    AdmissionFact,
    AdmissionPool,
    AdmissionQuota,
    AdmissionRejectedError,
    AdmissionResult,
    AdmissionRuntime,
    decide_admission,
    require_live_authority,
)
from speechnexus_backend.speech.domain.owners import PoolOwnership
from speechnexus_backend.speech.domain.runtime_intents import RuntimeRejectedError
from speechnexus_backend.speech.infrastructure.admission_codec import PreparedAdmission
from speechnexus_backend.speech.infrastructure.admission_recovery_sql import (
    AdmissionCursor,
    changed,
    one,
    read_original,
    rows,
)
from speechnexus_backend.speech.infrastructure.coordination_grants import DIMENSIONS
from speechnexus_backend.speech.infrastructure.jobs_sql import create_in_transaction
from speechnexus_backend.speech.infrastructure.runtime_observation_codec import (
    decode_observation,
    validate_stored_readiness,
)
from speechnexus_backend.speech.infrastructure.runtime_sql import (
    _head,
    _host,
    _instance,
    _record,
    _verify_event,
)

type Row = dict[str, Any]


def _pool(row: Row) -> AdmissionPool:
    return AdmissionPool(
        PoolOwnership(
            row["pool_id"],
            row["owner_id"],
            row["fencing_token"],
            row["owner_expires_at_us"],
            row["owner_state"],
            row["version"],
        ),
        row["pool_state"],
        row["service_class"],
    )


def _quota(row: Row) -> AdmissionQuota:
    return AdmissionQuota(
        row["workspace_id"],
        row["max_attempts"],
        row["held_attempts"],
        row["quarantined_attempts"],
        row["policy_revision"],
        row["version"],
    )


def _runtime(cursor: AdmissionCursor, instance: Row, head: Row, intent: Row) -> AdmissionRuntime:
    try:
        record = _record(intent)
        runtime_instance = _instance(instance, record)
        if record.observation_document is None or record.readiness_document is None:
            raise AdmissionRejectedError("reconciliation_required")
        observation = decode_observation(record.observation_document).observation
        readiness = validate_stored_readiness(
            record.readiness_document, record.request, record.approved_plan
        )
        _verify_event(cursor, record)
        batch = one(
            cursor,
            "SELECT COALESCE(SUM(l.reserved_slots), 0) AS slots FROM admission_leases l "
            "JOIN admission_attempts a ON a.operation_id = l.operation_id "
            "AND a.attempt_id = l.attempt_id WHERE l.instance_id = %s "
            "AND l.instance_generation = %s AND l.lease_state <> 'released' "
            "AND a.service_class <> 'realtime'",
            (instance["instance_id"], instance["instance_generation"]),
        )["slots"]
        return AdmissionRuntime(runtime_instance, _head(head), observation, readiness, int(batch))
    except RuntimeRejectedError as error:
        raise AdmissionRejectedError(error.code) from None
    except ValueError, TypeError, KeyError:
        raise AdmissionRejectedError("reconciliation_required") from None


def admit_in_transaction(
    cursor: AdmissionCursor,
    prepared: PreparedAdmission,
    *,
    clock: Callable[[], int],
    max_command_bytes: int,
) -> AdmissionResult:
    command = prepared.command
    request, identity = command.request, command.request.identity
    key = identity.operation_id, identity.attempt_id
    existing = read_original(
        cursor, identity, max_command_bytes=max_command_bytes, prepared=prepared
    )
    if existing is not None:
        return existing
    # Immutable parent hints must match before any child lock is acquired.
    # A request cannot lock instance B while claiming its parent is host A.
    instance_hints = {
        key: one(
            cursor,
            "SELECT instance_id, instance_generation, host_node_id, pool_id, gateway_id "
            "FROM engine_instances WHERE instance_id = %s AND instance_generation = %s",
            key,
        )
        for key in sorted({s.instance_key for s in request.stages})
    }
    for stage in request.stages:
        hint = instance_hints[stage.instance_key]
        if (hint["host_node_id"], hint["pool_id"], hint["gateway_id"]) != (
            stage.host_node_id,
            stage.pool_id,
            stage.gateway_id,
        ):
            raise AdmissionRejectedError("recipe_mismatch")
    pool_rows = {
        key: one(cursor, "SELECT * FROM resource_pools WHERE pool_id = %s FOR UPDATE", (key,))
        for key in sorted(a.pool_id for a in request.authorities)
    }
    quota_row = one(
        cursor,
        "SELECT * FROM workspace_quotas WHERE workspace_id = %s FOR UPDATE",
        (request.workspace_id,),
    )
    host_rows = {
        node: one(cursor, "SELECT * FROM host_nodes WHERE host_node_id = %s FOR UPDATE", (node,))
        for node in sorted({s.host_node_id for s in request.stages})
    }
    # A prior identical transaction may have committed while its parent locks
    # were awaited. Replay its immutable fact before consulting today's runtime.
    existing = read_original(
        cursor, identity, max_command_bytes=max_command_bytes, prepared=prepared
    )
    if existing is not None:
        return existing
    head_rows = {
        instance: one(
            cursor,
            "SELECT * FROM runtime_instance_heads WHERE instance_id = %s FOR UPDATE",
            (instance,),
        )
        for instance in sorted({s.instance_id for s in request.stages})
    }
    if any(
        head_rows[s.instance_id]["current_generation"] != s.instance_generation
        for s in request.stages
    ):
        raise AdmissionRejectedError("recipe_mismatch")
    instance_rows = {
        key: one(
            cursor,
            "SELECT * FROM engine_instances WHERE instance_id = %s "
            "AND instance_generation = %s FOR UPDATE",
            key,
        )
        for key in sorted({s.instance_key for s in request.stages})
    }
    for instance_key, row in instance_rows.items():
        hint = instance_hints[instance_key]
        if any(row[name] != hint[name] for name in ("host_node_id", "pool_id", "gateway_id")):
            raise AdmissionRejectedError("reconciliation_required")
    intent_keys = {
        (row["host_node_id"], intent_id)
        for row in instance_rows.values()
        for intent_id in (
            head_rows[row["instance_id"]]["current_prepare_intent_id"],
            head_rows[row["instance_id"]]["current_stop_intent_id"],
        )
        if intent_id is not None
    }
    intent_rows = {
        key: one(
            cursor,
            "SELECT * FROM runtime_instance_intents WHERE target_node_id = %s "
            "AND intent_id = %s FOR UPDATE",
            key,
        )
        for key in sorted(intent_keys)
    }
    # This anchor is after all resource parents, before attempt/request/leases.
    # No reverse FK to quota/attempt causes an implicit earlier-parent wait.
    operations = rows(
        cursor,
        "SELECT * FROM admission_operations WHERE operation_id = %s FOR UPDATE",
        (identity.operation_id,),
    )
    if operations:
        operation = operations[0]
        if (
            operation["workspace_id"] != request.workspace_id
            or operation["request_digest"] != command.business_command.request_digest
        ):
            raise AdmissionRejectedError("idempotency_conflict")
        existing = read_original(
            cursor, identity, max_command_bytes=max_command_bytes, prepared=prepared
        )
        if existing is not None:
            return existing
        # Safe new-attempt authorization/public outcome is not assembled yet.
        # A visible incomplete anchor or any other attempt must be reconciled.
        raise AdmissionRejectedError("reconciliation_required")
    if rows(
        cursor,
        "SELECT attempt_id FROM admission_attempts WHERE operation_id = %s LIMIT 1",
        (identity.operation_id,),
    ) or rows(
        cursor,
        "SELECT job_id FROM coordination_jobs WHERE operation_id = %s LIMIT 1",
        (identity.operation_id,),
    ):
        raise AdmissionRejectedError("reconciliation_required")
    pools = tuple(_pool(row) for row in pool_rows.values())
    hosts = tuple(_host(row) for row in host_rows.values())
    runtimes_list: list[AdmissionRuntime] = []
    for row in instance_rows.values():
        head = head_rows[row["instance_id"]]
        prepare_id = head["current_prepare_intent_id"]
        if prepare_id is None:
            raise AdmissionRejectedError("reconciliation_required")
        runtimes_list.append(
            _runtime(cursor, row, head, intent_rows[(row["host_node_id"], prepare_id)])
        )
    runtimes = tuple(runtimes_list)
    quota = _quota(quota_row)
    recorded_at_us = clock()  # All explicit parent/operation lock waits are over.
    require_live_authority(command, pools, runtimes, recorded_at_us)
    changed(
        cursor,
        "INSERT INTO admission_operations (operation_id, workspace_id, request_digest, "
        "request_document, initial_attempt_id, current_attempt_id, recorded_at_us) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s)",
        (
            identity.operation_id,
            request.workspace_id,
            command.business_command.request_digest,
            command.business_command.document,
            identity.attempt_id,
            identity.attempt_id,
            recorded_at_us,
        ),
    )
    # An absent operation row can still wait on a concurrent unique insertion.
    decision = decide_admission(
        command, quota=quota, pools=pools, hosts=hosts, runtimes=runtimes, now_us=clock()
    )
    changed(
        cursor,
        "INSERT INTO admission_attempts (operation_id, attempt_id, workspace_id, "
        "binding_revision_id, manifest_id, recipe_digest, policy_revision_id, service_class, "
        "request_digest, required_stage_ids) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (
            *key,
            request.workspace_id,
            request.binding_revision_id,
            request.manifest_id,
            request.recipe_digest,
            request.policy_revision_id,
            request.service_class,
            prepared.request_digest,
            json.dumps(sorted(s.stage_id for s in request.stages), separators=(",", ":")),
        ),
    )
    changed(
        cursor,
        "INSERT INTO admission_requests (operation_id, attempt_id, request_digest, "
        "request_document, recorded_at_us) VALUES (%s, %s, %s, %s, %s)",
        (*key, prepared.request_digest, prepared.document, recorded_at_us),
    )
    authorities = {a.pool_id: a for a in request.authorities}
    columns = ", ".join("reserved_" + dimension for dimension in DIMENSIONS)
    placeholders = ", ".join("%s" for _ in DIMENSIONS)
    for reservation in sorted(decision.reservations, key=lambda r: r.stage.lease_id):
        stage = reservation.stage
        changed(
            cursor,
            "INSERT INTO admission_leases (lease_id, operation_id, attempt_id, stage_id, "
            "host_node_id, instance_id, instance_generation, pool_id, gateway_id, fencing_token, "
            f"reserved_slots, deadline_at_us, {columns}) VALUES "
            f"(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, {placeholders})",
            (
                stage.lease_id,
                *key,
                stage.stage_id,
                stage.host_node_id,
                stage.instance_id,
                stage.instance_generation,
                stage.pool_id,
                stage.gateway_id,
                authorities[stage.pool_id].fencing_token,
                stage.reserved_slots,
                request.deadline_at_us,
                *reservation.physical.values,
            ),
        )
    changed(
        cursor,
        "UPDATE workspace_quotas SET held_attempts = %s, version = version + 1 "
        "WHERE workspace_id = %s AND version = %s",
        (decision.quota_held_attempts, request.workspace_id, quota.version),
    )
    host_by_id = {host.node_id: host for host in hosts}
    for node, held in decision.host_held:
        assignments = ", ".join("held_" + dimension + " = %s" for dimension in DIMENSIONS)
        changed(
            cursor,
            f"UPDATE host_nodes SET {assignments}, version = version + 1 "
            "WHERE host_node_id = %s AND version = %s",
            (*held.values, node, host_by_id[node].version),
        )
    by_instance = {
        (r.instance.instance_id, r.instance.instance_generation): r.instance for r in runtimes
    }
    for instance_key, held_slots in decision.instance_held_slots:
        changed(
            cursor,
            "UPDATE engine_instances SET held_slots = %s, version = version + 1 "
            "WHERE instance_id = %s AND instance_generation = %s AND version = %s",
            (held_slots, *instance_key, by_instance[instance_key].version),
        )
    job = create_in_transaction(cursor, request.job)  # Same transaction, never a committing port.
    if job.outcome == "replayed":
        raise AdmissionRejectedError("reconciliation_required")
    if job.outcome != "committed":
        raise AdmissionRejectedError(job.error_code or "reconciliation_required")
    # Job/outbox, lease and FK uniqueness waits can outlast every earlier clock.
    # Retained locks keep facts fixed; time windows must still be live now.
    require_live_authority(command, pools, runtimes, clock())
    return AdmissionResult("committed", identity, AdmissionFact(identity, decision.reservations, 1))
