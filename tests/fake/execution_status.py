"""Contract-valid execution-status documents for transport test doubles.

A test double that answers the gateway's status route must answer with a document the
frozen coordination contract accepts, otherwise the double silently tests a shape no
gateway can produce -- and a consumer gate keyed on that shape looks broken while it is
right. The documents here are validated by the generated `parse` on construction, so drift
fails at the double instead of at an assertion far away.
"""

from __future__ import annotations

from typing import Any

from speechnexus_contracts.execution import parse

#: Synthetic identities, all shaped like the contract's `Identifier`/`PoolId` patterns.
IDENTITIES: dict[str, Any] = {
    "lease_id": "lease-synthetic-main",
    "operation_id": "job-synthetic-0001",
    "attempt_id": "attempt-synthetic-1",
    "stage_id": "stage-synthetic-main",
    "workspace_id": "workspace-synthetic",
    "pool_id": "tts-realtime",
    "host_node_id": "host-synthetic-1",
    "gateway_id": "gateway-synthetic-1",
    "instance_id": "instance-synthetic-1",
    "instance_generation": 1,
    "binding_revision_id": "binding-synthetic-1",
    "policy_revision_id": "policy-synthetic-1",
    "voice_revision_id": "voice-synthetic-1",
    "fencing_token": 1,
    "command_digest": "sha256:" + "a" * 64,
    "deadline_at": "2026-11-17T12:00:00Z",
    "sample_rate_hz": 24000,
    "updated_at": "2026-11-17T11:00:00Z",
}
#: A stable engine failure code from the contract's closed enum: `failed` requires one.
ENGINE_FAILURE_CODE = "engine_transport_unknown"
#: The instant a dispatched execution reports, so `dispatched_at` is a real date-time.
DISPATCHED_AT = "2026-11-17T11:00:00Z"
ENGINE_REQUEST_ID = "engine-request-synthetic-1"

#: Per-state evidence the contract's invariant schemas require. Keeping them in one table
#: is what stops a double from publishing a cross-field shape no gateway can produce.
STATE_SHAPES: dict[str, dict[str, Any]] = {
    "submitting": {
        "cleanup_state": "pending",
        "stop_evidence_kind": "none",
        "dispatched_at": None,
        "engine_request_id": None,
    },
    "executing": {
        "cleanup_state": "pending",
        "stop_evidence_kind": "none",
        "dispatched_at": DISPATCHED_AT,
        "engine_request_id": ENGINE_REQUEST_ID,
    },
    "stopping": {
        "cleanup_state": "pending",
        "stop_evidence_kind": "none",
        "dispatched_at": DISPATCHED_AT,
        "engine_request_id": ENGINE_REQUEST_ID,
    },
    "succeeded": {
        "cleanup_state": "complete",
        "stop_evidence_kind": "engine_terminal_response",
        "dispatched_at": DISPATCHED_AT,
        "engine_request_id": ENGINE_REQUEST_ID,
    },
    # A failure that reached the engine: `complete` requires an observed dispatch time and
    # verified termination evidence. `not_required` is the other branch and must be asked
    # for explicitly, because it claims no request ever left.
    "failed": {
        "cleanup_state": "complete",
        "stop_evidence_kind": "engine_terminal_response",
        "dispatched_at": DISPATCHED_AT,
        "engine_request_id": ENGINE_REQUEST_ID,
    },
    "failed_not_dispatched": {
        "execution_state": "failed",
        "cleanup_state": "not_required",
        "stop_evidence_kind": "none",
        "dispatched_at": None,
        "engine_request_id": None,
        "error_code": "invalid_execution_request",
    },
    "aborted": {
        "cleanup_state": "complete",
        "stop_evidence_kind": "engine_abort_ack",
        "dispatched_at": DISPATCHED_AT,
        "engine_request_id": ENGINE_REQUEST_ID,
    },
    "unknown_quarantined": {
        "cleanup_state": "quarantined",
        "stop_evidence_kind": "unconfirmed_timeout",
        "dispatched_at": DISPATCHED_AT,
        "engine_request_id": ENGINE_REQUEST_ID,
    },
}


def execution_status(
    *,
    submit_intent_id: str,
    state: str,
    error_code: str | None = None,
    **overrides: Any,
) -> dict[str, Any]:
    """One contract-valid status document for `state`, validated before it is returned.

    `state` names a row of `STATE_SHAPES`; `failed_not_dispatched` is the contract's
    provable non-dispatch branch, which is a different fact from a failure that reached
    the engine.
    """
    shape = dict(STATE_SHAPES[state])
    document: dict[str, Any] = {
        "submit_intent_id": submit_intent_id,
        **IDENTITIES,
        "execution_state": shape.pop("execution_state", state),
        **shape,
    }
    if error_code is not None:
        document["error_code"] = error_code
    elif document["execution_state"] == "failed" and "error_code" not in document:
        # `failed` must carry a stable error code; a double that omits it produces a
        # document no gateway can answer with.
        document["error_code"] = ENGINE_FAILURE_CODE
    document.update(overrides)
    parse("ExecutionStatusResponse", document)
    return document
