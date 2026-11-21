"""Locked runtime observation statements; commit/network remain outside this unit."""

from __future__ import annotations

from collections.abc import Callable

from speechnexus_backend.speech.domain.owners import PoolOwnership
from speechnexus_backend.speech.domain.runtime_intents import (
    RuntimeHost,
    RuntimeInstance,
    RuntimeRecord,
    RuntimeRejectedError,
    RuntimeResult,
)
from speechnexus_backend.speech.domain.runtime_observations import (
    ObserveRuntimeIntent,
    RuntimeObservationDecision,
    RuntimeObservationPrerequisites,
    decide_runtime_observation,
    require_observation_progress,
)
from speechnexus_backend.speech.infrastructure.coordination_grants import DIMENSIONS
from speechnexus_backend.speech.infrastructure.runtime_observation_codec import (
    PreparedRuntimeObservation,
    decode_observation,
    readiness_to_document,
)
from speechnexus_backend.speech.infrastructure.runtime_sql import (
    RuntimeCursor,
    _changed,
    _head,
    _host,
    _instance,
    _one,
    _record,
    _related_records,
    _rows,
    _verify_event,
)


def _validate_progress(record: RuntimeRecord, prepared: PreparedRuntimeObservation) -> bool:
    if not prepared.observation.matches(record.request):
        raise RuntimeRejectedError("invalid_runtime_request")
    if record.observation_document is None:
        if record.intent_state in ("succeeded", "failed"):
            raise RuntimeRejectedError("reconciliation_required")
        return False
    previous = decode_observation(record.observation_document)
    require_observation_progress(previous.observation, prepared.observation)
    same = previous.semantic_document == prepared.semantic_document
    if previous.observation.intent_state in ("succeeded", "failed") and not same:
        raise RuntimeRejectedError("intent_conflict")
    return same


def _replay(cursor: RuntimeCursor, record: RuntimeRecord) -> RuntimeResult:
    instance = _one(
        cursor,
        "SELECT * FROM engine_instances WHERE instance_id = %s AND instance_generation = %s",
        (record.request.instance_id, record.request.instance_generation),
    )
    _instance(instance, record)
    _verify_event(cursor, record)
    return RuntimeResult("replayed", record.identity, record)


def _prerequisites(
    cursor: RuntimeCursor,
    record: RuntimeRecord,
    records: list[RuntimeRecord],
) -> RuntimeObservationPrerequisites:
    request = record.request
    leases = _rows(
        cursor,
        "SELECT lease_id, lease_state FROM admission_leases WHERE instance_id = %s "
        "AND instance_generation = %s ORDER BY lease_id FOR UPDATE",
        (request.instance_id, request.instance_generation),
    )
    if any(
        row["lease_state"] not in ("reserved", "executing", "stopping", "quarantined", "released")
        for row in leases
    ):
        raise RuntimeRejectedError("reconciliation_required")
    submits = _rows(
        cursor,
        "SELECT execution_state, cleanup_state FROM submit_intents WHERE lease_id IN "
        "(SELECT lease_id FROM admission_leases WHERE instance_id = %s "
        "AND instance_generation = %s) "
        "ORDER BY submit_intent_id FOR UPDATE",
        (request.instance_id, request.instance_generation),
    )
    return RuntimeObservationPrerequisites(
        all(row["lease_state"] == "released" for row in leases),
        all(
            row["execution_state"] in ("succeeded", "failed", "aborted")
            and row["cleanup_state"] in ("not_required", "complete")
            for row in submits
        ),
        all(
            row.identity == record.identity or row.intent_state in ("succeeded", "failed")
            for row in records
        ),
    )


def _same_decision(
    decision: RuntimeObservationDecision,
    host: RuntimeHost,
    instance: RuntimeInstance,
    record: RuntimeRecord,
) -> bool:
    return (
        decision.held == host.held
        and decision.quarantined == host.quarantined
        and decision.host_state == host.node_state
        and decision.instance_state == instance.instance_state
        and decision.base_budget_kind == instance.base_budget_kind
        and decision.intent_state == record.intent_state
        and decision.ready_at_us == instance.ready_at_us
        and decision.stopped_at_us == instance.stopped_at_us
        and decision.quarantine_reason == instance.quarantine_reason
    )


def observe_in_transaction(
    cursor: RuntimeCursor,
    command: ObserveRuntimeIntent,
    prepared: PreparedRuntimeObservation,
    *,
    clock: Callable[[], int],
) -> RuntimeResult:
    observation = command.observation
    if prepared.observation != observation:
        raise ValueError("Prepared runtime observation mismatch")
    identity = observation.identity
    located = _record(
        _one(
            cursor,
            "SELECT * FROM runtime_instance_intents WHERE target_node_id = %s AND intent_id = %s",
            (identity.target_node_id, identity.intent_id),
        )
    )
    same = _validate_progress(located, prepared)
    if same and (
        located.intent_state in ("succeeded", "failed")
        or command.expected_intent_version < located.version
    ):
        return _replay(cursor, located)
    request = located.request
    pool = _one(
        cursor,
        "SELECT * FROM resource_pools WHERE pool_id = %s FOR UPDATE",
        (request.binding.pool_id,),
    )
    host_row = _one(
        cursor,
        "SELECT * FROM host_nodes WHERE host_node_id = %s FOR UPDATE",
        (request.binding.target_node_id,),
    )
    head = _head(
        _one(
            cursor,
            "SELECT * FROM runtime_instance_heads WHERE instance_id = %s FOR UPDATE",
            (request.instance_id,),
        )
    )
    if head.current_generation != request.instance_generation:
        raise RuntimeRejectedError("stale_instance_generation")
    instance_row = _one(
        cursor,
        "SELECT * FROM engine_instances WHERE instance_id = %s "
        "AND instance_generation = %s FOR UPDATE",
        (request.instance_id, request.instance_generation),
    )
    records = _related_records(cursor, request, instance_row)
    selected = [row for row in records if row.identity == identity]
    preparations = [row for row in records if row.request.action == "prepare"]
    if (
        len(selected) != 1
        or len(preparations) != 1
        or preparations[0].identity.intent_id != head.prepare_intent_id
        or preparations[0].identity.target_node_id != request.binding.target_node_id
    ):
        raise RuntimeRejectedError("reconciliation_required")
    record = selected[0]
    if record.request != request or any(
        row.request.instance_id != request.instance_id
        or row.request.instance_generation != request.instance_generation
        or row.approved_plan != record.approved_plan
        for row in records
    ):
        raise RuntimeRejectedError("reconciliation_required")
    instance, host = _instance(instance_row, preparations[0]), _host(host_row)
    _verify_event(cursor, record)
    same = _validate_progress(record, prepared)
    if same and (
        record.intent_state in ("succeeded", "failed")
        or command.expected_intent_version < record.version
    ):
        return _replay(cursor, record)
    prerequisites = _prerequisites(cursor, record, records)
    now_us = clock()  # After every potentially blocking row lock.
    if now_us < record.updated_at_us:
        raise RuntimeRejectedError("invalid_counter")
    decision = decide_runtime_observation(
        command,
        record=record,
        head=head,
        instance=instance,
        host=host,
        ownership=PoolOwnership(
            pool["pool_id"],
            pool["owner_id"],
            pool["fencing_token"],
            pool["owner_expires_at_us"],
            pool["owner_state"],
            pool["version"],
        ),
        prerequisites=prerequisites,
        now_us=now_us,
    )
    if same and _same_decision(decision, host, instance, record):
        return RuntimeResult("replayed", identity, record)
    if (
        decision.held != host.held
        or decision.quarantined != host.quarantined
        or decision.host_state != host.node_state
    ):
        assignments = [
            prefix + name + " = %s" for prefix in ("held_", "quarantined_") for name in DIMENSIONS
        ]
        _changed(
            cursor,
            "UPDATE host_nodes SET "
            + ", ".join(assignments)
            + ", node_state = %s, version = version + 1 "
            "WHERE host_node_id = %s AND version = %s",
            decision.held.values
            + decision.quarantined.values
            + (decision.host_state, host.node_id, host.version),
        )
    _changed(
        cursor,
        "UPDATE engine_instances SET instance_state = %s, base_budget_kind = %s, ready_at_us = %s, "
        "stopped_at_us = %s, quarantine_reason = %s, version = version + 1 "
        "WHERE instance_id = %s AND instance_generation = %s AND version = %s",
        (
            decision.instance_state,
            decision.base_budget_kind,
            decision.ready_at_us,
            decision.stopped_at_us,
            decision.quarantine_reason,
            instance.instance_id,
            instance.instance_generation,
            instance.version,
        ),
    )
    readiness_document = record.readiness_document
    if readiness_document is None and decision.instance_state == "ready":
        if command.readiness is None:
            raise RuntimeRejectedError("reconciliation_required")
        readiness_document = readiness_to_document(command.readiness)
    original_document = record.observation_document if same else prepared.document
    _changed(
        cursor,
        "UPDATE runtime_instance_intents SET intent_state = %s, observation_document = %s, "
        "stop_evidence_document = %s, readiness_document = %s, error_code = %s, "
        "updated_at_us = %s, version = version + 1 WHERE target_node_id = %s "
        "AND intent_id = %s AND version = %s",
        (
            decision.intent_state,
            original_document,
            prepared.stop_document,
            readiness_document,
            observation.error_code,
            now_us,
            identity.target_node_id,
            identity.intent_id,
            record.version,
        ),
    )
    updated = RuntimeRecord(
        record.request,
        record.approved_plan,
        record.creator_id,
        record.request_digest,
        decision.intent_state,
        record.recorded_at_us,
        now_us,
        record.version + 1,
        original_document,
        readiness_document,
    )
    return RuntimeResult("committed", identity, updated)
