"""Teleprompter application boundary; the injected store owns each atomic transaction."""

from __future__ import annotations

from typing import Protocol

from speechnexus_backend.workbench.domain.teleprompter import (
    MAX_VISIBLE_POSITION,
    TeleprompterState,
)
from speechnexus_backend.workbench.domain.versions import (
    InvalidVersionChangeError,
    validate_revision_number,
)


class TeleprompterStore(Protocol):
    async def get(self, *, project_id: str, workspace_id: str) -> TeleprompterState: ...
    async def update(
        self,
        *,
        project_id: str,
        workspace_id: str,
        script_revision: int,
        visible_position: int,
        manual_hold: bool,
        key: str | None,
    ) -> TeleprompterState: ...


class WorkbenchTeleprompter:
    """Typed commands; transport parsing, cursors and receipts stay outside."""

    def __init__(self, store: TeleprompterStore) -> None:
        self._store = store

    async def get(self, *, project_id: str, workspace_id: str) -> TeleprompterState:
        return await self._store.get(project_id=project_id, workspace_id=workspace_id)

    async def update(
        self,
        *,
        project_id: str,
        workspace_id: str,
        script_revision: int,
        visible_position: int,
        manual_hold: bool,
        key: str | None = None,
    ) -> TeleprompterState:
        validate_revision_number(script_revision)
        if type(visible_position) is not int or not (0 <= visible_position <= MAX_VISIBLE_POSITION):
            raise InvalidVersionChangeError("teleprompter position outside the supported range")
        if type(manual_hold) is not bool:
            raise InvalidVersionChangeError("teleprompter manual hold must be a boolean")
        if key is not None and not 8 <= len(key) <= 128:
            raise InvalidVersionChangeError("invalid teleprompter request key")
        return await self._store.update(
            project_id=project_id,
            workspace_id=workspace_id,
            script_revision=script_revision,
            visible_position=visible_position,
            manual_hold=manual_hold,
            key=key,
        )
