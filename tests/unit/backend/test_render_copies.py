"""A settled result is copied under the unit's project with a stable replay key."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast

from speechnexus_backend.workbench.application.books import WorkbenchBooks
from speechnexus_backend.workbench.application.render_copies import WorkbenchRenderResultCopies
from speechnexus_backend.workbench.application.saved_assets import WorkbenchSavedAssets
from speechnexus_backend.workbench.domain.render_units import (
    RenderDependencies,
    RenderSource,
    RenderUnit,
    RevisionPin,
)

NOW = datetime(2026, 10, 24, tzinfo=UTC)


def unit() -> RenderUnit:
    dependencies = RenderDependencies(
        original=RevisionPin("original", 1),
        working=RevisionPin("working", 2),
        chapter_id="chapter",
        role_id="narrator",
        text_plan=RevisionPin("plan", 1),
        voice=RevisionPin("voice", 3),
        model="tts-quality",
        model_binding=RevisionPin("binding", 4),
        text="合成旁白单元",
    )
    return RenderUnit.create(
        unit_id="unit", book_id="book", ordinal=0, dependencies=dependencies, now=NOW
    )


class Books:
    def __init__(self) -> None:
        self.seen: list[dict[str, str]] = []

    async def get_book(self, *, book_id: str, workspace_id: str) -> Any:
        self.seen.append({"book_id": book_id, "workspace_id": workspace_id})
        return type("Book", (), {"project_id": "project"})()


class Assets:
    def __init__(self) -> None:
        self.saved: list[dict[str, Any]] = []

    async def save(self, **arguments: Any) -> Any:
        self.saved.append(arguments)
        return type("Asset", (), {"id": "asset_1"})()


async def test_copy_is_saved_under_the_book_project_with_a_replay_key() -> None:
    books, assets = Books(), Assets()
    copies = WorkbenchRenderResultCopies(
        cast(WorkbenchBooks, books), cast(WorkbenchSavedAssets, assets)
    )
    source = RenderSource("unit", "job_1", "artifact_1", "receipt_1", "sha256:" + "a" * 64)
    assert await copies.save_copy(workspace_id="workspace", unit=unit(), source=source) == "asset_1"
    assert books.seen == [{"book_id": "book", "workspace_id": "workspace"}]
    (saved,) = assets.saved
    assert saved["project_id"] == "project"
    assert saved["workspace_id"] == "workspace"
    assert (saved["source_artifact_id"], saved["source_receipt_id"]) == (
        "artifact_1",
        "receipt_1",
    )
    key = saved["key"]
    assert key.startswith("render:") and 8 <= len(key) <= 128
    await copies.save_copy(workspace_id="workspace", unit=unit(), source=source)
    assert assets.saved[1]["key"] == key
