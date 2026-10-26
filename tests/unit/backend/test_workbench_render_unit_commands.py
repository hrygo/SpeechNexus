"""Render-unit commands refuse bad input before any transaction opens."""

from __future__ import annotations

from typing import Any, cast

import pytest

from speechnexus_backend.workbench.application.listings import InvalidListingError
from speechnexus_backend.workbench.application.render_units import WorkbenchRenderUnits
from speechnexus_backend.workbench.application.versions import ResourceNotFoundError
from speechnexus_backend.workbench.domain.render_units import RenderUnit
from speechnexus_backend.workbench.domain.versions import InvalidVersionChangeError


class _Store:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.listing: tuple[Any, ...] | None = ()

    async def list_units(self, **kw: Any) -> tuple[Any, ...] | None:
        self.calls.append(("list", dict(kw)))
        return self.listing

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
        self.calls.append(("create", {"key": key, "chapter_id": chapter_id, "ordinal": ordinal}))
        return cast(RenderUnit, None)

    async def get_unit(self, *, unit_id: str, workspace_id: str) -> RenderUnit:
        self.calls.append(("get", {"unit_id": unit_id}))
        return cast(RenderUnit, None)

    async def start_unit(self, **kw: Any) -> RenderUnit:
        self.calls.append(("start", dict(kw)))
        return cast(RenderUnit, None)

    async def complete_unit(self, **kw: Any) -> RenderUnit:
        return cast(RenderUnit, None)

    async def fail_unit(self, **kw: Any) -> RenderUnit:
        return cast(RenderUnit, None)

    async def scan_generating(self, *, workspace_id: str, limit: int) -> tuple[str, ...]:
        return ()

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
        self.calls.append(
            ("update", {"expected_revision": expected_revision, "text": text, "model": model})
        )
        return cast(RenderUnit, None)


async def _create(service: WorkbenchRenderUnits, key: str | None) -> None:
    await service.create_unit(
        workspace_id="workspace-render",
        chapter_id="chapter_demo",
        ordinal=0,
        text="合成旁白单元",
        voice_id="voice_demo",
        voice_revision=1,
        model="tts-quality",
        model_binding_id="binding_demo",
        model_binding_revision=3,
        key=key,
    )


async def test_keys_outside_the_accepted_length_never_reach_the_store() -> None:
    store = _Store()
    service = WorkbenchRenderUnits(store)
    for key in ("short", "x" * 129):
        with pytest.raises(InvalidVersionChangeError, match="invalid workbench request key"):
            await _create(service, key)
    assert store.calls == []

    for key in ("x" * 8, "x" * 128):
        await _create(service, key)
    assert [call[1]["key"] for call in store.calls] == ["x" * 8, "x" * 128]


async def test_an_empty_patch_is_refused_and_a_real_one_is_forwarded() -> None:
    store = _Store()
    service = WorkbenchRenderUnits(store)
    with pytest.raises(InvalidVersionChangeError, match="empty workbench render update"):
        await service.update_unit(
            unit_id="unit_demo", workspace_id="workspace-render", expected_revision=1
        )
    assert store.calls == []

    await service.update_unit(
        unit_id="unit_demo",
        workspace_id="workspace-render",
        expected_revision=2,
        text="改写后的旁白单元",
    )
    assert store.calls == [
        ("update", {"expected_revision": 2, "text": "改写后的旁白单元", "model": None})
    ]


async def test_list_units_guards_its_window_and_probes_one_extra_row() -> None:
    store = _Store()
    service = WorkbenchRenderUnits(store)
    with pytest.raises(InvalidListingError):
        await service.list_units(chapter_id="chapter", workspace_id="workspace-render", limit=101)
    # The window is rejected before any transaction opens.
    assert store.calls == []

    store.listing = None
    with pytest.raises(ResourceNotFoundError):
        await service.list_units(chapter_id="chapter", workspace_id="workspace-render")
    assert store.calls[-1][1]["limit"] == 51
    assert store.calls[-1][1]["after"] is None
