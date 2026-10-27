"""Build the frozen gateway submit document from an admitted synchronous ASR."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import Any, cast

import rfc8785
from speechnexus_contracts.execution_models import SubmitExecutionRequest

from speechnexus_backend.speech.application.transcription import (
    AdmittedExecution,
    StagedAudio,
    TranscriptionAudioUnsupportedError,
    TranscriptionUnsupportedError,
)
from speechnexus_backend.speech.domain.transcription import (
    TranscriptFormat,
    TranscriptionPlan,
)
from speechnexus_backend.speech.infrastructure.transcription_recipe import (
    AsrParameters,
)

_GATEWAY_MEDIA_TYPES = frozenset(
    {"audio/wav", "audio/flac", "audio/mpeg", "audio/ogg", "audio/pcm"}
)


def command_digest(command: dict[str, Any]) -> str:
    return "sha256:" + hashlib.sha256(rfc8785.dumps(command)).hexdigest()


def deadline_text(deadline_at_us: int) -> str:
    at = datetime.fromtimestamp(deadline_at_us / 1_000_000, UTC)
    return at.isoformat(timespec="microseconds").replace("+00:00", "Z")


class TranscriptionCommandPlanner:
    """Pure production builder; admission and storage provide their own facts."""

    def __init__(self, asr: AsrParameters) -> None:
        self._asr = asr

    def validate(self, plan: TranscriptionPlan) -> None:
        """Reject client values the file-ASR gateway cannot represent."""
        if plan.audio_media_type not in _GATEWAY_MEDIA_TYPES:
            raise TranscriptionAudioUnsupportedError
        if plan.timestamp_granularities:
            # The file-ASR gateway command rejects a non-empty list; it cannot
            # ask the engine for spacing that was not already requested by the
            # binding. Never silently drop a client parameter.
            raise TranscriptionUnsupportedError(
                "The file ASR gateway cannot request timestamp granularities.",
                param="timestamp_granularities[]",
            )

    def build(
        self, plan: TranscriptionPlan, admitted: AdmittedExecution, audio: StagedAudio
    ) -> SubmitExecutionRequest:
        self.validate(plan)
        gateway_format = (
            plan.response_format.value
            if plan.response_format in (TranscriptFormat.JSON, TranscriptFormat.TEXT)
            else "json"
        )
        command: dict[str, Any] = {
            "schema_version": "1.0.0",
            "kind": "asr",
            "task_kind": "asr_file",
            "stage_role": "asr_batch",
            "resources": {
                "voice": None,
                "voice_material": None,
                "vocabulary": [],
                "pronunciation": [],
                "voice_kind": None,
                "vocabulary_source": None,
                "pronunciation_source": None,
            },
            "audio": {
                "storage": {
                    "asset_or_artifact_id": audio.asset_id,
                    "workspace_id": audio.workspace_id,
                    "storage_bucket": audio.bucket,
                    "object_key": audio.object_key,
                    "content_sha256": audio.content_sha256,
                    "byte_length": audio.byte_length,
                },
                "media_type": plan.audio_media_type,
                "sample_rate_hz": _sample_rate(plan),
                "channels": _channels(plan),
            },
            "parameters": {
                "language": plan.language,
                "turn_detection": {
                    "mode": "server_vad",
                    "silence_duration_ms": self._asr.silence_duration_ms,
                },
                "finalization": "full_segment",
                "preview_interval_ms": self._asr.preview_interval_ms,
                "max_segment_ms": plan.max_segment_ms,
                "rollback_tokens": 0,
                "final_deadline_ms": self._asr.final_deadline_ms,
                "auxiliary_deadline_ms": self._asr.auxiliary_deadline_ms,
                "alignment": "off",
                "diarization": "off",
                "vocabulary_set_revision": None,
                "prompt": plan.prompt or None,
                "temperature": self._asr.temperature,
                "response_format": gateway_format,
                "timestamp_granularities": [],
            },
        }
        request: dict[str, object] = {
            "submit_intent_id": admitted.submit_intent_id,
            "lease_id": admitted.lease_id,
            "operation_id": admitted.operation_id,
            "attempt_id": admitted.attempt_id,
            "stage_id": admitted.stage_id,
            "workspace_id": admitted.workspace_id,
            "pool_id": admitted.pool_id,
            "host_node_id": admitted.host_node_id,
            "gateway_id": admitted.gateway_id,
            "instance_id": admitted.instance_id,
            "instance_generation": admitted.instance_generation,
            "binding_revision_id": admitted.binding_revision_id,
            "policy_revision_id": admitted.policy_revision_id,
            "voice_revision_id": None,
            "fencing_token": admitted.fencing_token,
            "command_digest": command_digest(command),
            "command": command,
            "deadline_at": deadline_text(admitted.deadline_at_us),
            "max_output_audio_ms": None,
            "sample_rate_hz": None,
        }
        return cast(SubmitExecutionRequest, request)


def _sample_rate(plan: TranscriptionPlan) -> int:
    return plan.audio_sample_rate_hz


def _channels(plan: TranscriptionPlan) -> int:
    return plan.audio_channels


__all__ = ["TranscriptionCommandPlanner", "command_digest", "deadline_text"]
