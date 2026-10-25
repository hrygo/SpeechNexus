"""Assistant turn state: a turn pins its input and voice, then advances once.

Nothing here knows about a provider, endpoint or transport. A turn that has not been
answered carries no output, and only a failed turn carries an error.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Literal, Self

from speechnexus_backend.workbench.domain.versions import (
    RevisionConflictError,
    validate_revision_number,
)

AssistantStatus = Literal["queued", "running", "succeeded", "failed", "cancelled"]

_STATUSES = frozenset({"queued", "running", "succeeded", "failed", "cancelled"})
_TERMINAL = frozenset({"succeeded", "failed", "cancelled"})
MAX_INPUT = 20_000


class AssistantStateConflictError(Exception):
    """The turn is not in a state that accepts this transition."""


def _valid_status(value: object) -> bool:
    return type(value) is str and value in _STATUSES


def _text(value: object, maximum: int, field: str) -> str:
    if (
        type(value) is not str
        or not 1 <= len(value) <= maximum
        or not value.strip()
        or "\x00" in value
    ):
        raise ValueError(f"invalid assistant {field}")
    return value


def _identifier(value: object, field: str) -> str:
    return _text(value, 128, field)


def _optional_identifier(value: object, field: str) -> str | None:
    if value is None:
        return None
    return _identifier(value, field)


def assistant_time(now: datetime, previous: datetime | None = None) -> datetime:
    """Freeze caller-owned time to UTC so a stored snapshot cannot drift."""
    if type(now) is not datetime or now.utcoffset() is None:
        raise ValueError("assistant time must be aware and monotonic")
    fixed = now.astimezone(UTC)
    if previous is not None and fixed < previous:
        raise ValueError("assistant time must be aware and monotonic")
    return fixed


def assistant_cas(expected: int, actual: int) -> None:
    """Validate before equality: bool and float must never satisfy an integer revision."""
    validate_revision_number(expected)
    if expected != actual:
        raise RevisionConflictError(expected, actual)


@dataclass(frozen=True, slots=True)
class ResourcePin:
    id: str
    revision: int

    def __post_init__(self) -> None:
        _identifier(self.id, "resource")
        validate_revision_number(self.revision)


@dataclass(frozen=True, slots=True)
class AssistantError:
    """A stable, provider-free failure description."""

    code: str
    message: str
    retryable: bool

    def __post_init__(self) -> None:
        if type(self.code) is not str or re.fullmatch(r"[a-z][a-z0-9_]{2,63}", self.code) is None:
            raise ValueError("invalid assistant error code")
        _text(self.message, 2_000, "error message")
        if type(self.retryable) is not bool:
            raise ValueError("invalid assistant error retryable")


@dataclass(frozen=True, slots=True)
class AssistantTurn:
    id: str
    project_id: str
    status: AssistantStatus
    revision: int
    input_text: str
    voice: ResourcePin
    playback_generation: int
    created_at: datetime
    updated_at: datetime
    model: str | None = None
    profile: ResourcePin | None = None
    speech_job_id: str | None = None
    output_text: str | None = None
    error: AssistantError | None = None

    def __post_init__(self) -> None:
        _identifier(self.id, "turn")
        _identifier(self.project_id, "project")
        if not _valid_status(self.status):
            raise ValueError("invalid assistant status")
        validate_revision_number(self.revision)
        validate_revision_number(self.playback_generation)
        _text(self.input_text, MAX_INPUT, "input text")
        if type(self.voice) is not ResourcePin:
            raise ValueError("invalid assistant voice reference")
        _optional_identifier(self.model, "model")
        _optional_identifier(self.speech_job_id, "speech job")
        if self.output_text is not None:
            _text(self.output_text, MAX_INPUT, "output text")
        if (self.status == "failed") != (self.error is not None):
            raise ValueError("assistant failure state does not match its error")
        if self.status == "succeeded" and (self.output_text is None or self.speech_job_id is None):
            # The wire shape requires both for a succeeded turn; refuse earlier and louder.
            raise ValueError("succeeded assistant turn requires an answer and a speech job")
        if self.status == "queued" and self.output_text is not None:
            raise ValueError("an unanswered turn cannot carry output")
        for value in (self.created_at, self.updated_at):
            if type(value) is not datetime or value.utcoffset() is None:
                raise ValueError("assistant time must be aware and monotonic")
        if self.updated_at < self.created_at:
            raise ValueError("assistant time must be aware and monotonic")

    @classmethod
    def create(
        cls,
        *,
        turn_id: str,
        project_id: str,
        input_text: str,
        voice: ResourcePin,
        now: datetime,
        model: str | None = None,
        profile: ResourcePin | None = None,
    ) -> Self:
        fixed = assistant_time(now)
        return cls(
            id=turn_id,
            project_id=project_id,
            status="queued",
            revision=1,
            input_text=input_text,
            voice=voice,
            playback_generation=1,
            created_at=fixed,
            updated_at=fixed,
            model=model,
            profile=profile,
        )

    def _advance(self, expected_revision: int, now: datetime) -> tuple[datetime, int]:
        assistant_cas(expected_revision, self.revision)
        fixed = assistant_time(now, self.updated_at)
        return fixed, self.revision + 1

    def start(self, *, expected_revision: int, speech_job_id: str, now: datetime) -> Self:
        fixed, revision = self._advance(expected_revision, now)
        if self.status != "queued":
            raise AssistantStateConflictError
        return replace(
            self,
            status="running",
            revision=revision,
            updated_at=fixed,
            speech_job_id=_identifier(speech_job_id, "speech job"),
        )

    def complete(self, *, expected_revision: int, output_text: str, now: datetime) -> Self:
        fixed, revision = self._advance(expected_revision, now)
        if self.status != "running":
            raise AssistantStateConflictError
        return replace(
            self,
            status="succeeded",
            revision=revision,
            updated_at=fixed,
            output_text=_text(output_text, MAX_INPUT, "output text"),
            error=None,
        )

    def fail(self, *, expected_revision: int, error: AssistantError, now: datetime) -> Self:
        fixed, revision = self._advance(expected_revision, now)
        if self.status not in {"queued", "running"}:
            raise AssistantStateConflictError
        if type(error) is not AssistantError:
            raise ValueError("invalid assistant error")
        return replace(self, status="failed", revision=revision, updated_at=fixed, error=error)

    def cancel(self, *, expected_revision: int, now: datetime) -> Self:
        fixed, revision = self._advance(expected_revision, now)
        if self.status in _TERMINAL:
            raise AssistantStateConflictError
        return replace(self, status="cancelled", revision=revision, updated_at=fixed)
