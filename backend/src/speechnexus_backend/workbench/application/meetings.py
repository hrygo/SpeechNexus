"""Meeting application boundary; the injected store owns each atomic transaction."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol

from speechnexus_backend.workbench.application.llm import LlmProfile, TextLlm
from speechnexus_backend.workbench.application.minutes_tasks import MinutesTaskView
from speechnexus_backend.workbench.domain.meetings import Meeting, MeetingSourceUnsupportedError
from speechnexus_backend.workbench.domain.versions import (
    InvalidVersionChangeError,
    Unset,
    validate_revision_number,
)


@dataclass(frozen=True, slots=True)
class MeetingView:
    meeting: Meeting
    minutes: MinutesTaskView | None = None

    def __post_init__(self) -> None:
        if self.minutes is None:
            if self.meeting.minutes_id is not None:
                raise InvalidVersionChangeError("meeting minutes fact missing")
        elif self.meeting.minutes_id != self.minutes.task.id or not self.meeting.matches_minutes(
            self.minutes.task
        ):
            raise InvalidVersionChangeError("meeting minutes fact mismatch")

    @property
    def status(self) -> Literal["capturing", "sealed", "minutes_pending", "minutes_ready"]:
        if self.minutes is not None:
            return "minutes_ready" if self.minutes.task.status == "succeeded" else "minutes_pending"
        return "capturing" if self.meeting.sealed_revision is None else "sealed"


class MeetingStore(Protocol):
    async def create(
        self,
        *,
        project_id: str,
        workspace_id: str,
        transcript_id: str,
        display_name: str,
        key: str | None,
    ) -> Meeting: ...
    async def get(self, *, meeting_id: str, workspace_id: str) -> MeetingView: ...
    async def seal(
        self,
        *,
        meeting_id: str,
        workspace_id: str,
        expected_revision: int,
        text: str | Unset,
        key: str | None,
    ) -> MeetingView: ...
    async def enqueue_minutes(
        self,
        *,
        meeting_id: str,
        workspace_id: str,
        template_revision: int,
        llm_model: str | None,
        profile: LlmProfile | None,
        binding_id: str | None,
        key: str | None,
        expected_source_revision: int | None = None,
    ) -> MinutesTaskView: ...
    async def get_minutes(
        self, *, meeting_id: str, workspace_id: str, minutes_id: str
    ) -> MinutesTaskView: ...


class WorkbenchMeetings:
    def __init__(self, store: MeetingStore, llm: TextLlm | None = None) -> None:
        self._store = store
        self._llm = llm

    @property
    def llm_enabled(self) -> bool:
        return (
            self._llm is not None
            and self._llm.profile is not None
            and self._llm.binding_id is not None
        )

    @property
    def llm_profile(self) -> LlmProfile | None:
        return self._llm.profile if self.llm_enabled and self._llm is not None else None

    async def create(
        self,
        *,
        project_id: str,
        workspace_id: str,
        display_name: str,
        transcript_id: str | None = None,
        speech_session_id: str | None = None,
        save_audio: bool = False,
        key: str | None = None,
    ) -> MeetingView:
        if (transcript_id is None) == (speech_session_id is None) or type(save_audio) is not bool:
            raise InvalidVersionChangeError("invalid meeting source selection")
        if speech_session_id is not None or save_audio:
            raise MeetingSourceUnsupportedError
        assert transcript_id is not None
        return MeetingView(
            await self._store.create(
                project_id=project_id,
                workspace_id=workspace_id,
                transcript_id=transcript_id,
                display_name=display_name,
                key=key,
            )
        )

    async def get(self, *, meeting_id: str, workspace_id: str) -> MeetingView:
        return await self._store.get(meeting_id=meeting_id, workspace_id=workspace_id)

    async def seal(
        self,
        *,
        meeting_id: str,
        workspace_id: str,
        expected_revision: int,
        text: str | Unset = Unset.VALUE,
        key: str | None = None,
    ) -> MeetingView:
        validate_revision_number(expected_revision)
        return await self._store.seal(
            meeting_id=meeting_id,
            workspace_id=workspace_id,
            expected_revision=expected_revision,
            text=text,
            key=key,
        )

    async def create_minutes(
        self,
        *,
        meeting_id: str,
        workspace_id: str,
        template_revision: int,
        llm_model: str | None = None,
        key: str | None = None,
        expected_source_revision: int | None = None,
    ) -> MinutesTaskView:
        validate_revision_number(template_revision)
        if expected_source_revision is not None:
            validate_revision_number(expected_source_revision)
        # Store replays an accepted intent before checking the current optional profile.
        # No model request or background task is dispatched by the HTTP command.
        return await self._store.enqueue_minutes(
            meeting_id=meeting_id,
            workspace_id=workspace_id,
            template_revision=template_revision,
            llm_model=llm_model,
            profile=None if self._llm is None else self._llm.profile,
            binding_id=None if self._llm is None else self._llm.binding_id,
            key=key,
            expected_source_revision=expected_source_revision,
        )

    async def get_minutes(
        self, *, meeting_id: str, workspace_id: str, minutes_id: str
    ) -> MinutesTaskView:
        return await self._store.get_minutes(
            meeting_id=meeting_id, workspace_id=workspace_id, minutes_id=minutes_id
        )
