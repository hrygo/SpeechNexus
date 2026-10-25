"""Render-unit commands over a narrow store port.

The port carries only what the use cases need; the store decides transaction scope,
idempotency and how a chapter resolves to its owning project.
"""

from __future__ import annotations

from typing import Protocol

from speechnexus_backend.workbench.application.listings import (
    ListingPage,
    ListingPosition,
    listing_page,
    validate_listing,
)
from speechnexus_backend.workbench.application.versions import ResourceNotFoundError
from speechnexus_backend.workbench.domain.render_units import RenderUnit, RenderUnitSummary
from speechnexus_backend.workbench.domain.versions import InvalidVersionChangeError


def _key(key: str | None) -> None:
    if key is None:
        return
    if not 8 <= len(key) <= 128:
        raise InvalidVersionChangeError("invalid workbench request key")


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


class WorkbenchRenderUnits:
    """A revise is refused before any transaction opens when nothing changes."""

    def __init__(self, units: RenderUnitStore) -> None:
        self._units = units

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
