"""Explicit runtime statements; enclosing adapters own connections and commits."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Protocol

from speechnexus_backend.speech.domain.capacity import PhysicalResources
from speechnexus_backend.speech.domain.owners import PoolOwnership
from speechnexus_backend.speech.domain.runtime_intents import (
    CreateRuntimeIntent,
    RuntimeHead,
    RuntimeHost,
    RuntimeIdentity,
    RuntimeInstance,
    RuntimeIntent,
    RuntimePrerequisites,
    RuntimeRecord,
    RuntimeRejectedError,
    RuntimeResult,
    digest,
    integer,
    prepare_resources,
    require_runtime_authority,
    require_stop,
    unknown,
)
from speechnexus_backend.speech.infrastructure.coordination_grants import DIMENSIONS
from speechnexus_backend.speech.infrastructure.runtime_codec import (
    PreparedRuntimeRequest,
    decode_record,
    encode_record,
    prepare_request,
    validate_original_observation,
)
from speechnexus_backend.speech.infrastructure.runtime_observation_codec import (
    decode_observation,
    validate_stored_readiness,
)

type Row = dict[str, Any]


class RuntimeCursor(Protocol):
    @property
    def rowcount(self) -> int: ...

    def execute(self, query: str, parameters: tuple[object, ...] = ()) -> object: ...

    def fetchall(self) -> list[Row]: ...


def _rows(cursor: RuntimeCursor, query: str, parameters: tuple[object, ...] = ()) -> list[Row]:
    cursor.execute(query, parameters)
    return cursor.fetchall()


def _one(cursor: RuntimeCursor, query: str, parameters: tuple[object, ...]) -> Row:
    rows = _rows(cursor, query, parameters)
    if not rows:
        raise RuntimeRejectedError("not_found")
    if len(rows) != 1:
        raise RuntimeRejectedError("reconciliation_required")
    return rows[0]


def _vector(row: Row, prefix: str) -> PhysicalResources:
    return PhysicalResources(*(row[prefix + dimension] for dimension in DIMENSIONS))


def _host(row: Row) -> RuntimeHost:
    return RuntimeHost(
        row["host_node_id"],
        row["memory_architecture"],
        row["topology_digest"],
        row["node_state"],
        row["version"],
        _vector(row, "allocatable_"),
        _vector(row, "held_"),
        _vector(row, "quarantined_"),
        row["quarantine_threshold_ppm"],
    )


def _head(row: Row) -> RuntimeHead:
    return RuntimeHead(
        row["instance_id"],
        row["current_generation"],
        row["generation_high_water"],
        row["current_prepare_intent_id"],
        row["current_stop_intent_id"],
        row["version"],
    )


def _record(row: Row) -> RuntimeRecord:
    try:
        prepared, plan, creator = decode_record(row["request_document"])
        request = prepared.request
        if (
            row["target_node_id"],
            row["intent_id"],
            row["instance_id"],
            row["instance_generation"],
            row["action"],
            row["request_digest"],
        ) != (
            request.binding.target_node_id,
            request.intent_id,
            request.instance_id,
            request.instance_generation,
            request.action,
            prepared.request_digest,
        ):
            raise ValueError("Immutable runtime identity mismatch")
        observation = row["observation_document"]
        if observation is not None:
            value = validate_original_observation(observation, request)
            if (
                row["intent_state"] in ("succeeded", "failed")
                and row["intent_state"] != value["intent_state"]
            ):
                raise ValueError("Runtime terminal state does not match its actual observation")
        if row["stop_evidence_document"] is not None and (
            observation is None
            or decode_observation(observation).stop_document != row["stop_evidence_document"]
        ):
            raise ValueError("Standalone stop evidence does not match its actual observation")
        readiness_document = row["readiness_document"]
        if readiness_document is not None:
            validate_stored_readiness(readiness_document, request, plan)
            if observation is None or row["intent_state"] != "succeeded":
                raise ValueError("Readiness provenance requires a successful prepare observation")
            observed = decode_observation(observation).observation
            if observed.action != "prepare" or observed.intent_state != "succeeded":
                raise ValueError("Readiness does not match an actual ready observation")
        return RuntimeRecord(
            request,
            plan,
            creator,
            prepared.request_digest,
            row["intent_state"],
            row["recorded_at_us"],
            row["updated_at_us"],
            row["version"],
            observation,
            row["readiness_document"],
        )
    except ValueError, TypeError, KeyError:
        raise RuntimeRejectedError("reconciliation_required") from None


def _instance(row: Row, record: RuntimeRecord) -> RuntimeInstance:
    plan = record.approved_plan
    binding = plan.binding
    if (
        row["host_node_id"],
        row["pool_id"],
        row["gateway_id"],
        row["approved_template_id"],
        row["binding_revision_id"],
        row["manifest_id"],
        row["config_digest"],
        row["management_mode"],
        row["loading_policy"],
        row["max_slots"],
        row["realtime_reserved_slots"],
    ) != (
        binding.target_node_id,
        binding.pool_id,
        binding.gateway_id,
        binding.approved_template_id,
        binding.binding_revision_id,
        binding.manifest_id,
        binding.config_digest,
        "managed",
        plan.loading_policy,
        plan.max_slots,
        plan.realtime_reserved_slots,
    ):
        raise RuntimeRejectedError("reconciliation_required")
    if (
        _vector(row, "peak_") != plan.peak
        or _vector(row, "steady_") != plan.steady
        or _vector(row, "per_slot_") != plan.per_slot
    ):
        raise RuntimeRejectedError("reconciliation_required")
    if record.readiness_document is not None:
        receipt = validate_stored_readiness(record.readiness_document, record.request, plan)
        if (
            row["ready_at_us"] is None
            or not receipt.verified_at_us <= row["ready_at_us"] < receipt.expires_at_us
        ):
            raise RuntimeRejectedError("reconciliation_required")
    return RuntimeInstance(
        row["instance_id"],
        row["instance_generation"],
        plan,
        row["instance_state"],
        row["base_budget_kind"],
        row["held_slots"],
        row["quarantined_slots"],
        row["last_fencing_token"],
        row["version"],
        row["stopped_at_us"],
        row["ready_at_us"],
        row["quarantine_reason"],
    )


def _verify_event(cursor: RuntimeCursor, record: RuntimeRecord) -> None:
    prepared = prepare_request(record.request)
    rows = _rows(
        cursor,
        "SELECT event_kind, job_id, target_node_id, intent_id, event_digest "
        "FROM coordination_outbox WHERE event_id = %s",
        (prepared.event_id,),
    )
    if rows != [
        {
            "event_kind": "runtime",
            "job_id": None,
            "target_node_id": record.identity.target_node_id,
            "intent_id": record.identity.intent_id,
            "event_digest": prepared.request_digest,
        }
    ]:
        raise RuntimeRejectedError("reconciliation_required")


def _existing(
    cursor: RuntimeCursor, identity: RuntimeIdentity, expected_digest: str | None
) -> RuntimeResult | None:
    rows = _rows(
        cursor,
        "SELECT * FROM runtime_instance_intents WHERE target_node_id = %s AND intent_id = %s",
        (identity.target_node_id, identity.intent_id),
    )
    if not rows:
        return None
    if len(rows) != 1:
        raise RuntimeRejectedError("reconciliation_required")
    record = _record(rows[0])
    if record.identity != identity:
        raise RuntimeRejectedError("reconciliation_required")
    if expected_digest is not None and expected_digest != record.request_digest:
        raise RuntimeRejectedError("intent_conflict")
    engine = _one(
        cursor,
        "SELECT * FROM engine_instances WHERE instance_id = %s AND instance_generation = %s",
        (record.request.instance_id, record.request.instance_generation),
    )
    _instance(engine, record)
    _verify_event(cursor, record)
    return RuntimeResult("replayed", identity, record)


def query_in_transaction(
    cursor: RuntimeCursor,
    identity: RuntimeIdentity,
    *,
    request_digest: str | None = None,
    conflict_if_absent: bool = False,
) -> RuntimeResult:
    if request_digest is not None:
        digest(request_digest)
    result = _existing(cursor, identity, request_digest)
    if result is not None:
        return result
    if conflict_if_absent:
        # Only the adapter's unique-conflict path can use confirmed rollback.
        raise RuntimeRejectedError("version_conflict")
    return unknown(identity)


def _related_records(
    cursor: RuntimeCursor, request: RuntimeIntent, current: Row | None
) -> list[RuntimeRecord]:
    selector = "target_node_id = %s AND intent_id = %s"
    parameters: tuple[object, ...] = (request.binding.target_node_id, request.intent_id)
    if current is not None:
        selector = "(" + selector + ") OR (instance_id = %s AND instance_generation = %s)"
        parameters += (current["instance_id"], current["instance_generation"])
    rows = _rows(
        cursor,
        "SELECT * FROM runtime_instance_intents WHERE "
        + selector
        + " ORDER BY target_node_id, intent_id FOR UPDATE",
        parameters,
    )
    return [_record(row) for row in rows]


def _prerequisites(
    cursor: RuntimeCursor,
    command: CreateRuntimeIntent,
    head: RuntimeHead,
    instance: RuntimeInstance,
    records: list[RuntimeRecord],
) -> RuntimePrerequisites:
    leases = _rows(
        cursor,
        "SELECT lease_id, lease_state, fencing_token, reserved_slots FROM admission_leases "
        "WHERE instance_id = %s AND instance_generation = %s ORDER BY lease_id FOR UPDATE",
        (instance.instance_id, instance.instance_generation),
    )
    held = quarantined = 0
    for lease in leases:
        slots = integer(lease["reserved_slots"], minimum=1)
        integer(lease["fencing_token"], minimum=1)
        if lease["lease_state"] == "quarantined":
            quarantined += slots
        elif lease["lease_state"] in ("reserved", "executing", "stopping"):
            held += slots
        elif lease["lease_state"] != "released":
            raise RuntimeRejectedError("reconciliation_required")
    if held != instance.held_slots or quarantined != instance.quarantined_slots:
        raise RuntimeRejectedError("reconciliation_required")
    outstanding = [row for row in leases if row["lease_state"] != "released"]
    barrier = _rows(
        cursor,
        "SELECT min_fencing_token, sealed_evidence_digest FROM gateway_fencing_barriers "
        "WHERE gateway_id = %s AND pool_id = %s FOR UPDATE",
        (instance.plan.binding.gateway_id, instance.plan.binding.pool_id),
    )
    sealed = False
    if barrier:
        if len(barrier) != 1:
            raise RuntimeRejectedError("reconciliation_required")
        integer(barrier[0]["min_fencing_token"], minimum=1)
        digest(barrier[0]["sealed_evidence_digest"])
        sealed = barrier[0]["min_fencing_token"] >= command.request.fencing_token
    submits = _rows(
        cursor,
        "SELECT execution_state FROM submit_intents WHERE lease_id IN "
        "(SELECT lease_id FROM admission_leases WHERE instance_id = %s AND "
        "instance_generation = %s) "
        "ORDER BY submit_intent_id FOR UPDATE",
        (instance.instance_id, instance.instance_generation),
    )
    terminal = all(
        row["execution_state"] in ("succeeded", "failed", "aborted", "unknown_quarantined")
        for row in submits
    )
    stopped = False
    for record in records:
        if (
            record.request.intent_id == head.stop_intent_id
            and record.request.binding.target_node_id == instance.plan.binding.target_node_id
            and record.request.action == "stop"
            and record.intent_state == "succeeded"
            and record.observation_document is not None
        ):
            value = validate_original_observation(record.observation_document, record.request)
            evidence = value.get("stop_evidence")
            stopped = bool(
                value["observed_instance_state"] in ("stopped", "absent")
                and record.request.fencing_token == instance.last_fencing_token
                and isinstance(evidence, dict)
                and evidence.get("process_or_container_exited") is True
                and evidence.get("resources_released_confirmed") is True
            )
    return RuntimePrerequisites(
        not outstanding,
        terminal,
        all(row.intent_state in ("succeeded", "failed") for row in records),
        stopped,
        bool(outstanding)
        and sealed
        and all(
            row["lease_state"] == "quarantined"
            and row["fencing_token"] < command.request.fencing_token
            for row in outstanding
        ),
    )


def _changed(cursor: RuntimeCursor, query: str, parameters: tuple[object, ...]) -> None:
    cursor.execute(query, parameters)
    if cursor.rowcount != 1:
        raise RuntimeRejectedError("reconciliation_required")


def create_in_transaction(
    cursor: RuntimeCursor,
    command: CreateRuntimeIntent,
    prepared: PreparedRuntimeRequest,
    *,
    clock: Callable[[], int],
) -> RuntimeResult:
    request = command.request
    if prepared.request != request:
        raise ValueError("Prepared runtime command mismatch")
    replay = _existing(cursor, request.identity, prepared.request_digest)
    if replay is not None:
        return replay  # Replay never allocates or reinterprets current defaults.
    # Locate only; immutable references are rechecked after parent/head locks.
    located = _rows(
        cursor,
        "SELECT e.pool_id, e.host_node_id FROM runtime_instance_heads h JOIN engine_instances e "
        "ON e.instance_id = h.instance_id AND e.instance_generation = h.current_generation "
        "WHERE h.instance_id = %s",
        (request.instance_id,),
    )
    if len(located) > 1:
        raise RuntimeRejectedError("reconciliation_required")
    pool_ids = {request.binding.pool_id, *(row["pool_id"] for row in located)}
    node_ids = {request.binding.target_node_id, *(row["host_node_id"] for row in located)}
    pools = {
        key: _one(cursor, "SELECT * FROM resource_pools WHERE pool_id = %s FOR UPDATE", (key,))
        for key in sorted(pool_ids)
    }
    hosts = {
        key: _one(cursor, "SELECT * FROM host_nodes WHERE host_node_id = %s FOR UPDATE", (key,))
        for key in sorted(node_ids)
    }
    replay = _existing(cursor, request.identity, prepared.request_digest)
    if replay is not None:
        return replay
    heads = _rows(
        cursor,
        "SELECT * FROM runtime_instance_heads WHERE instance_id = %s FOR UPDATE",
        (request.instance_id,),
    )
    if len(heads) > 1:
        raise RuntimeRejectedError("reconciliation_required")
    head = _head(heads[0]) if heads else None
    current: Row | None = None
    if head is not None:
        located_current = _one(
            cursor,
            "SELECT host_node_id, pool_id FROM engine_instances WHERE instance_id = %s "
            "AND instance_generation = %s",
            (head.instance_id, head.current_generation),
        )
        if located_current["host_node_id"] not in hosts or located_current["pool_id"] not in pools:
            raise RuntimeRejectedError("version_conflict")
        current = _one(
            cursor,
            "SELECT * FROM engine_instances WHERE instance_id = %s AND "
            "instance_generation = %s FOR UPDATE",
            (head.instance_id, head.current_generation),
        )
    elif _rows(
        cursor,
        "SELECT instance_generation FROM engine_instances WHERE instance_id = %s",
        (request.instance_id,),
    ):
        raise RuntimeRejectedError("reconciliation_required")
    records = _related_records(cursor, request, current)
    previous: RuntimeInstance | None = None
    facts = RuntimePrerequisites(True, True, True, False, False)
    if current is not None and head is not None:
        preparations = [record for record in records if record.request.action == "prepare"]
        if (
            len(preparations) != 1
            or preparations[0].identity.target_node_id != current["host_node_id"]
            or preparations[0].identity.intent_id != head.prepare_intent_id
        ):
            raise RuntimeRejectedError("reconciliation_required")
        previous = _instance(current, preparations[0])
        for record in records:
            if (
                record.request.instance_id != previous.instance_id
                or record.request.instance_generation != previous.instance_generation
                or record.approved_plan != previous.plan
            ):
                raise RuntimeRejectedError("reconciliation_required")
        facts = _prerequisites(cursor, command, head, previous, records)
    pool = pools[request.binding.pool_id]
    owner = PoolOwnership(
        pool["pool_id"],
        pool["owner_id"],
        pool["fencing_token"],
        pool["owner_expires_at_us"],
        pool["owner_state"],
        pool["version"],
    )
    host = _host(hosts[request.binding.target_node_id])
    now_us = clock()  # Explicit row locks are held; INSERTs can still wait on unique keys.
    if request.action == "prepare":
        updated = prepare_resources(
            command,
            ownership=owner,
            pool_state=pool["pool_state"],
            service_class=pool["service_class"],
            host=host,
            head=head,
            previous=previous,
            prerequisites=facts,
            now_us=now_us,
        )
        plan = command.approved_plan
        if plan is None:
            raise RuntimeRejectedError("unapproved_runtime_template")
        assignments = ", ".join(f"held_{name} = %s" for name in DIMENSIONS)
        _changed(
            cursor,
            f"UPDATE host_nodes SET {assignments}, version = version + 1 WHERE "
            f"host_node_id = %s AND version = %s",
            (*updated.values, host.node_id, host.version),
        )
        columns = [
            "instance_id",
            "instance_generation",
            "host_node_id",
            "pool_id",
            "gateway_id",
            "binding_revision_id",
            "manifest_id",
            "approved_template_id",
            "management_mode",
            "loading_policy",
            "config_digest",
            "instance_state",
            "max_slots",
            "realtime_reserved_slots",
            "last_fencing_token",
        ]
        values: tuple[object, ...] = (
            request.instance_id,
            request.instance_generation,
            plan.binding.target_node_id,
            plan.binding.pool_id,
            plan.binding.gateway_id,
            plan.binding.binding_revision_id,
            plan.binding.manifest_id,
            plan.binding.approved_template_id,
            "managed",
            plan.loading_policy,
            plan.binding.config_digest,
            "preparing",
            plan.max_slots,
            plan.realtime_reserved_slots,
            request.fencing_token,
        )
        for prefix, vector in (
            ("peak_", plan.peak),
            ("steady_", plan.steady),
            ("per_slot_", plan.per_slot),
        ):
            columns.extend(prefix + name for name in DIMENSIONS)
            values += vector.values
        cursor.execute(
            "INSERT INTO engine_instances ("
            + ", ".join(columns)
            + ") VALUES ("
            + ", ".join(["%s"] * len(values))
            + ")",
            values,
        )
        if head is None:
            cursor.execute(
                "INSERT INTO runtime_instance_heads (instance_id, current_generation, "
                "generation_high_water, current_prepare_intent_id) VALUES (%s, %s, %s, %s)",
                (
                    request.instance_id,
                    request.instance_generation,
                    request.instance_generation,
                    request.intent_id,
                ),
            )
        else:
            _changed(
                cursor,
                "UPDATE runtime_instance_heads SET current_generation = %s, "
                "generation_high_water = %s, current_prepare_intent_id = %s, "
                "current_stop_intent_id = NULL, version = version + 1 WHERE instance_id = "
                "%s AND version = %s",
                (
                    request.instance_generation,
                    request.instance_generation,
                    request.intent_id,
                    request.instance_id,
                    head.version,
                ),
            )
    else:
        require_stop(
            command,
            ownership=owner,
            head=head,
            instance=previous,
            prerequisites=facts,
            now_us=now_us,
        )
        if previous is None or head is None:
            raise RuntimeRejectedError("reconciliation_required")
        plan = previous.plan
        # Cleanup uses the original immutable reservation even if a later
        # topology declaration changed. No capacity is allocated or released.
        _changed(
            cursor,
            "UPDATE engine_instances SET instance_state = 'draining', "
            "last_fencing_token = %s, version = version + 1 WHERE instance_id = %s AND "
            "instance_generation = %s AND version = %s",
            (
                request.fencing_token,
                previous.instance_id,
                previous.instance_generation,
                previous.version,
            ),
        )
        _changed(
            cursor,
            "UPDATE runtime_instance_heads SET current_stop_intent_id = %s, version = "
            "version + 1 WHERE instance_id = %s AND version = %s",
            (request.intent_id, head.instance_id, head.version),
        )
    # A conflicting global instance/head insertion can wait on a transaction
    # with different resource parents, then succeed after that rival rolls back.
    now_us = clock()
    require_runtime_authority(owner, request, now_us, owner_id=command.owner_id)
    # No network, object write or separate transaction in this unit.
    cursor.execute(
        "INSERT INTO runtime_instance_intents (target_node_id, intent_id, "
        "instance_id, instance_generation, action, request_digest, "
        "request_document, intent_state, recorded_at_us, updated_at_us) VALUES "
        "(%s, %s, %s, %s, %s, %s, %s, 'pending', %s, %s)",
        (
            request.binding.target_node_id,
            request.intent_id,
            request.instance_id,
            request.instance_generation,
            request.action,
            prepared.request_digest,
            encode_record(prepared, plan, command.owner_id),
            now_us,
            now_us,
        ),
    )
    cursor.execute(
        "INSERT INTO coordination_outbox (event_id, event_digest, event_kind, "
        "target_node_id, intent_id) VALUES (%s, %s, 'runtime', %s, %s)",
        (
            prepared.event_id,
            prepared.request_digest,
            request.binding.target_node_id,
            request.intent_id,
        ),
    )
    # Only this same-transaction creation proves a fresh unsent command. Old
    # pending outbox rows are adopted as unknown by migration, never recreated.
    cursor.execute(
        "INSERT INTO runtime_intent_deliveries "
        "(event_id, delivery_phase, created_at_us, updated_at_us) "
        "VALUES (%s, 'unsent', %s, %s)",
        (prepared.event_id, now_us, now_us),
    )
    # The global outbox identity can introduce one last uniqueness wait. A
    # successful INSERT after a rival rollback must not commit expired authority.
    require_runtime_authority(owner, request, clock(), owner_id=command.owner_id)
    record = RuntimeRecord(
        request, plan, command.owner_id, prepared.request_digest, "pending", now_us, now_us, 1
    )
    return RuntimeResult("committed", request.identity, record)
