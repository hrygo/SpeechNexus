"""Insert one non-content settlement root inside the existing admission transaction."""

from __future__ import annotations

import json
from typing import Any, Protocol, cast

from speechnexus_contracts.execution import parse

from speechnexus_backend.speech.domain.admission import AdmissionRejectedError


class SettlementCursor(Protocol):
    def execute(self, query: str, parameters: tuple[object, ...] = ()) -> object: ...
    def fetchall(self) -> list[dict[str, Any]]: ...


def insert_tts_settlement(cursor: SettlementCursor, request: Any, deadline_at_us: int) -> None:
    """Caller verifies TTS authority and admission; conflicting roots fail closed."""
    gateway = parse("SubmitExecutionRequest", request)
    if gateway["command"]["kind"] != "tts" or gateway["command"]["task_kind"] != "tts_file":
        raise AdmissionRejectedError("invalid_execution_request")
    keys = (
        "submit_intent_id",
        "lease_id",
        "operation_id",
        "attempt_id",
        "stage_id",
        "workspace_id",
        "pool_id",
        "gateway_id",
        "host_node_id",
        "instance_id",
        "instance_generation",
        "binding_revision_id",
        "policy_revision_id",
        "voice_revision_id",
        "fencing_token",
        "command_digest",
    )
    target = json.dumps(
        {key: cast(dict[str, object], gateway)[key] for key in keys},
        sort_keys=True,
        separators=(",", ":"),
    )
    cursor.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended('speechnexus:tts-cancel:' "
        "|| current_schema() || ':' || %s, 0))",
        (gateway["operation_id"],),
    )
    cursor.execute(
        "SELECT reason FROM speech_tts_cancellation WHERE operation_id=%s",
        (gateway["operation_id"],),
    )
    cancellation = cursor.fetchall()
    if len(cancellation) > 1:
        raise AdmissionRejectedError("reconciliation_required")
    reason = cancellation[0]["reason"] if cancellation else None
    values = (
        gateway["lease_id"],
        gateway["operation_id"],
        gateway["attempt_id"],
        gateway["submit_intent_id"],
        target,
        deadline_at_us,
    )
    cursor.execute(
        "INSERT INTO speech_tts_settlement (lease_id,operation_id,attempt_id,submit_intent_id,"
        "target_document,deadline_at_us,abort_reason) VALUES (%s,%s,%s,%s,%s,%s,%s) "
        "ON CONFLICT (lease_id) DO NOTHING",
        (*values, reason),
    )
    cursor.execute(
        "SELECT lease_id,operation_id,attempt_id,submit_intent_id,target_document,deadline_at_us "
        "FROM speech_tts_settlement WHERE lease_id=%s",
        (gateway["lease_id"],),
    )
    rows = cursor.fetchall()
    if (
        len(rows) != 1
        or tuple(
            rows[0][key]
            for key in (
                "lease_id",
                "operation_id",
                "attempt_id",
                "submit_intent_id",
                "target_document",
                "deadline_at_us",
            )
        )
        != values
    ):
        raise AdmissionRejectedError("reconciliation_required")
