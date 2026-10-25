"""Immutable meeting ownership and sealed sources; no transport or runtime state."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime
from hashlib import sha256

from speechnexus_backend.workbench.domain.minutes_tasks import MinutesTask
from speechnexus_backend.workbench.domain.versions import (
    InvalidVersionChangeError,
    Transcript,
    TranscriptRevision,
    validate_revision_number,
)


class MeetingSourceUnsupportedError(Exception):
    def __init__(self) -> None:
        super().__init__("meeting source or audio persistence is unavailable")


def _text(value: str, maximum: int = 128) -> None:
    if not isinstance(value, str) or not 1 <= len(value) <= maximum or "\x00" in value:
        raise InvalidVersionChangeError("invalid meeting value")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise InvalidVersionChangeError("invalid meeting value") from None


@dataclass(frozen=True, slots=True)
class Meeting:
    id: str
    workspace_id: str
    project_id: str
    transcript_id: str
    display_name: str = field(repr=False)
    save_audio: bool
    started_at: datetime
    updated_at: datetime
    version: int = 1
    ended_at: datetime | None = None
    sealed_revision: int | None = None
    sealed_revision_id: str | None = None
    source_sha256: str | None = None
    minutes_id: str | None = None

    def __post_init__(self) -> None:
        for value in (
            self.id,
            self.workspace_id,
            self.project_id,
            self.transcript_id,
            self.display_name,
        ):
            _text(value)
        validate_revision_number(self.version)
        if (
            type(self.save_audio) is not bool
            or self.started_at.utcoffset() is None
            or self.updated_at.utcoffset() is None
            or self.updated_at < self.started_at
        ):
            raise InvalidVersionChangeError("invalid meeting state")
        if self.sealed_revision is None:
            if any(
                v is not None
                for v in (
                    self.ended_at,
                    self.sealed_revision_id,
                    self.source_sha256,
                    self.minutes_id,
                )
            ):
                raise InvalidVersionChangeError("invalid capturing meeting")
        else:
            validate_revision_number(self.sealed_revision)
            if (
                self.ended_at is None
                or self.ended_at.utcoffset() is None
                or not self.started_at <= self.ended_at <= self.updated_at
                or self.sealed_revision_id is None
                or self.source_sha256 is None
                or len(self.source_sha256) != 71
                or not self.source_sha256.startswith("sha256:")
                or any(c not in "0123456789abcdef" for c in self.source_sha256[7:])
            ):
                raise InvalidVersionChangeError("invalid sealed meeting")
            _text(self.sealed_revision_id)
        if self.minutes_id is not None:
            _text(self.minutes_id)

    @classmethod
    def create(
        cls,
        *,
        meeting_id: str,
        workspace_id: str,
        project_id: str,
        transcript: Transcript,
        display_name: str,
        now: datetime,
    ) -> Meeting:
        if transcript.project_id != project_id:
            raise InvalidVersionChangeError("meeting source parent mismatch")
        return cls(
            meeting_id, workspace_id, project_id, transcript.id, display_name, False, now, now
        )

    def _advance(self, *, now: datetime, **changes: object) -> Meeting:
        if now.utcoffset() is None or now < self.updated_at:
            raise InvalidVersionChangeError("invalid meeting time")
        return replace(self, version=self.version + 1, updated_at=now, **changes)  # type: ignore[arg-type]

    def seal(self, revision: TranscriptRevision, *, now: datetime) -> Meeting:
        if (
            revision.transcript_id != self.transcript_id
            or revision.status != "sealed"
            or (self.sealed_revision is not None and revision.number <= self.sealed_revision)
        ):
            raise InvalidVersionChangeError("invalid meeting seal source")
        return self._advance(
            now=now,
            ended_at=self.ended_at or now,
            sealed_revision=revision.number,
            sealed_revision_id=revision.id,
            source_sha256="sha256:" + sha256(revision.text.encode()).hexdigest(),
            minutes_id=None,
        )

    def matches_minutes(self, task: MinutesTask) -> bool:
        return self.sealed_revision is not None and (
            task.workspace_id,
            task.project_id,
            task.transcript_id,
            task.source_revision,
            task.source_revision_id,
            task.source_sha256,
        ) == (
            self.workspace_id,
            self.project_id,
            self.transcript_id,
            self.sealed_revision,
            self.sealed_revision_id,
            self.source_sha256,
        )

    def attach_minutes(self, task: MinutesTask, *, now: datetime) -> Meeting:
        if not self.matches_minutes(task):
            raise InvalidVersionChangeError("meeting minutes source mismatch")
        return self._advance(now=now, minutes_id=task.id)
