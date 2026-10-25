"""The assistant projection must satisfy the wire shape it is declared against."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from speechnexus_contracts.workbench_http import validate_response

from speechnexus_backend.workbench.domain.assistant import (
    AssistantError,
    AssistantTurn,
    ResourcePin,
)
from speechnexus_backend.workbench.interfaces.assistant_views import assistant_turn_payload

NOW = datetime(2026, 10, 24, 12, 0, tzinfo=UTC)


def turn(**overrides: Any) -> AssistantTurn:
    fields: dict[str, Any] = {
        "id": "turn_demo",
        "project_id": "project_demo",
        "status": "queued",
        "revision": 1,
        "input_text": "合成提问",
        "voice": ResourcePin("voice_demo", 1),
        "playback_generation": 1,
        "created_at": NOW,
        "updated_at": NOW,
        "model": "internal-llm",
        "profile": ResourcePin("binding_demo", 3),
    }
    fields.update(overrides)
    return AssistantTurn(**fields)


@pytest.mark.parametrize(
    "value",
    [
        turn(),
        turn(
            status="succeeded",
            output_text="合成回答",
            speech_job_id="job_demo",
            profile=None,
            model=None,
        ),
        turn(
            status="failed",
            error=AssistantError("unavailable", "合成失败原因", True),
            updated_at=NOW + timedelta(seconds=1),
        ),
        turn(status="cancelled", voice=ResourcePin("voice_demo", 7), model=None),
    ],
)
def test_every_state_projects_into_a_contract_valid_turn(value: AssistantTurn) -> None:
    payload = assistant_turn_payload(value)
    validate_response("getAssistantTurn", 200, {"body": payload})
    assert payload["object"] == "nexus.assistant_turn"
    assert payload["voice"] == {"id": value.voice.id, "revision": str(value.voice.revision)}
    # Model and profile name server-side configuration and never travel outward.
    assert "model" not in payload
    assert "profile" not in payload


def test_the_projection_keeps_the_private_pins_and_failure_out_of_the_wire_shape() -> None:
    payload = assistant_turn_payload(
        turn(status="failed", error=AssistantError("unavailable", "合成失败原因", False))
    )
    assert payload["error"] == {
        "code": "unavailable",
        "message": "合成失败原因",
        "retryable": False,
    }
    assert payload["speech_job_id"] is None
    assert payload["playback_generation"] == 1


def test_a_foreign_value_is_refused_instead_of_projected() -> None:
    with pytest.raises(ValueError):
        assistant_turn_payload("not a turn")  # type: ignore[arg-type]


def test_a_succeeded_turn_without_a_speech_job_is_refused() -> None:
    with pytest.raises(ValueError):
        turn(status="succeeded", output_text="合成回答", speech_job_id=None)
