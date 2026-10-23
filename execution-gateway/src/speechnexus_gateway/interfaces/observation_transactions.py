"""Offline coordination boundary for trusted observations, separate from HTTP."""

from datetime import datetime
from typing import Any

from speechnexus_contracts.validation import validate_coordination

from speechnexus_gateway.application.observe_execution import ExecutionObservationService
from speechnexus_gateway.domain.observation import (
    ExecutionObservation,
    RecordExecutionObservation,
)
from speechnexus_gateway.domain.recovery import ExecutionSnapshot
from speechnexus_gateway.domain.submission import (
    CleanupState,
    ExecutionErrorCode,
    ExecutionRejectedError,
    ExecutionState,
    InvalidExecutionRequestError,
    StaleSubmissionVersionError,
    StopEvidenceKind,
    SubmissionIdentity,
    SubmissionOutcomeUnknownError,
    SubmissionStoreError,
    max_output_audio_ms_for,
)


def observation_command(request: Any) -> RecordExecutionObservation:
    """Translate a validated boundary snapshot without importing DTOs into the domain."""
    value = request["observation"]
    watermark = value.get("output_watermark")
    if value.get("staging_artifact_ref") is not None:
        raise InvalidExecutionRequestError("observations do not publish staging artifacts")
    if watermark is not None and watermark["sample_rate_hz"] != value["sample_rate_hz"]:
        raise InvalidExecutionRequestError("output observation uses another sample rate")
    identity = SubmissionIdentity(
        submit_intent_id=value["submit_intent_id"],
        lease_id=value["lease_id"],
        operation_id=value["operation_id"],
        attempt_id=value["attempt_id"],
        stage_id=value["stage_id"],
        workspace_id=value["workspace_id"],
        pool_id=value["pool_id"],
        host_node_id=value["host_node_id"],
        gateway_id=value["gateway_id"],
        instance_id=value["instance_id"],
        instance_generation=int(value["instance_generation"]),
        binding_revision_id=value["binding_revision_id"],
        policy_revision_id=value["policy_revision_id"],
        voice_revision_id=value["voice_revision_id"],
        fencing_token=int(value["fencing_token"]),
        command_digest=value["command_digest"],
        deadline_at=datetime.fromisoformat(value["deadline_at"]),
        max_output_audio_ms=(
            None
            if watermark is None
            else max_output_audio_ms_for(
                ceiling_samples=int(watermark["hard_output_ceiling_samples"]),
                sample_rate_hz=int(watermark["sample_rate_hz"]),
            )
        ),
        sample_rate_hz=None if value["sample_rate_hz"] is None else int(value["sample_rate_hz"]),
    )
    return RecordExecutionObservation(
        ExecutionObservation(
            identity=identity,
            output_stream_id=None if watermark is None else watermark["output_stream_id"],
            generated_pcm_samples=0
            if watermark is None
            else int(watermark["generated_pcm_samples"]),
            execution_state=ExecutionState(value["execution_state"]),
            cleanup_state=CleanupState(value["cleanup_state"]),
            dispatched_at=(
                None
                if value["dispatched_at"] is None
                else datetime.fromisoformat(value["dispatched_at"])
            ),
            engine_request_id=value.get("engine_request_id"),
            error_code=(
                None if value.get("error_code") is None else ExecutionErrorCode(value["error_code"])
            ),
            stop_evidence_kind=StopEvidenceKind(value["stop_evidence_kind"]),
            observed_at=datetime.fromisoformat(value["updated_at"]),
        ),
        expected_version=int(request["expected_version"]),
    )


class ExecutionObservationBoundary:
    def __init__(self, service: ExecutionObservationService) -> None:
        self._service = service

    def record_execution_observation(self, request: Any) -> dict[str, Any]:
        validate_coordination("record_execution_observation", request)
        result: dict[str, Any] = {
            "schema_version": "1.0.0",
            "operation": "record_execution_observation",
            "scope_id": request["observation"]["submit_intent_id"],
            "recovery": "none",
        }
        try:
            try:
                command = observation_command(request)
            except KeyError, TypeError, ValueError:
                raise InvalidExecutionRequestError("incomplete execution observation") from None
            saved = self._service.record_execution_observation(command)
            result.update(
                outcome="replayed" if saved.version == command.expected_version else "committed",
                version=str(saved.version),
                value=ExecutionSnapshot(saved).response(),
            )
        except SubmissionOutcomeUnknownError:
            result.update(
                outcome="unknown", error_code="outcome_unknown", recovery="query_original"
            )
        except SubmissionStoreError as error:
            result.update(
                outcome="conflict"
                if isinstance(error, StaleSubmissionVersionError)
                else "rejected",
                error_code=error.code,
                recovery=error.recovery,
            )
        except ExecutionRejectedError as error:
            result.update(
                outcome="rejected",
                error_code=error.error_code.value,
                recovery=(
                    "reconcile"
                    if error.error_code is ExecutionErrorCode.EXECUTION_CLEANUP_PENDING
                    else "none"
                ),
            )
        validate_coordination("TransactionResult", result)
        return result
