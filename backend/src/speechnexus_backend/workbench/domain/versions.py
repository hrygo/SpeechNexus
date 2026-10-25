"""Immutable workbench content; transport DTOs and persistence live outside this module."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import Enum
from typing import Literal

type ProjectStatus = Literal["active", "archived"]
type RevisionStatus = Literal["draft", "sealed"]


class InvalidVersionChangeError(ValueError):
    """An edit would violate a workbench content invariant."""


class RevisionConflictError(Exception):
    """The caller edited a version that is no longer current."""

    def __init__(self, expected: int, actual: int) -> None:
        super().__init__("workbench revision conflict")
        self.expected = expected
        self.actual = actual


class Unset(Enum):
    """Distinguish an omitted patch field from an explicit null."""

    VALUE = "unset"


@dataclass(frozen=True, slots=True)
class ProjectChanges:
    """Changes to project metadata, independent of a public PATCH DTO."""

    name: str | Unset = field(default=Unset.VALUE, repr=False)
    description: str | Unset | None = field(default=Unset.VALUE, repr=False)
    status: ProjectStatus | Unset = Unset.VALUE


def validate_revision_number(value: int) -> None:
    """Validate a typed domain revision; transport parsing remains an outer concern."""
    if type(value) is not int or not 1 <= value <= 10**19 - 1:
        raise InvalidVersionChangeError("revision outside the supported range")


def _text(value: str, *, minimum: int, maximum: int) -> None:
    if not minimum <= len(value) <= maximum:
        raise InvalidVersionChangeError("content length outside the supported range")
    # Keep accepted content lossless across UTF-8 transport and text storage.
    if "\x00" in value:
        raise InvalidVersionChangeError("content contains an unsupported character")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise InvalidVersionChangeError("content contains an unsupported character") from None


def _time(now: datetime, previous: datetime | None = None) -> None:
    if now.utcoffset() is None or (previous is not None and now < previous):
        raise InvalidVersionChangeError("version time must be aware and monotonic")


@dataclass(frozen=True, slots=True)
class Project:
    """Shared project metadata with an optimistic concurrency version."""

    id: str
    workspace_id: str
    name: str = field(repr=False)
    description: str | None = field(repr=False)
    status: ProjectStatus
    metadata_revision: int
    created_at: datetime
    updated_at: datetime

    @classmethod
    def create(
        cls,
        *,
        project_id: str,
        workspace_id: str,
        name: str,
        description: str | None = None,
        now: datetime,
    ) -> Project:
        _text(name, minimum=1, maximum=128)
        if description is not None:
            _text(description, minimum=0, maximum=2000)
        _time(now)
        return cls(project_id, workspace_id, name, description, "active", 1, now, now)

    def edit(
        self,
        *,
        expected_revision: int,
        changes: ProjectChanges,
        now: datetime,
    ) -> Project:
        if expected_revision != self.metadata_revision:
            raise RevisionConflictError(expected_revision, self.metadata_revision)
        if self.metadata_revision >= 10**19 - 1:
            raise InvalidVersionChangeError("project revision range exhausted")
        if changes == ProjectChanges():
            raise InvalidVersionChangeError("at least one metadata change is required")
        name = self.name if isinstance(changes.name, Unset) else changes.name
        description = (
            self.description if isinstance(changes.description, Unset) else changes.description
        )
        status = self.status if isinstance(changes.status, Unset) else changes.status
        _text(name, minimum=1, maximum=128)
        if description is not None:
            _text(description, minimum=0, maximum=2000)
        _time(now, self.updated_at)
        return replace(
            self,
            name=name,
            description=description,
            status=status,
            metadata_revision=self.metadata_revision + 1,
            updated_at=now,
        )


@dataclass(frozen=True, slots=True)
class SpeechSource:
    """A paired Artifact/receipt reference, verified by the public-protocol adapter."""

    artifact_id: str
    receipt_id: str


@dataclass(frozen=True, slots=True)
class TranscriptRevision:
    """An immutable snapshot; sealing never authorizes overwriting its text."""

    id: str
    transcript_id: str
    number: int
    status: RevisionStatus
    text: str = field(repr=False)
    source_revision: int | None
    source: SpeechSource | None
    created_at: datetime


@dataclass(frozen=True, slots=True)
class Transcript:
    """Transcript identity and current head, without loading its entire history."""

    id: str
    project_id: str
    display_name: str = field(repr=False)
    created_at: datetime
    head: TranscriptRevision

    @classmethod
    def create(
        cls,
        *,
        transcript_id: str,
        project_id: str,
        display_name: str,
        revision_id: str,
        text: str,
        now: datetime,
        source: SpeechSource | None = None,
    ) -> Transcript:
        _text(display_name, minimum=1, maximum=128)
        _text(text, minimum=1 if source is None else 0, maximum=1000000)
        _time(now)
        head = TranscriptRevision(
            revision_id,
            transcript_id,
            1,
            "draft" if source is None else "sealed",
            text,
            None,
            source,
            now,
        )
        return cls(transcript_id, project_id, display_name, now, head)

    def revise(
        self,
        *,
        expected_revision: int,
        revision_id: str,
        text: str,
        now: datetime,
        source_revision: TranscriptRevision | None = None,
    ) -> Transcript:
        if expected_revision != self.head.number:
            raise RevisionConflictError(expected_revision, self.head.number)
        # Public Revision uses at most 19 decimal digits; never wrap or publish an invalid value.
        if self.head.number >= 10**19 - 1:
            raise InvalidVersionChangeError("transcript revision range exhausted")
        source = self.head if source_revision is None else source_revision
        if (
            source.transcript_id != self.id
            or not 1 <= source.number <= self.head.number
            or source.source != self.head.source
        ):
            raise InvalidVersionChangeError(
                "source revision does not belong to this transcript history"
            )
        _text(text, minimum=0, maximum=1000000)
        _time(now, self.head.created_at)
        head = TranscriptRevision(
            revision_id,
            self.id,
            self.head.number + 1,
            "draft",
            text,
            source.number,
            self.head.source,
            now,
        )
        return replace(self, head=head)

    def seal(
        self,
        *,
        expected_revision: int,
        revision_id: str,
        now: datetime,
        text: str | Unset = Unset.VALUE,
    ) -> Transcript:
        """Append a fixed source without mutating the draft or earlier speech original."""
        validate_revision_number(expected_revision)
        if expected_revision != self.head.number:
            raise RevisionConflictError(expected_revision, self.head.number)
        if self.head.status != "draft":
            raise InvalidVersionChangeError("transcript revision is already sealed")
        appended = self.revise(
            expected_revision=expected_revision,
            revision_id=revision_id,
            text=self.head.text if isinstance(text, Unset) else text,
            now=now,
            source_revision=self.head,
        )
        return replace(appended, head=replace(appended.head, status="sealed"))
