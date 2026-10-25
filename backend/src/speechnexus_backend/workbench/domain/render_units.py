"""Immutable render intent; a ready source is neither a saved copy nor export qualification."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime
from typing import Literal, Self

from speechnexus_backend.workbench.domain.versions import (
    RevisionConflictError,
    validate_revision_number,
)

type RenderStatus = Literal["pending", "generating", "ready", "stale", "failed"]


class RenderAttemptConflictError(Exception):
    """An attempt or its result does not belong to the current render intent."""

    def __init__(self) -> None:
        super().__init__("workbench render attempt conflict")


def _text(value: str, maximum: int) -> None:
    if (
        type(value) is not str
        or not 1 <= len(value) <= maximum
        or not value.strip()
        or "\x00" in value
    ):
        raise ValueError("invalid render text or identifier")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise ValueError("unsupported render character") from None


def _digest(value: str) -> None:
    if type(value) is not str or re.fullmatch(r"sha256:[0-9a-f]{64}", value) is None:
        raise ValueError("invalid render dependency digest")


def _valid_status(value: object) -> bool:
    return type(value) is str and value in {"pending", "generating", "ready", "stale", "failed"}


def render_time(now: datetime, previous: datetime | None = None) -> datetime:
    """Validate time and freeze it to UTC so caller-owned tzinfo cannot mutate a snapshot."""
    if type(now) is not datetime or now.utcoffset() is None:
        raise ValueError("render time must be aware and monotonic")
    fixed = now.astimezone(UTC)
    if previous is not None and fixed < previous:
        raise ValueError("render time must be aware and monotonic")
    return fixed


def render_cas(expected: int, actual: int) -> None:
    """Validate before equality: bool and float must never satisfy an integer revision."""
    validate_revision_number(expected)
    if expected != actual:
        raise RevisionConflictError(expected, actual)


@dataclass(frozen=True, slots=True)
class RevisionPin:
    id: str
    revision: int

    def __post_init__(self) -> None:
        _text(self.id, 128)
        validate_revision_number(self.revision)


@dataclass(frozen=True, slots=True)
class RenderDependencies:
    """Per-unit pinned provenance. No global casting counter participates in this digest."""

    original: RevisionPin
    working: RevisionPin
    chapter_id: str
    role_id: str
    text_plan: RevisionPin
    voice: RevisionPin
    model: str
    model_binding: RevisionPin
    text: str = field(repr=False)
    pronunciation: RevisionPin | None = None
    profile: RevisionPin | None = None

    def __post_init__(self) -> None:
        required = (self.original, self.working, self.text_plan, self.voice, self.model_binding)
        optional = (self.pronunciation, self.profile)
        if any(type(pin) is not RevisionPin for pin in required) or any(
            pin is not None and type(pin) is not RevisionPin for pin in optional
        ):
            raise ValueError("render dependencies require immutable revision pins")
        for identifier in (self.chapter_id, self.role_id, self.model):
            _text(identifier, 128)
        _text(self.text, 200000)

    @property
    def sha256(self) -> str:
        # Private domain format, not a public RFC8785/wire digest. No normalization or trimming.
        encoded = json.dumps(
            asdict(self), sort_keys=True, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
        return "sha256:" + hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True)
class RenderGeneration:
    """An explicit dispatch identity, retained after edits to prevent accidental reuse."""

    id: str = field(repr=False)
    job_id: str
    dependencies_sha256: str
    revision: int

    def __post_init__(self) -> None:
        _text(self.id, 128)
        _text(self.job_id, 128)
        _digest(self.dependencies_sha256)
        validate_revision_number(self.revision)


@dataclass(frozen=True, slots=True)
class RenderSource:
    """A producer's fixed result references, not proof of bytes, save, receipt or quality checks."""

    unit_id: str
    job_id: str
    artifact_id: str
    receipt_id: str
    dependencies_sha256: str
    # The workbench's own retained copy of this exact producer result, when one exists.
    saved_asset_id: str | None = None

    def __post_init__(self) -> None:
        for identifier in (self.unit_id, self.job_id, self.artifact_id, self.receipt_id):
            _text(identifier, 128)
        if self.saved_asset_id is not None:
            _text(self.saved_asset_id, 128)
        _digest(self.dependencies_sha256)


@dataclass(frozen=True, slots=True)
class RenderUnit:
    id: str
    book_id: str
    ordinal: int
    revision: int
    dependencies: RenderDependencies
    status: RenderStatus
    updated_at: datetime
    generations: tuple[RenderGeneration, ...] = ()
    source: RenderSource | None = None

    def __post_init__(self) -> None:
        _text(self.id, 128)
        _text(self.book_id, 128)
        validate_revision_number(self.revision)
        object.__setattr__(self, "updated_at", render_time(self.updated_at))
        if (
            type(self.ordinal) is not int
            or self.ordinal < 0
            or type(self.dependencies) is not RenderDependencies
            or not _valid_status(self.status)
            or type(self.generations) is not tuple
            or any(type(generation) is not RenderGeneration for generation in self.generations)
            or (self.source is not None and type(self.source) is not RenderSource)
        ):
            raise ValueError("invalid render unit state")
        ids: set[str] = set()
        jobs: set[str] = set()
        last_revision = 0
        for generation in self.generations:
            if (
                generation.id in ids
                or generation.job_id in jobs
                or not last_revision < generation.revision <= self.revision
            ):
                raise ValueError("invalid render generation history")
            ids.add(generation.id)
            jobs.add(generation.job_id)
            last_revision = generation.revision
        if (
            (self.status == "pending" and bool(self.generations))
            or (self.status == "ready") != (self.source is not None)
            or (
                self.status in {"generating", "ready", "failed"}
                and (
                    self.generation is None
                    or self.generation.dependencies_sha256 != self.dependencies.sha256
                )
            )
            or (self.source is not None and not self._source_matches(self.source))
        ):
            raise ValueError("render state does not match pinned attempt or result")

    @property
    def chapter_id(self) -> str:
        return self.dependencies.chapter_id

    @property
    def role_id(self) -> str:
        return self.dependencies.role_id

    @property
    def generation(self) -> RenderGeneration | None:
        if self.status in {"generating", "ready", "failed"} and self.generations:
            return self.generations[-1]
        return None

    @classmethod
    def create(
        cls,
        *,
        unit_id: str,
        book_id: str,
        ordinal: int,
        dependencies: RenderDependencies,
        now: datetime,
    ) -> Self:
        return cls(unit_id, book_id, ordinal, 1, dependencies, "pending", now)

    def _source_matches(self, source: RenderSource) -> bool:
        return (
            self.generation is not None
            and source.unit_id == self.id
            and source.job_id == self.generation.job_id
            and source.dependencies_sha256 == self.dependencies.sha256
        )

    def start(
        self,
        *,
        expected_revision: int,
        job_id: str,
        generation_id: str,
        now: datetime,
    ) -> Self:
        render_cas(expected_revision, self.revision)
        fixed_time = render_time(now, self.updated_at)
        generation = RenderGeneration(
            generation_id, job_id, self.dependencies.sha256, self.revision + 1
        )
        if self.status == "generating" or any(
            prior.id == generation_id or prior.job_id == job_id for prior in self.generations
        ):
            raise RenderAttemptConflictError
        return replace(
            self,
            revision=self.revision + 1,
            status="generating",
            updated_at=fixed_time,
            generations=(*self.generations, generation),
            source=None,
        )

    def revise(
        self,
        *,
        expected_revision: int,
        dependencies: RenderDependencies,
        now: datetime,
    ) -> Self:
        render_cas(expected_revision, self.revision)
        fixed_time = render_time(now, self.updated_at)
        if type(dependencies) is not RenderDependencies:
            raise ValueError("render dependencies require an immutable snapshot")
        if dependencies == self.dependencies:
            return self
        return replace(
            self,
            revision=self.revision + 1,
            dependencies=dependencies,
            status="stale",
            updated_at=fixed_time,
            source=None,
        )

    def _current_attempt(self, generation_id: str, now: datetime) -> datetime:
        fixed_time = render_time(now, self.updated_at)
        _text(generation_id, 128)
        if (
            self.status != "generating"
            or self.generation is None
            or self.generation.id != generation_id
        ):
            raise RenderAttemptConflictError
        return fixed_time

    def complete(self, *, generation_id: str, source: RenderSource, now: datetime) -> Self:
        fixed_time = self._current_attempt(generation_id, now)
        if type(source) is not RenderSource:
            raise ValueError("render result requires fixed source references")
        if not self._source_matches(source):
            raise RenderAttemptConflictError
        return replace(
            self, revision=self.revision + 1, status="ready", updated_at=fixed_time, source=source
        )

    def fail(self, *, generation_id: str, now: datetime) -> Self:
        fixed_time = self._current_attempt(generation_id, now)
        return replace(self, revision=self.revision + 1, status="failed", updated_at=fixed_time)


@dataclass(frozen=True, slots=True)
class RenderUnitSummary:
    """Listing position facts for a render unit, carrying the unit as its payload.

    A render unit has no creation order of its own on the wire, so the listing
    position is kept beside the unit instead of being exposed as content.
    """

    id: str
    created_at: datetime
    unit: RenderUnit
