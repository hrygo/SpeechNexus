"""Insert one non-content ASR settlement root inside the existing admission transaction.

The root answers only "who owns settling this lease". It carries no execution
state, stop evidence or command digest: the release transaction re-reads every
authorization fact under its own locks, so a conflicting root must fail the
admission instead of being repaired later.
"""

from __future__ import annotations

from typing import Any, Protocol

from speechnexus_backend.speech.domain.admission import (
    AdmissionRejectedError,
    AdmissionRequest,
)


class SettlementCursor(Protocol):
    def execute(self, query: str, parameters: tuple[object, ...] = ()) -> object: ...

    def fetchall(self) -> list[dict[str, Any]]: ...


_KEYS = ("lease_id", "operation_id", "attempt_id", "gateway_id", "host_node_id")


def insert_asr_settlement(cursor: SettlementCursor, request: AdmissionRequest) -> None:
    """Caller verified the ASR business shape and admission in this transaction."""
    stages = request.stages
    identity = request.identity
    if (
        len(stages) != 1
        or not isinstance(identity.operation_id, str)
        or not identity.operation_id.startswith("operation-")
    ):
        raise AdmissionRejectedError("invalid_execution_request")
    stage = stages[0]
    values = (
        stage.lease_id,
        identity.operation_id,
        identity.attempt_id,
        stage.gateway_id,
        stage.host_node_id,
    )
    cursor.execute(
        "INSERT INTO speech_asr_settlement "
        "(lease_id,operation_id,attempt_id,gateway_id,host_node_id,next_check_at_us) "
        "VALUES (?,?,?,?,?,0) ON CONFLICT (lease_id) DO NOTHING",
        values,
    )
    cursor.execute(
        "SELECT lease_id,operation_id,attempt_id,gateway_id,host_node_id "
        "FROM speech_asr_settlement WHERE lease_id=?",
        (stage.lease_id,),
    )
    rows = cursor.fetchall()
    if len(rows) != 1 or tuple(rows[0][key] for key in _KEYS) != values:
        # A root already pinned to another identity is a repair problem, never a
        # reason to keep the admission and let the worker guess which lease it is.
        raise AdmissionRejectedError("reconciliation_required")


ASR_SETTLEMENT_BACKFILL = """INSERT INTO speech_asr_settlement
    (lease_id, operation_id, attempt_id, gateway_id, host_node_id, next_check_at_us)
    SELECT l.lease_id, l.operation_id, l.attempt_id, l.gateway_id, l.host_node_id, 0
    FROM admission_leases AS l
    JOIN admission_operations AS o ON o.operation_id = l.operation_id
    WHERE l.lease_state <> 'released'
      AND o.operation_id LIKE 'operation-%'
      AND (CASE WHEN o.operation_id LIKE 'operation-%'
                THEN CAST(o.request_document AS jsonb)->>'kind' END) = 'asr_file'
    ON CONFLICT (lease_id) DO NOTHING"""
