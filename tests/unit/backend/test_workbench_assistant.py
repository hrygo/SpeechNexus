"""Assistant turns advance once, and a disabled assistant never reaches the store."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import pytest

from speechnexus_backend.workbench.application.assistant import WorkbenchAssistant
from speechnexus_backend.workbench.application.llm import (
    DisabledTextLlm,
    LlmCompletion,
    LlmError,
    LlmProfile,
    LlmRequest,
    LlmUsage,
)
from speechnexus_backend.workbench.domain.assistant import (
    AssistantError,
    AssistantStateConflictError,
    AssistantTurn,
    ResourcePin,
    assistant_time,
)
from speechnexus_backend.workbench.domain.versions import (
    InvalidVersionChangeError,
    RevisionConflictError,
)

NOW = datetime(2026, 10, 24, 12, 0, tzinfo=UTC)
VOICE = ResourcePin("voice_demo", 1)
BINDING = ResourcePin("binding_demo", 3)
FAILURE = AssistantError("unavailable", "合成失败原因", True)


def created(*, model: str | None = None, profile: ResourcePin | None = None) -> AssistantTurn:
    return AssistantTurn.create(
        turn_id="turn_demo",
        project_id="project_demo",
        input_text="合成提问",
        voice=VOICE,
        now=NOW,
        model=model,
        profile=profile,
    )


class _Store:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.turn = created()

    async def create_turn(self, **kwargs: object) -> AssistantTurn:
        self.calls.append(("create", kwargs))
        return self.turn

    async def get_turn(self, **kwargs: object) -> AssistantTurn:
        self.calls.append(("get", kwargs))
        return self.turn

    async def cancel_current_turn(self, **kwargs: object) -> tuple[AssistantTurn, bool]:
        self.calls.append(("cancel", kwargs))
        return created(), False


class _EnabledLlm:
    def __init__(self) -> None:
        self.profile_value = LlmProfile(
            model="internal-llm", template_revision=1, system_prompt="合成系统提示"
        )

    @property
    def binding_id(self) -> str:
        return "sha256:" + "a" * 64

    @property
    def profile(self) -> LlmProfile:
        return self.profile_value

    async def generate(self, request: LlmRequest) -> LlmCompletion:
        return LlmCompletion(
            profile=request.profile,
            text="合成回答",
            usage=LlmUsage(input_tokens=1, output_tokens=1),
        )


def test_a_new_turn_starts_unanswered_with_one_playback_generation() -> None:
    turn = created(model="internal-llm", profile=BINDING)
    assert (turn.status, turn.revision, turn.playback_generation) == ("queued", 1, 1)
    assert turn.created_at == turn.updated_at == NOW
    assert (turn.output_text, turn.error, turn.speech_job_id) == (None, None, None)


def test_a_turn_advances_once_and_refuses_every_later_transition() -> None:
    turn = created()
    running = turn.start(expected_revision=1, speech_job_id="job_demo", now=NOW + timedelta(1))
    assert (running.status, running.revision, running.speech_job_id) == ("running", 2, "job_demo")
    done = running.complete(expected_revision=2, output_text="合成回答", now=NOW + timedelta(2))
    assert (done.status, done.revision, done.output_text, done.error) == (
        "succeeded",
        3,
        "合成回答",
        None,
    )
    attempts: list[Callable[[], AssistantTurn]] = [
        lambda: done.cancel(expected_revision=3, now=NOW + timedelta(3)),
        lambda: done.start(expected_revision=3, speech_job_id="job_other", now=NOW + timedelta(3)),
        lambda: done.fail(expected_revision=3, error=FAILURE, now=NOW + timedelta(3)),
    ]
    for attempt in attempts:
        with pytest.raises(AssistantStateConflictError):
            attempt()


def test_cancellation_and_failure_are_distinct_states() -> None:
    cancelled = created().cancel(expected_revision=1, now=NOW + timedelta(1))
    assert (cancelled.status, cancelled.error) == ("cancelled", None)

    failed = created().fail(expected_revision=1, error=FAILURE, now=NOW + timedelta(1))
    assert failed.status == "failed"
    assert failed.error == FAILURE
    with pytest.raises(RevisionConflictError):
        failed.cancel(expected_revision=1, now=NOW + timedelta(2))


def test_invalid_time_and_state_combinations_are_refused() -> None:
    with pytest.raises(ValueError):
        assistant_time(NOW.replace(tzinfo=None))
    with pytest.raises(ValueError):
        created().cancel(expected_revision=1, now=NOW - timedelta(1))
    with pytest.raises(ValueError):
        AssistantTurn(
            id="turn_demo",
            project_id="project_demo",
            status="failed",
            revision=2,
            input_text="合成提问",
            voice=VOICE,
            playback_generation=1,
            created_at=NOW,
            updated_at=NOW,
        )
    with pytest.raises(ValueError):
        AssistantTurn(
            id="turn_demo",
            project_id="project_demo",
            status="queued",
            revision=1,
            input_text="合成提问",
            voice=VOICE,
            playback_generation=1,
            created_at=NOW,
            updated_at=NOW,
            output_text="合成回答",
        )
    with pytest.raises(ValueError):
        AssistantError("bad code", "合成失败原因", True)


async def test_a_disabled_assistant_refuses_before_the_store_is_touched() -> None:
    for llm in (None, DisabledTextLlm()):
        store = _Store()
        service = WorkbenchAssistant(store, llm)
        assert service.enabled is False
        with pytest.raises(LlmError) as error:
            await service.create_turn(
                workspace_id="workspace-assistant",
                project_id="project_demo",
                input_text="合成提问",
                voice_id="voice_demo",
                voice_revision=1,
                key="assistant-request-key-0001",
            )
        assert error.value.code == "disabled"
        assert store.calls == []


async def test_an_enabled_assistant_validates_input_and_delegates() -> None:
    store = _Store()
    service = WorkbenchAssistant(store, _EnabledLlm())
    assert service.enabled is True
    with pytest.raises(InvalidVersionChangeError, match="invalid workbench request key"):
        await service.create_turn(
            workspace_id="workspace-assistant",
            project_id="project_demo",
            input_text="合成提问",
            voice_id="voice_demo",
            voice_revision=1,
            key="short",
        )
    with pytest.raises(InvalidVersionChangeError, match="incomplete assistant profile reference"):
        await service.create_turn(
            workspace_id="workspace-assistant",
            project_id="project_demo",
            input_text="合成提问",
            voice_id="voice_demo",
            voice_revision=1,
            profile_id="binding_demo",
        )
    assert store.calls == []

    await service.create_turn(
        workspace_id="workspace-assistant",
        project_id="project_demo",
        input_text="合成提问",
        voice_id="voice_demo",
        voice_revision=1,
        profile_id="binding_demo",
        profile_revision=3,
        key="assistant-request-key-0001",
    )
    await service.get_turn(turn_id="turn_demo", workspace_id="workspace-assistant")
    turn, changed = await service.cancel_turn(
        turn_id="turn_demo", workspace_id="workspace-assistant"
    )
    assert changed is False
    assert turn.status == "queued"
    assert [call[0] for call in store.calls] == ["create", "get", "cancel"]
    assert store.calls[0][1]["profile_revision"] == 3
