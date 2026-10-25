"""Private assistant snapshot projected into the generated wire shape.

The wire turn deliberately carries no model or profile: those name server-side
configuration and stay out of the response.
"""

from datetime import datetime
from typing import Any, cast

from speechnexus_contracts.workbench import parse

from speechnexus_backend.workbench.domain.assistant import AssistantTurn


def _time(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def assistant_turn_payload(value: AssistantTurn) -> Any:
    if type(value) is not AssistantTurn:
        raise ValueError("invalid assistant turn")
    return parse(
        "AssistantTurn",
        cast(
            Any,
            {
                "object": "nexus.assistant_turn",
                "id": value.id,
                "project_id": value.project_id,
                "status": value.status,
                "input_text": value.input_text,
                "output_text": value.output_text,
                "voice": {"id": value.voice.id, "revision": str(value.voice.revision)},
                "speech_job_id": value.speech_job_id,
                "playback_generation": value.playback_generation,
                "error": None
                if value.error is None
                else {
                    "code": value.error.code,
                    "message": value.error.message,
                    "retryable": value.error.retryable,
                },
                "created_at": _time(value.created_at),
                "updated_at": _time(value.updated_at),
            },
        ),
    )
