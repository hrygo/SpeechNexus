"""Immutable workbench task state; request completion is not physical stop evidence."""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Literal, Self

from speechnexus_backend.workbench.domain.versions import validate_revision_number

type MinutesTaskStatus = Literal["queued", "running", "succeeded", "failed", "cancelled", "expired"]
type MinutesTaskFailure = Literal["unavailable", "invalid_response", "interrupted"]


class MinutesTaskConflictError(Exception):
    def __init__(self) -> None:
        super().__init__("workbench minutes task state conflict")


def _text(value: str, maximum: int) -> bool:
    if not isinstance(value, str) or not 1 <= len(value) <= maximum or "\x00" in value:
        return False
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


@dataclass(frozen=True, slots=True)
class MinutesTask:
    id: str
    workspace_id: str
    project_id: str
    transcript_id: str
    source_revision: int
    source_revision_id: str
    source_sha256: str
    profile_sha256: str
    binding_id: str
    status: MinutesTaskStatus
    attempt: int
    attempt_token: str | None = field(repr=False)
    version: int
    created_at: datetime
    updated_at: datetime
    text: str | None = field(default=None, repr=False)
    error: MinutesTaskFailure | None = None

    def __post_init__(self) -> None:
        validate_revision_number(self.source_revision)
        validate_revision_number(self.version)
        if (
            any(
                not _text(value, 128)
                for value in (
                    self.id,
                    self.workspace_id,
                    self.project_id,
                    self.transcript_id,
                    self.source_revision_id,
                )
            )
            or any(
                not re.fullmatch(r"sha256:[0-9a-f]{64}", value)
                for value in (
                    self.source_sha256,
                    self.profile_sha256,
                    self.binding_id,
                )
            )
            or self.created_at.utcoffset() is None
            or self.updated_at.utcoffset() is None
            or self.updated_at < self.created_at
            or type(self.attempt) is not int
            or not 0 <= self.attempt < 2**31
            or self.status
            not in {"queued", "running", "succeeded", "failed", "cancelled", "expired"}
            or (self.status == "running" and (self.attempt == 0 or self.attempt_token is None))
            or (self.attempt_token is not None and not _text(self.attempt_token, 128))
            or (self.status == "succeeded") != (self.text is not None)
            or (
                self.text is not None and (not _text(self.text, 1_000_000) or not self.text.strip())
            )
            or (self.status == "failed") != (self.error is not None)
            or self.error not in {None, "unavailable", "invalid_response", "interrupted"}
        ):
            raise ValueError("invalid minutes task state")

    @classmethod
    def create(
        cls,
        *,
        task_id: str,
        workspace_id: str,
        project_id: str,
        transcript_id: str,
        source_revision: int,
        source_revision_id: str,
        source_sha256: str,
        profile_sha256: str,
        binding_id: str,
        now: datetime,
    ) -> Self:
        return cls(
            task_id,
            workspace_id,
            project_id,
            transcript_id,
            source_revision,
            source_revision_id,
            source_sha256,
            profile_sha256,
            binding_id,
            "queued",
            0,
            None,
            1,
            now,
            now,
        )

    def _advance(self, *, now: datetime, **changes: object) -> Self:
        if now.utcoffset() is None or now < self.updated_at:
            raise ValueError("invalid minutes task time")
        return replace(self, version=self.version + 1, updated_at=now, **changes)  # type: ignore[arg-type]

    def claim(self, *, token: str, now: datetime) -> Self:
        if self.status != "queued":
            raise MinutesTaskConflictError
        return self._advance(
            now=now, status="running", attempt=self.attempt + 1, attempt_token=token
        )

    def finish(
        self,
        *,
        token: str,
        text: str | None,
        error: MinutesTaskFailure | None,
        now: datetime,
    ) -> Self:
        if token != self.attempt_token:
            raise MinutesTaskConflictError
        if self.status in {"cancelled", "expired"}:
            return self
        if self.status != "running":
            raise MinutesTaskConflictError
        if (text is None) == (error is None):
            raise ValueError("minutes completion needs exactly one outcome")
        return self._advance(
            now=now, status="failed" if error is not None else "succeeded", text=text, error=error
        )

    def cancel(self, *, now: datetime) -> Self:
        if self.status not in {"queued", "running"}:
            return self
        return self._advance(now=now, status="cancelled", text=None, error=None)

    def expire(self, *, now: datetime) -> Self:
        if self.status in {"cancelled", "expired"}:
            return self
        return self._advance(now=now, status="expired", text=None, error=None)

    def retry(self, *, expected_attempt: int, now: datetime) -> Self:
        if (
            self.status != "failed"
            or type(expected_attempt) is not int
            or self.attempt != expected_attempt
        ):
            raise MinutesTaskConflictError
        return self._advance(now=now, status="queued", attempt_token=None, text=None, error=None)
