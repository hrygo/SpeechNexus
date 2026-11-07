"""Build the frozen ``voiceclone`` submit that measures one candidate revision.

Validation reuses the existing clone command deliberately: the wire contract,
gateway command parser and engine path already exist, so this slice adds no new
command kind. The only difference from a generation submit is *which* text the
probe asks the engine to synthesize - it must be new text, not the generation
preview, so a successful result is evidence that reusable TTS accepts unseen
text rather than replaying a cached render.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any, cast

from speechnexus_contracts.execution_models import SubmitExecutionRequest

from speechnexus_backend.speech.application.transcription import AdmittedExecution, StagedAudio
from speechnexus_backend.speech.infrastructure.transcription_submit import (
    command_digest,
    deadline_text,
)

# Server-decided measurement budget. The binding contract has no voice
# execution block, and this Job must not borrow ASR/TTS defaults: validation is
# a bounded measurement, not a caller-selected synthesis configuration. The
# value stays inside the existing 600 s public Job cap.
VOICE_VALIDATION_TIMEOUT_SECONDS = 600
VOICE_VALIDATION_SAMPLE_RATE_HZ = 24_000
VOICE_VALIDATION_MAX_OUTPUT_AUDIO_MS = 60_000
VOICE_VALIDATION_AUDIO_MEDIA_TYPE = "audio/wav"
VOICE_VALIDATION_OUTPUT_FORMAT = "wav"

_LANGUAGE = re.compile(r"^(auto|[a-z]{2,3}(-[A-Za-z0-9]{2,8})*)$")
_EMPTY_RESOURCES: dict[str, Any] = {
    "voice": None,
    "voice_material": None,
    "vocabulary": [],
    "pronunciation": [],
    "voice_kind": None,
    "vocabulary_source": None,
    "pronunciation_source": None,
}


def validation_probe_text(job_id: str) -> str:
    """A deterministic sentence that cannot be the generation preview by accident.

    The token makes the exact probe addressable from the report and prevents a
    caller from reusing one validation Job's text for another. The sentence is
    intentionally plain ASCII so the probe exercises text-to-speech rather than
    the draft's language-specific text normalizer.
    """
    token = hashlib.sha256(f"validation-probe:{job_id}".encode()).hexdigest()[:16]
    return (
        f"SpeechNexus validation probe {token}. "
        "This sentence is new text, not the generation preview."
    )


def validation_language(value: object) -> str:
    """Return the declared draft language or the internal auto sentinel."""
    if value is None:
        return "auto"
    if not isinstance(value, str) or _LANGUAGE.fullmatch(value) is None:
        raise ValueError("Validation draft language is outside the frozen contract")
    return value


def build_voice_validation_submit(
    admitted: AdmittedExecution,
    reference: StagedAudio,
    *,
    reference_sample_rate_hz: int,
    reference_channels: int,
    reference_text: str | None,
    language: str,
    preview_text: str,
) -> SubmitExecutionRequest:
    """Build one complete clone command from already-frozen admission facts."""
    if not 1 <= len(preview_text) <= 2000:
        raise ValueError("Validation probe text is outside the frozen contract")
    if reference_text is not None and not 1 <= len(reference_text) <= 10000:
        raise ValueError("Validation reference text is outside the frozen contract")
    if _LANGUAGE.fullmatch(language) is None:
        raise ValueError("Validation language is outside the frozen contract")
    if reference_sample_rate_hz not in (16_000, 24_000, 48_000):
        raise ValueError("Validation reference sample rate is outside the frozen contract")
    if reference_channels < 1 or reference_channels > 8:
        raise ValueError("Validation reference channel count is outside the frozen contract")
    command: dict[str, Any] = {
        "schema_version": "1.0.0",
        "kind": "voiceclone",
        "task_kind": "voice_clone",
        "stage_role": "voice_clone_base",
        "resources": _EMPTY_RESOURCES,
        "reference_audio": {
            "storage": {
                "asset_or_artifact_id": reference.asset_id,
                "workspace_id": reference.workspace_id,
                "storage_bucket": reference.bucket,
                "object_key": reference.object_key,
                "content_sha256": reference.content_sha256,
                "byte_length": reference.byte_length,
            },
            "media_type": VOICE_VALIDATION_AUDIO_MEDIA_TYPE,
            "sample_rate_hz": reference_sample_rate_hz,
            "channels": reference_channels,
        },
        "reference_text": reference_text,
        "preview_text": preview_text,
        "language": language,
        "source_use_confirmed": True,
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
        "max_output_audio_ms": VOICE_VALIDATION_MAX_OUTPUT_AUDIO_MS,
        "sample_rate_hz": VOICE_VALIDATION_SAMPLE_RATE_HZ,
    }
    return cast(SubmitExecutionRequest, request)


__all__ = [
    "VOICE_VALIDATION_AUDIO_MEDIA_TYPE",
    "VOICE_VALIDATION_MAX_OUTPUT_AUDIO_MS",
    "VOICE_VALIDATION_OUTPUT_FORMAT",
    "VOICE_VALIDATION_SAMPLE_RATE_HZ",
    "VOICE_VALIDATION_TIMEOUT_SECONDS",
    "build_voice_validation_submit",
    "validation_language",
    "validation_probe_text",
]
