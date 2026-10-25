"""Meeting source identity and optional minutes remain separate facts."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest

from speechnexus_backend.workbench.application.llm import LlmProfile
from speechnexus_backend.workbench.application.meetings import (
    MeetingStore,
    MeetingView,
    WorkbenchMeetings,
)
from speechnexus_backend.workbench.application.minutes_tasks import MinutesTaskView
from speechnexus_backend.workbench.domain.meetings import Meeting, MeetingSourceUnsupportedError
from speechnexus_backend.workbench.domain.minutes_tasks import MinutesTask
from speechnexus_backend.workbench.domain.versions import InvalidVersionChangeError, Transcript

NOW = datetime(2026, 10, 24, tzinfo=UTC)


def source() -> Transcript:
    return Transcript.create(
        transcript_id="transcript-demo",
        project_id="project-demo",
        display_name="合成稿",
        revision_id="revision-one",
        text=" 原稿 e\u0301😀\n ",
        now=NOW,
    )


def meeting() -> Meeting:
    return Meeting.create(
        meeting_id="meeting-demo",
        workspace_id="workspace-demo",
        project_id="project-demo",
        transcript=source(),
        display_name="合成会议",
        now=NOW,
    )


def sealed() -> Meeting:
    revision = source().seal(expected_revision=1, revision_id="revision-sealed", now=NOW).head
    return meeting().seal(revision, now=NOW)


def minutes(owner: Meeting) -> MinutesTaskView:
    assert owner.sealed_revision is not None
    assert owner.sealed_revision_id is not None
    assert owner.source_sha256 is not None
    task = MinutesTask.create(
        task_id="minutes-demo",
        workspace_id=owner.workspace_id,
        project_id=owner.project_id,
        transcript_id=owner.transcript_id,
        source_revision=owner.sealed_revision,
        source_revision_id=owner.sealed_revision_id,
        source_sha256=owner.source_sha256,
        profile_sha256="sha256:" + "a" * 64,
        binding_id="sha256:" + "b" * 64,
        now=NOW,
    )
    return MinutesTaskView(
        task, LlmProfile(model="synthetic-model", template_revision=1, system_prompt="合成模板")
    )


def test_creation_has_no_fabricated_seal_audio_or_minutes() -> None:
    m = meeting()
    assert m.transcript_id == "transcript-demo" and m.version == 1
    assert m.sealed_revision is None and m.ended_at is None and m.minutes_id is None
    assert not m.save_audio and MeetingView(m).status == "capturing"


def test_seal_pins_the_exact_immutable_source_and_preserves_original_text() -> None:
    original = source()
    revision = original.seal(expected_revision=1, revision_id="revision-sealed", now=NOW).head
    m = meeting().seal(revision, now=NOW)
    assert m.sealed_revision == 2 and m.sealed_revision_id == revision.id
    assert m.version == 2 and m.ended_at == NOW and MeetingView(m).status == "sealed"
    assert original.head.text == revision.text == " 原稿 e\u0301😀\n "


def test_explicit_reseal_keeps_capture_end_and_clears_latest_minutes_link() -> None:
    m = sealed()
    current = minutes(m)
    attached = m.attach_minutes(current.task, now=NOW)
    later = replace(source().head, id="revision-later", number=3, status="sealed")
    changed = attached.seal(later, now=NOW + timedelta(seconds=1))
    assert changed.ended_at == m.ended_at and changed.updated_at > m.updated_at
    assert changed.minutes_id is None and changed.sealed_revision == 3
    assert m.sealed_revision == 2 and attached.minutes_id == "minutes-demo"


@pytest.mark.parametrize("change", [{"transcript_id": "another"}, {"status": "draft"}])
def test_seal_rejects_wrong_source_or_unsealed_revision(change: dict[str, Any]) -> None:
    revision = replace(
        source().seal(expected_revision=1, revision_id="revision-sealed", now=NOW).head,
        **change,
    )
    with pytest.raises(InvalidVersionChangeError):
        meeting().seal(revision, now=NOW)


def test_stale_reseal_and_invalid_time_or_version_are_rejected() -> None:
    m = sealed()
    revision = replace(source().head, id="revision-sealed", number=2, status="sealed")
    with pytest.raises(InvalidVersionChangeError):
        m.seal(revision, now=NOW)
    with pytest.raises(InvalidVersionChangeError):
        m.seal(replace(revision, number=3), now=NOW - timedelta(seconds=1))
    with pytest.raises(InvalidVersionChangeError):
        replace(m, version=10**19 - 1).seal(replace(revision, number=3), now=NOW)


@pytest.mark.parametrize(
    "field", ["workspace_id", "project_id", "transcript_id", "source_revision_id", "source_sha256"]
)
def test_minutes_cannot_be_attached_to_a_different_source(field: str) -> None:
    m = sealed()
    task = minutes(m).task
    wrong = "sha256:" + "c" * 64 if field == "source_sha256" else "another"
    with pytest.raises(InvalidVersionChangeError):
        changes: dict[str, Any] = {field: wrong}
        m.attach_minutes(replace(task, **changes), now=NOW)


def test_minutes_success_failure_and_expiry_do_not_change_capture_fact() -> None:
    m = sealed()
    v = minutes(m)
    attached = m.attach_minutes(v.task, now=NOW)
    running = v.task.claim(token="attempt-demo", now=NOW)
    failed = running.finish(token="attempt-demo", text=None, error="unavailable", now=NOW)
    ready = running.finish(token="attempt-demo", text="合成纪要", error=None, now=NOW)
    assert MeetingView(attached, v).status == "minutes_pending"
    assert MeetingView(attached, replace(v, task=failed)).status == "minutes_pending"
    assert MeetingView(attached, replace(v, task=ready)).status == "minutes_ready"
    assert MeetingView(attached, replace(v, task=ready.expire(now=NOW))).status == "minutes_pending"
    assert attached.sealed_revision == 2 and attached.ended_at == NOW
    with pytest.raises(InvalidVersionChangeError):
        MeetingView(attached)
    with pytest.raises(InvalidVersionChangeError):
        MeetingView(m, v)


@pytest.mark.parametrize("bad", ["", "x" * 129, "unsafe\x00name", "unsafe\ud800name"])
def test_invalid_meeting_name_is_rejected_without_exposing_content(bad: str) -> None:
    with pytest.raises(InvalidVersionChangeError) as caught:
        Meeting.create(
            meeting_id="m",
            workspace_id="w",
            project_id="project-demo",
            transcript=source(),
            display_name=bad,
            now=NOW,
        )
    assert "unsafe" not in str(caught.value)


class NoStore:
    async def create(self, **kwargs: object) -> Meeting:
        raise AssertionError("unsupported source must fail before storage")


@pytest.mark.asyncio
async def test_unverified_live_session_and_audio_intent_are_not_accepted() -> None:
    service = WorkbenchMeetings(cast(MeetingStore, NoStore()))
    for args in (
        {"speech_session_id": "session-demo"},
        {"transcript_id": "transcript-demo", "save_audio": True},
    ):
        with pytest.raises(MeetingSourceUnsupportedError):
            await service.create(
                project_id="project-demo",
                workspace_id="workspace-demo",
                display_name="合成会议",
                key="original-key",
                **args,
            )
