"""Read original admission facts only; never repair, reserve or recreate a Job."""

from __future__ import annotations

import hashlib
import json
from typing import Any

import rfc8785

from speechnexus_backend.speech.domain.admission import (
    AdmissionFact,
    AdmissionIdentity,
    AdmissionRejectedError,
    AdmissionRequest,
    AdmissionResult,
    StageReservation,
)
from speechnexus_backend.speech.domain.owners import MAX_VERSION
from speechnexus_backend.speech.domain.runtime_intents import digest, identifier, integer
from speechnexus_backend.speech.infrastructure.admission_codec import (
    PreparedAdmission,
    request_to_wire,
    request_with_business_digest,
)
from speechnexus_backend.speech.infrastructure.jobs_sql import recover_in_transaction
from speechnexus_backend.speech.infrastructure.leases_sql import _record as lease_record
from speechnexus_backend.speech.infrastructure.runtime_sql import RuntimeCursor

type Row = dict[str, Any]
AdmissionCursor = RuntimeCursor


def rows(cursor: AdmissionCursor, query: str, parameters: tuple[object, ...] = ()) -> list[Row]:
    cursor.execute(query, parameters)
    return cursor.fetchall()


def one(cursor: AdmissionCursor, query: str, parameters: tuple[object, ...]) -> Row:
    values = rows(cursor, query, parameters)
    if len(values) != 1:
        raise AdmissionRejectedError("not_found" if not values else "reconciliation_required")
    return values[0]


def changed(cursor: AdmissionCursor, query: str, parameters: tuple[object, ...]) -> None:
    cursor.execute(query, parameters)
    if cursor.rowcount != 1:
        raise AdmissionRejectedError("reconciliation_required")


def _original_request(
    attempt: Row, envelope: Row, operation: Row, *, max_command_bytes: int
) -> AdmissionRequest:
    try:
        document = envelope["request_document"]
        if not isinstance(document, str) or not 0 < len(document.encode()) <= max_command_bytes:
            raise ValueError
        actual_digest = "sha256:" + hashlib.sha256(document.encode()).hexdigest()
        if (
            envelope["request_digest"] != actual_digest
            or attempt["request_digest"] != actual_digest
        ):
            raise ValueError
        digest(operation["request_digest"])
        request = request_with_business_digest(json.loads(document), operation["request_digest"])
        if rfc8785.dumps(request_to_wire(request)).decode() != document:
            raise ValueError
        if (
            attempt["operation_id"],
            attempt["attempt_id"],
            attempt["workspace_id"],
            attempt["binding_revision_id"],
            attempt["manifest_id"],
            attempt["recipe_digest"],
            attempt["policy_revision_id"],
            attempt["service_class"],
        ) != (
            request.identity.operation_id,
            request.identity.attempt_id,
            request.workspace_id,
            request.binding_revision_id,
            request.manifest_id,
            request.recipe_digest,
            request.policy_revision_id,
            request.service_class,
        ):
            raise ValueError
        required = json.loads(attempt["required_stage_ids"])
        if (
            not isinstance(required, list)
            or any(not isinstance(stage_id, str) for stage_id in required)
            or len(set(required)) != len(required)
            or set(required) != {stage.stage_id for stage in request.stages}
            or operation["workspace_id"] != request.workspace_id
            or attempt["quota_state"] not in ("held", "quarantined", "released")
        ):
            raise ValueError
        identifier(operation["initial_attempt_id"])
        identifier(operation["current_attempt_id"])
        integer(operation["recorded_at_us"], minimum=1)
        integer(envelope["recorded_at_us"], minimum=operation["recorded_at_us"])
        integer(attempt["version"], minimum=1, maximum=MAX_VERSION)
        return request
    except ValueError, TypeError, KeyError, UnicodeError, RecursionError:
        raise AdmissionRejectedError("reconciliation_required") from None


def read_original(
    cursor: AdmissionCursor,
    identity: AdmissionIdentity,
    *,
    max_command_bytes: int,
    request_digest: str | None = None,
    prepared: PreparedAdmission | None = None,
) -> AdmissionResult | None:
    """Missing means unobserved, not permission to perform a fresh execution."""
    key = identity.operation_id, identity.attempt_id
    attempts = rows(
        cursor, "SELECT * FROM admission_attempts WHERE operation_id = %s AND attempt_id = %s", key
    )
    if not attempts:
        return None
    if len(attempts) != 1:
        raise AdmissionRejectedError("reconciliation_required")
    attempt = attempts[0]
    try:
        envelope = one(
            cursor,
            "SELECT * FROM admission_requests WHERE operation_id = %s AND attempt_id = %s",
            key,
        )
        operation = one(
            cursor,
            "SELECT operation_id, workspace_id, request_digest, initial_attempt_id, "
            "current_attempt_id, recorded_at_us FROM admission_operations WHERE operation_id = %s",
            (identity.operation_id,),
        )
    except AdmissionRejectedError:
        raise AdmissionRejectedError("reconciliation_required") from None
    if request_digest is not None and envelope["request_digest"] != request_digest:
        raise AdmissionRejectedError("idempotency_conflict")
    if prepared is not None:
        original = prepared.command.business_command
        if (
            envelope["request_digest"] != prepared.request_digest
            or envelope["request_document"] != prepared.document
            or operation["request_digest"] != original.request_digest
            or operation["workspace_id"] != prepared.command.request.workspace_id
        ):
            raise AdmissionRejectedError("idempotency_conflict")
        body = one(
            cursor,
            "SELECT request_document FROM admission_operations WHERE operation_id = %s",
            (identity.operation_id,),
        )["request_document"]
        # A tombstone cannot restore content or authorize dispatch. Original
        # reservation metadata remains recoverable using its immutable digest.
        if body is not None and body != original.document:
            raise AdmissionRejectedError("reconciliation_required")
    request = _original_request(attempt, envelope, operation, max_command_bytes=max_command_bytes)
    if request.identity != identity:
        raise AdmissionRejectedError("reconciliation_required")
    leases = rows(
        cursor,
        "SELECT * FROM admission_leases WHERE operation_id = %s AND attempt_id = %s "
        "ORDER BY stage_id LIMIT 65",
        key,
    )
    by_stage = {stage.stage_id: stage for stage in request.stages}
    if len(leases) != len(by_stage) or {row["stage_id"] for row in leases} != set(by_stage):
        raise AdmissionRejectedError("reconciliation_required")
    authorities = {authority.pool_id: authority for authority in request.authorities}
    reservations: list[StageReservation] = []
    for row in leases:
        stage = by_stage[row["stage_id"]]
        if (
            row["lease_id"],
            row["operation_id"],
            row["attempt_id"],
            row["pool_id"],
            row["host_node_id"],
            row["gateway_id"],
            row["instance_id"],
            row["instance_generation"],
            row["reserved_slots"],
            row["fencing_token"],
            row["deadline_at_us"],
        ) != (
            stage.lease_id,
            identity.operation_id,
            identity.attempt_id,
            stage.pool_id,
            stage.host_node_id,
            stage.gateway_id,
            stage.instance_id,
            stage.instance_generation,
            stage.reserved_slots,
            authorities[stage.pool_id].fencing_token,
            request.deadline_at_us,
        ):
            raise AdmissionRejectedError("reconciliation_required")
        try:
            fact = lease_record(row)
        except ValueError, TypeError, KeyError:
            raise AdmissionRejectedError("reconciliation_required") from None
        reservations.append(StageReservation(stage, fact.reserved))
    job = recover_in_transaction(cursor, request.job)
    if job.outcome != "replayed":
        raise AdmissionRejectedError("reconciliation_required")
    return AdmissionResult(
        "replayed", identity, AdmissionFact(identity, tuple(reservations), attempt["version"])
    )
