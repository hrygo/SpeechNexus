"""Save a settled render result as a workbench-owned copy.

The copy is pinned to the producer receipt, so replaying a settlement writes the same
object and key instead of a second copy. A missing object zone is not a settlement
failure: the unit stays ready and the operator can still save the copy explicitly.
"""

from __future__ import annotations

import hashlib
from typing import Protocol

from speechnexus_backend.workbench.application.books import WorkbenchBooks
from speechnexus_backend.workbench.application.saved_assets import WorkbenchSavedAssets
from speechnexus_backend.workbench.domain.render_units import RenderSource, RenderUnit


class RenderResultCopyPort(Protocol):
    async def save_copy(
        self, *, workspace_id: str, unit: RenderUnit, source: RenderSource
    ) -> str: ...


class WorkbenchRenderResultCopies:
    """Resolve the unit's book to its project and save the receipt-pinned copy."""

    def __init__(self, books: WorkbenchBooks, assets: WorkbenchSavedAssets) -> None:
        self._books = books
        self._assets = assets

    async def save_copy(self, *, workspace_id: str, unit: RenderUnit, source: RenderSource) -> str:
        book = await self._books.get_book(book_id=unit.book_id, workspace_id=workspace_id)
        asset = await self._assets.save(
            project_id=book.project_id,
            workspace_id=workspace_id,
            source_artifact_id=source.artifact_id,
            source_receipt_id=source.receipt_id,
            key="render:"
            + hashlib.sha256(f"{unit.id}\n{source.artifact_id}".encode()).hexdigest()[:48],
        )
        return asset.id
