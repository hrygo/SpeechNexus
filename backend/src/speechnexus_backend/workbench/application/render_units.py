"""Render-unit commands over a narrow store port.

The port carries only what the use cases need; the store decides transaction scope,
idempotency and how a chapter resolves to its owning project.
"""

from __future__ import annotations

import hashlib
from typing import Protocol
from uuid import uuid4

from speechnexus_backend.workbench.application.listings import (
    ListingPage,
    ListingPosition,
    listing_page,
    validate_listing,
)
from speechnexus_backend.workbench.application.versions import ResourceNotFoundError
from speechnexus_backend.workbench.domain.render_units import (
    RenderAttemptConflictError,
    RenderSource,
    RenderUnit,
    RenderUnitSummary,
)
from speechnexus_backend.workbench.domain.versions import (
    InvalidVersionChangeError,
    RevisionConflictError,
)


def _key(key: str | None) -> None:
    if key is None:
        return
    if not 8 <= len(key) <= 128:
        raise InvalidVersionChangeError("invalid workbench request key")


class RenderDispatchUnavailableError(Exception):
    """No public speech job producer is configured; the attempt never starts."""


class RenderSubmissionUnknownError(Exception):
    """The Job submission may have succeeded; only the original key can recover it."""


class RenderSubmissionConflictError(Exception):
    """The same key names a different submission intent."""


class RenderSubmissionRejectedError(Exception):
    """The pinned unit cannot be expressed as a valid public speech Job."""


class RenderJobPort(Protocol):
    """Public speech job submission; the port returns the durable job identity."""

    async def submit(self, *, unit: RenderUnit, key: str | None, reference: str) -> str: ...


class RenderUnitStore(Protocol):
    async def create_unit(
        self,
        *,
        workspace_id: str,
        chapter_id: str,
        ordinal: int,
        text: str,
        voice_id: str,
        voice_revision: int,
        model: str,
        model_binding_id: str,
        model_binding_revision: int,
        key: str | None,
    ) -> RenderUnit: ...

    async def get_unit(self, *, unit_id: str, workspace_id: str) -> RenderUnit: ...

    async def list_units(
        self, *, chapter_id: str, workspace_id: str, limit: int, after: ListingPosition | None
    ) -> tuple[RenderUnitSummary, ...] | None:
        """None for an absent chapter; chapter visibility and rows share one view."""
        ...

    async def update_unit(
        self,
        *,
        unit_id: str,
        workspace_id: str,
        expected_revision: int,
        text: str | None = None,
        voice_id: str | None = None,
        voice_revision: int | None = None,
        model: str | None = None,
        model_binding_id: str | None = None,
        model_binding_revision: int | None = None,
    ) -> RenderUnit: ...

    async def start_unit(
        self,
        *,
        unit_id: str,
        workspace_id: str,
        expected_revision: int,
        job_id: str,
        generation_id: str,
    ) -> RenderUnit: ...

    async def complete_unit(
        self,
        *,
        unit_id: str,
        workspace_id: str,
        generation_id: str,
        source: RenderSource,
    ) -> RenderUnit: ...

    async def fail_unit(
        self, *, unit_id: str, workspace_id: str, generation_id: str
    ) -> RenderUnit: ...

    async def scan_generating(self, *, workspace_id: str, limit: int) -> tuple[str, ...]: ...

    async def scan_ready_without_copy(
        self, *, workspace_id: str, limit: int
    ) -> tuple[str, ...]: ...


class WorkbenchRenderUnits:
    """A revise is refused before any transaction opens when nothing changes."""

    async def unsaved_units(self, *, workspace_id: str, limit: int = 20) -> tuple[RenderUnit, ...]:
        ids = await self._units.scan_ready_without_copy(workspace_id=workspace_id, limit=limit)
        units: list[RenderUnit] = []
        for unit_id in ids:
            try:
                units.append(await self._units.get_unit(unit_id=unit_id, workspace_id=workspace_id))
            except ResourceNotFoundError:
                continue
        return tuple(units)

    def __init__(self, units: RenderUnitStore, jobs: RenderJobPort | None = None) -> None:
        self._units = units
        self._jobs = jobs

    async def start_unit(
        self,
        *,
        unit_id: str,
        workspace_id: str,
        expected_revision: int,
        key: str | None = None,
    ) -> tuple[RenderUnit, str]:
        """Submit one pinned attempt and record it; a replayed key returns that attempt."""
        _key(key)
        if self._jobs is None:
            raise RenderDispatchUnavailableError
        unit = await self._units.get_unit(unit_id=unit_id, workspace_id=workspace_id)
        reference = (
            f"gen:{uuid4().hex}"
            if key is None
            else "gen:" + hashlib.sha256(f"{unit.id}\n{key}".encode()).hexdigest()[:48]
        )
        # A recorded attempt is the original result. Replaying its key returns that
        # attempt even though starting it advanced the unit revision.
        recorded = next(
            (generation for generation in unit.generations if generation.id == reference), None
        )
        if recorded is not None:
            return unit, recorded.job_id
        if unit.revision != expected_revision:
            raise RevisionConflictError(expected_revision, unit.revision)
        job_id = await self._jobs.submit(unit=unit, key=key, reference=reference)
        try:
            started = await self._units.start_unit(
                unit_id=unit_id,
                workspace_id=workspace_id,
                expected_revision=expected_revision,
                job_id=job_id,
                generation_id=reference,
            )
        except RenderAttemptConflictError:
            current = await self._units.get_unit(unit_id=unit_id, workspace_id=workspace_id)
            if any(
                generation.id == reference and generation.job_id == job_id
                for generation in current.generations
            ):
                return current, job_id
            raise
        return started, job_id

    async def complete_unit(
        self,
        *,
        unit_id: str,
        workspace_id: str,
        generation_id: str,
        source: RenderSource,
    ) -> RenderUnit:
        return await self._units.complete_unit(
            unit_id=unit_id,
            workspace_id=workspace_id,
            generation_id=generation_id,
            source=source,
        )

    async def fail_unit(self, *, unit_id: str, workspace_id: str, generation_id: str) -> RenderUnit:
        return await self._units.fail_unit(
            unit_id=unit_id, workspace_id=workspace_id, generation_id=generation_id
        )

    async def generating_units(
        self, *, workspace_id: str, limit: int = 20
    ) -> tuple[RenderUnit, ...]:
        ids = await self._units.scan_generating(workspace_id=workspace_id, limit=limit)
        units: list[RenderUnit] = []
        for unit_id in ids:
            try:
                units.append(await self._units.get_unit(unit_id=unit_id, workspace_id=workspace_id))
            except ResourceNotFoundError:
                continue
        return tuple(units)

    async def create_unit(
        self,
        *,
        workspace_id: str,
        chapter_id: str,
        ordinal: int,
        text: str,
        voice_id: str,
        voice_revision: int,
        model: str,
        model_binding_id: str,
        model_binding_revision: int,
        key: str | None,
    ) -> RenderUnit:
        _key(key)
        return await self._units.create_unit(
            workspace_id=workspace_id,
            chapter_id=chapter_id,
            ordinal=ordinal,
            text=text,
            voice_id=voice_id,
            voice_revision=voice_revision,
            model=model,
            model_binding_id=model_binding_id,
            model_binding_revision=model_binding_revision,
            key=key,
        )

    async def get_unit(self, *, unit_id: str, workspace_id: str) -> RenderUnit:
        return await self._units.get_unit(unit_id=unit_id, workspace_id=workspace_id)

    async def list_units(
        self,
        *,
        chapter_id: str,
        workspace_id: str,
        limit: int = 50,
        after: ListingPosition | None = None,
    ) -> ListingPage[RenderUnitSummary]:
        validate_listing("render_units", chapter_id, limit, after)
        rows = await self._units.list_units(
            chapter_id=chapter_id, workspace_id=workspace_id, limit=limit + 1, after=after
        )
        if rows is None:
            raise ResourceNotFoundError
        return listing_page(rows, kind="render_units", parent_id=chapter_id, limit=limit)

    async def update_unit(
        self,
        *,
        unit_id: str,
        workspace_id: str,
        expected_revision: int,
        text: str | None = None,
        voice_id: str | None = None,
        voice_revision: int | None = None,
        model: str | None = None,
        model_binding_id: str | None = None,
        model_binding_revision: int | None = None,
    ) -> RenderUnit:
        if all(
            value is None
            for value in (
                text,
                voice_id,
                voice_revision,
                model,
                model_binding_id,
                model_binding_revision,
            )
        ):
            raise InvalidVersionChangeError("empty workbench render update")
        return await self._units.update_unit(
            unit_id=unit_id,
            workspace_id=workspace_id,
            expected_revision=expected_revision,
            text=text,
            voice_id=voice_id,
            voice_revision=voice_revision,
            model=model,
            model_binding_id=model_binding_id,
            model_binding_revision=model_binding_revision,
        )
