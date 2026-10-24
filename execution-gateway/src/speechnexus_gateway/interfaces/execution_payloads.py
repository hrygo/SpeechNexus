"""Frozen execution payload translation shared by every gateway entry point.

The HTTP route and the trusted process-local boundary accept the same
``SubmitExecutionRequest`` and must reject the same values, so both translate it here
instead of keeping two copies of the identity rules.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, cast

from speechnexus_contracts import execution_models as models

from speechnexus_gateway.domain.submission import (
    InvalidExecutionRequestError,
    SubmissionIdentity,
    SubmitRequest,
    derived_output_stream_id,
)


def derived_stream_id_for(payload: models.SubmitExecutionRequest) -> str | None:
    """Name the one logical stream of a PCM attempt, or nothing when a stage emits no audio.

    The frozen request carries the output ceiling and sample rate but no stream name, so a
    stage that emits PCM names its stream from its own identity and every stage of that
    attempt shares it.
    """
    if payload["sample_rate_hz"] is None:
        return None
    return derived_output_stream_id(payload["operation_id"], payload["attempt_id"])


def submit_request_from_payload(
    payload: models.SubmitExecutionRequest, *, output_stream_id: str | None
) -> SubmitRequest:
    """Translate one frozen wire request into the domain request, rejecting bad values."""
    try:
        deadline_at = datetime.fromisoformat(payload["deadline_at"])
    except ValueError as error:
        raise InvalidExecutionRequestError("deadline_at must be an RFC 3339 instant") from error
    try:
        identity = SubmissionIdentity(
            submit_intent_id=payload["submit_intent_id"],
            lease_id=payload["lease_id"],
            operation_id=payload["operation_id"],
            attempt_id=payload["attempt_id"],
            stage_id=payload["stage_id"],
            workspace_id=payload["workspace_id"],
            pool_id=payload["pool_id"],
            host_node_id=payload["host_node_id"],
            gateway_id=payload["gateway_id"],
            instance_id=payload["instance_id"],
            instance_generation=payload["instance_generation"],
            binding_revision_id=payload["binding_revision_id"],
            policy_revision_id=payload["policy_revision_id"],
            voice_revision_id=payload["voice_revision_id"],
            fencing_token=payload["fencing_token"],
            command_digest=payload["command_digest"],
            deadline_at=deadline_at,
            max_output_audio_ms=payload["max_output_audio_ms"],
            sample_rate_hz=payload["sample_rate_hz"],
        )
        return SubmitRequest(
            identity=identity,
            command=cast(Any, payload["command"]),
            output_stream_id=output_stream_id,
        )
    except ValueError as error:
        raise InvalidExecutionRequestError(f"the execution request is invalid: {error}") from error
