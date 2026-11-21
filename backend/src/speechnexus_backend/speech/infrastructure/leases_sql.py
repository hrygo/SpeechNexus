"""Atomic held-to-quarantine projections; callers own transaction/commit scope."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Protocol

from speechnexus_backend.speech.domain.capacity import PhysicalResources
from speechnexus_backend.speech.domain.leases import (
    LeaseAccounting,
    LeaseRecord,
    LeaseRejectedError,
    LeaseResult,
    QuarantineLease,
    quarantine,
    unknown,
)
from speechnexus_backend.speech.domain.runtime_intents import identifier
from speechnexus_backend.speech.infrastructure.coordination_grants import DIMENSIONS

type Row = dict[str, Any]


class LeaseCursor(Protocol):
    @property
    def rowcount(self) -> int: ...

    def execute(self, query: str, parameters: tuple[object, ...] = ()) -> object: ...

    def fetchall(self) -> list[Row]: ...


def _rows(cursor: LeaseCursor, query: str, parameters: tuple[object, ...]) -> list[Row]:
    cursor.execute(query, parameters)
    return cursor.fetchall()


def _one(cursor: LeaseCursor, query: str, parameters: tuple[object, ...]) -> Row:
    rows = _rows(cursor, query, parameters)
    if len(rows) != 1:
        raise LeaseRejectedError("not_found" if not rows else "reconciliation_required")
    return rows[0]


def _vector(row: Row, prefix: str) -> PhysicalResources:
    return PhysicalResources(*(row[prefix + dimension] for dimension in DIMENSIONS))


def _record(row: Row) -> LeaseRecord:
    return LeaseRecord(
        row["lease_id"],
        row["lease_state"],
        row["cleanup_state"],
        row["version"],
        _vector(row, "reserved_"),
        row["reserved_slots"],
        row["quarantine_observed_at_us"],
        row["quarantine_reason_code"],
        row["budget_released_at_us"],
    )


def query_in_transaction(cursor: LeaseCursor, lease_id: str) -> LeaseResult:
    identifier(lease_id)
    rows = _rows(cursor, "SELECT * FROM admission_leases WHERE lease_id = %s", (lease_id,))
    if not rows:
        return unknown(lease_id)
    if len(rows) != 1:
        raise LeaseRejectedError("reconciliation_required")
    return LeaseResult("replayed", lease_id, _record(rows[0]))


def _update(cursor: LeaseCursor, query: str, parameters: tuple[object, ...]) -> None:
    cursor.execute(query, parameters)
    if cursor.rowcount != 1:
        raise LeaseRejectedError("reconciliation_required")


def quarantine_in_transaction(
    cursor: LeaseCursor, request: QuarantineLease, *, clock: Callable[[], int]
) -> LeaseResult:
    # Unlocked reads locate parent keys only. Every reference is rechecked after
    # the ordered locks; neither a TTL nor a caller's timestamp grants authority.
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
        cursor,
        "SELECT * FROM admission_leases WHERE lease_id = %s FOR UPDATE",
        (request.lease_id,),
    )
    submits = _rows(
        cursor,
        "SELECT submit_intent_id, lease_id, operation_id, attempt_id, execution_state, "
        "cleanup_state, version FROM submit_intents WHERE lease_id = %s "
        "ORDER BY submit_intent_id FOR UPDATE",
        (request.lease_id,),
    )
    now_us = clock()  # DB clock after all explicit and parent-row waits.
    frozen_keys = (
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
        *("reserved_" + dimension for dimension in DIMENSIONS),
    )
    if (
        any(lease[key] != hint[key] for key in frozen_keys)
        or attempt["workspace_id"] != attempt_hint["workspace_id"]
        or any(instance[key] != lease[key] for key in ("host_node_id", "pool_id", "gateway_id"))
        or len(submits) > 1
        or any(
            row[key] != lease[key]
            for row in submits
            for key in ("lease_id", "operation_id", "attempt_id")
        )
    ):
        raise LeaseRejectedError("reconciliation_required")
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
    decision = quarantine(
        _record(lease),
        accounting,
        request,
        execution=(submits[0]["execution_state"], submits[0]["cleanup_state"]) if submits else None,
        now_us=now_us,
    )
    if decision.replayed:
        return LeaseResult("replayed", request.lease_id, decision.lease)
    updated, fact = decision.accounting, decision.lease
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
        prefix + dimension + " = %s"
        for prefix in ("held_", "quarantined_")
        for dimension in DIMENSIONS
    )
    _update(
        cursor,
        f"UPDATE host_nodes SET {assignments}, node_state = %s, version = %s "
        "WHERE host_node_id = %s AND version = %s",
        (
            *updated.held.values,
            *updated.quarantined.values,
            updated.node_state,
            updated.host_version,
            host["host_node_id"],
            accounting.host_version,
        ),
    )
    _update(
        cursor,
        "UPDATE engine_instances SET held_slots = %s, quarantined_slots = %s, version = %s "
        "WHERE instance_id = %s AND instance_generation = %s AND version = %s",
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
        "UPDATE admission_leases SET lease_state = %s, cleanup_state = %s, "
        "quarantine_observed_at_us = %s, quarantine_reason_code = %s, version = %s "
        "WHERE lease_id = %s AND version = %s",
        (
            fact.lease_state,
            fact.cleanup_state,
            fact.observed_at_us,
            fact.reason_code,
            fact.version,
            fact.lease_id,
            lease["version"],
        ),
    )
    return LeaseResult("committed", request.lease_id, fact)
