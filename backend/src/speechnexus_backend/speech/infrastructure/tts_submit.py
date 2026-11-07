"""Build the effective private file-TTS command before admission commits."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from speechnexus_contracts.execution import parse
from speechnexus_contracts.execution_models import SubmitExecutionRequest

from speechnexus_backend.speech.application.transcription import AdmittedExecution
from speechnexus_backend.speech.domain.speech_synthesis import SpeechPlan
from speechnexus_backend.speech.infrastructure.transcription_submit import (
    command_digest,
    deadline_text,
)


def build_tts_submit(plan: SpeechPlan, admitted: AdmittedExecution) -> SubmitExecutionRequest:
    """Freeze all effective inputs and identity; callers must persist before sending."""
    if (
        (plan.workspace_id, plan.binding_revision_id)
        != (
            admitted.workspace_id,
            admitted.binding_revision_id,
        )
        or len(plan.text) > plan.planner_max_chars
        or plan.language is None
    ):
        raise ValueError("Speech plan does not match the execution or planner limit")
    resources: dict[str, Any] = {
        "voice": {"id": plan.voice_id, "revision": str(plan.voice_revision)},
        "voice_material": None,
        "vocabulary": [],
        "pronunciation": [],
        "voice_kind": "builtin",
        "vocabulary_source": None,
        "pronunciation_source": None,
    }
    parameters: dict[str, Any] = {
        "text_commit_mode": "explicit",
        "audio_delivery": "complete",
        "language": plan.language,
        "pronunciation_revision": None,
        "reference_text": None,
        "instructions": plan.instructions or None,
        "speed": plan.speed,
        "seed": plan.seed,
        "planner": {
            "max_chars": plan.planner_max_chars,
            "max_wait_ms": plan.planner_max_wait_ms,
        },
        "response_format": plan.response_format.value,
    }
    return build_tts_submit_from_parameters(
        plan, admitted, resources=resources, parameters=parameters
    )


def build_tts_submit_from_parameters(
    plan: SpeechPlan,
    admitted: AdmittedExecution,
    *,
    resources: dict[str, Any],
    parameters: dict[str, Any],
) -> SubmitExecutionRequest:
    """Build one complete TTS command from already-frozen resources and parameters."""
    if (plan.workspace_id, plan.binding_revision_id) != (
        admitted.workspace_id,
        admitted.binding_revision_id,
    ):
        raise ValueError("Speech plan does not match the execution identity")
    command: dict[str, Any] = {
        "schema_version": "1.0.0",
        "kind": "tts",
        "task_kind": "tts_file",
        "stage_role": "tts_connector",
        "resources": resources,
        "text": plan.text,
        "text_stream": None,
        "parameters": parameters,
    }
    request: dict[str, Any] = {
        **{key: value for key, value in asdict(admitted).items() if key != "deadline_at_us"},
        "voice_revision_id": plan.voice_revision_id,
        "command_digest": command_digest(command),
        "command": command,
        "deadline_at": deadline_text(admitted.deadline_at_us),
        "max_output_audio_ms": plan.max_audio_ms,
        "sample_rate_hz": plan.sample_rate_hz,
    }
    return parse("SubmitExecutionRequest", request)
