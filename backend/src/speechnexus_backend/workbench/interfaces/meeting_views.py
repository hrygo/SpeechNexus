"""Strict public projections; persistence identifiers, prompts and usage stay internal."""

import speechnexus_contracts.workbench_models as wire
from speechnexus_contracts.workbench import parse

from speechnexus_backend.workbench.application.meetings import MeetingView
from speechnexus_backend.workbench.application.minutes_tasks import MinutesTaskView
from speechnexus_backend.workbench.interfaces.version_views import _revision, _time


def meeting_payload(value: MeetingView) -> wire.Meeting:
    meeting = value.meeting
    return parse(
        "Meeting",
        {
            "id": meeting.id,
            "object": "nexus.meeting",
            "project_id": meeting.project_id,
            "display_name": meeting.display_name,
            "status": value.status,
            "transcript_id": meeting.transcript_id,
            "save_audio": meeting.save_audio,
            "sealed_revision": None
            if meeting.sealed_revision is None
            else _revision(meeting.sealed_revision),
            "minutes_id": meeting.minutes_id,
            "started_at": _time(meeting.started_at),
            "ended_at": None if meeting.ended_at is None else _time(meeting.ended_at),
            "error": None,
        },
    )


_FAILURES = {
    "unavailable": "Minutes generation is unavailable.",
    "invalid_response": "Minutes generation returned an invalid response.",
    "interrupted": "Minutes generation was interrupted.",
}


def minutes_payload(meeting_id: str, value: MinutesTaskView) -> wire.MinutesJob:
    task = value.task
    error = None
    if task.status == "failed":
        if task.error not in _FAILURES:
            raise ValueError("missing stable minutes failure")
        error = {
            "code": "minutes_" + task.error,
            "message": _FAILURES[task.error],
            "retryable": False,
            "param": None,
        }
    return parse(
        "MinutesJob",
        {
            "id": task.id,
            "object": "nexus.minutes_job",
            "meeting_id": meeting_id,
            "status": task.status,
            "source_revision": _revision(task.source_revision),
            "template_revision": _revision(value.profile.template_revision),
            "llm_model": value.profile.model,
            "minutes_text": task.text if task.status == "succeeded" else None,
            "artifact_id": None,
            "error": error,
            "created_at": _time(task.created_at),
            "updated_at": _time(task.updated_at),
        },
    )
