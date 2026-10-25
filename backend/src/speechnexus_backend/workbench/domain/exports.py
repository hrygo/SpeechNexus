"""Export job state machine over an immutable book casting revision.

An export names the manifest it was accepted against, so a later casting change can
never silently alter what a running or finished export produced. The wire shares the
job's own error shape, so the failure detail stays provider-free here too.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Literal, Self, cast

ExportStatus = Literal[
    "queued", "running", "cancelling", "succeeded", "failed", "cancelled", "expired"
]
ExportFormat = Literal["wav", "mp3", "zip"]

_STATUSES = frozenset(
    {"queued", "running", "cancelling", "succeeded", "failed", "cancelled", "expired"}
)
_FORMATS = frozenset({"wav", "mp3", "zip"})
_MEDIA_TYPES = {"wav": "audio/wav", "mp3": "audio/mpeg", "zip": "application/zip"}
_IN_FLIGHT = frozenset({"queued", "running"})
_FINISHED = frozenset({"succeeded", "failed", "cancelled", "expired"})
_IDENTIFIER = re.compile(r"[a-z0-9][a-z0-9._:-]{1,126}[a-z0-9]")
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
_MAX_REVISION = 9999999999999999999


class ExportStateConflictError(Exception):
    """The export is not in a state that allows the requested transition."""


def valid_identifier(value: object) -> bool:
    return type(value) is str and _IDENTIFIER.fullmatch(value) is not None


def _valid_status(value: object) -> bool:
    return type(value) is str and value in _STATUSES


def _valid_format(value: object) -> bool:
    return type(value) is str and value in _FORMATS


def _identifier(value: object, field: str) -> str:
    if not valid_identifier(value):
        raise ValueError(f"invalid {field}")
    return cast(str, value)


def _revision(value: object, field: str) -> int:
    if type(value) is not int or not 1 <= value <= _MAX_REVISION:
        raise ValueError(f"invalid {field}")
    return value


def _count(value: object, field: str, *, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise ValueError(f"invalid {field}")
    return value


def _text(value: object, maximum: int, field: str) -> str:
    if type(value) is not str or not 1 <= len(value) <= maximum:
        raise ValueError(f"invalid {field}")
    return value


def export_time(value: object, field: str) -> datetime:
    if type(value) is not datetime or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"invalid {field}")
    return value


@dataclass(frozen=True)
class ExportError:
    code: str
    message: str
    retryable: bool

    def __post_init__(self) -> None:
        if type(self.code) is not str or re.fullmatch(r"[a-z][a-z0-9_]{2,63}", self.code) is None:
            raise ValueError("invalid export error code")
        _text(self.message, 2000, "export error message")
        if type(self.retryable) is not bool:
            raise ValueError("invalid export error retryable")


@dataclass(frozen=True)
class ExportArtifact:
    """A published result pinned to the manifest revision that produced it."""

    id: str
    export_id: str
    manifest_revision: int
    media_type: str
    bytes: int
    sha256: str
    created_at: datetime

    def __post_init__(self) -> None:
        _identifier(self.id, "artifact id")
        _identifier(self.export_id, "artifact export id")
        _revision(self.manifest_revision, "artifact manifest revision")
        _text(self.media_type, 128, "artifact media type")
        _count(self.bytes, "artifact bytes", minimum=1)
        if type(self.sha256) is not str or _DIGEST.fullmatch(self.sha256) is None:
            raise ValueError("invalid artifact digest")
        export_time(self.created_at, "artifact created_at")


@dataclass(frozen=True)
class ExportJob:
    id: str
    book_id: str
    status: ExportStatus
    format: ExportFormat
    manifest_revision: int
    progress_completed: int
    progress_total: int
    created_at: datetime
    updated_at: datetime
    artifact: ExportArtifact | None = None
    error: ExportError | None = None

    def __post_init__(self) -> None:
        _identifier(self.id, "export id")
        _identifier(self.book_id, "export book id")
        if not _valid_status(self.status):
            raise ValueError("invalid export status")
        if not _valid_format(self.format):
            raise ValueError("invalid export format")
        _revision(self.manifest_revision, "manifest revision")
        _count(self.progress_completed, "export progress completed")
        _count(self.progress_total, "export progress total", minimum=1)
        if self.progress_completed > self.progress_total:
            raise ValueError("export progress exceeds its total")
        if self.status == "succeeded" and self.progress_completed != self.progress_total:
            raise ValueError("successful export requires complete progress")
        created = export_time(self.created_at, "export created_at")
        updated = export_time(self.updated_at, "export updated_at")
        if updated < created:
            raise ValueError("export updated before it was created")
        if (self.status == "failed") != (self.error is not None):
            raise ValueError("export failure state and error must agree")
        # The wire shape keeps these paired: an artifact exists exactly on success.
        if (self.status == "succeeded") != (self.artifact is not None):
            raise ValueError("export artifact exists exactly when it succeeded")
        if self.artifact is not None:
            if self.artifact.export_id != self.id:
                raise ValueError("artifact must belong to its own export")
            if self.artifact.manifest_revision != self.manifest_revision:
                raise ValueError("artifact must pin its export manifest revision")
            if self.artifact.media_type != _MEDIA_TYPES[self.format]:
                raise ValueError("artifact media type must match its export format")

    @property
    def finished(self) -> bool:
        return self.status in _FINISHED

    @property
    def in_flight(self) -> bool:
        return self.status in _IN_FLIGHT

    @classmethod
    def create(
        cls,
        *,
        export_id: str,
        book_id: str,
        export_format: str,
        manifest_revision: int,
        progress_total: int,
        now: datetime,
    ) -> Self:
        return cls(
            id=export_id,
            book_id=book_id,
            status="queued",
            format=cast(ExportFormat, export_format),
            manifest_revision=manifest_revision,
            progress_completed=0,
            progress_total=progress_total,
            created_at=export_time(now, "export created_at"),
            updated_at=export_time(now, "export updated_at"),
        )

    def _moment(self, now: datetime) -> datetime:
        moment = export_time(now, "export transition time")
        if moment < self.updated_at:
            raise ValueError("export time moved backwards")
        return moment

    def start(self, *, now: datetime) -> Self:
        if self.status != "queued":
            raise ExportStateConflictError
        return replace(self, status="running", updated_at=self._moment(now))

    def record_progress(self, *, completed: int, now: datetime) -> Self:
        if self.status != "running":
            raise ExportStateConflictError
        counted = _count(completed, "export progress completed")
        if counted > self.progress_total or counted < self.progress_completed:
            raise ValueError("export progress must not move backwards or past its total")
        return replace(self, progress_completed=counted, updated_at=self._moment(now))

    def request_cancel(self, *, now: datetime) -> Self:
        if self.status not in _IN_FLIGHT:
            raise ExportStateConflictError
        return replace(self, status="cancelling", updated_at=self._moment(now))

    def settle_cancel(self, *, now: datetime) -> Self:
        if self.status != "cancelling":
            raise ExportStateConflictError
        return replace(self, status="cancelled", updated_at=self._moment(now))

    def complete(self, *, artifact: ExportArtifact, now: datetime) -> Self:
        if self.status != "running":
            raise ExportStateConflictError
        if self.progress_completed != self.progress_total:
            raise ValueError("cannot complete an export with incomplete progress")
        return replace(
            self,
            status="succeeded",
            artifact=artifact,
            updated_at=self._moment(now),
        )

    def fail(self, *, error: ExportError, now: datetime) -> Self:
        if self.status not in _IN_FLIGHT:
            raise ExportStateConflictError
        return replace(self, status="failed", error=error, updated_at=self._moment(now))

    def expire(self, *, now: datetime) -> Self:
        if self.status != "succeeded":
            raise ExportStateConflictError
        # Retention removes the artifact reference; the manifest stays as evidence.
        return replace(self, status="expired", artifact=None, updated_at=self._moment(now))
