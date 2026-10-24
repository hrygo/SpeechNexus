"""Only authoritative get and keyset reads; no group, budget or policy writes."""

from __future__ import annotations

from typing import Protocol

from speechnexus_backend.speech.domain.assets import identifier
from speechnexus_backend.speech.domain.workspaces import (
    Workspace,
    WorkspaceNotFoundError,
    WorkspacePage,
    WorkspaceQuery,
    WorkspaceUnavailableError,
)


class WorkspaceReads(Protocol):
    def get(self, workspace_id: str) -> Workspace | None: ...

    def scan(self, query: WorkspaceQuery) -> tuple[Workspace, ...]:
        """Return ordered rows after the key, bounded to limit + 1."""
        ...


class WorkspaceService:
    def __init__(self, reads: WorkspaceReads) -> None:
        self._reads = reads

    def get(self, workspace_id: str) -> Workspace:
        identifier(workspace_id)
        record = self._reads.get(workspace_id)
        if record is None:
            raise WorkspaceNotFoundError
        if not isinstance(record, Workspace) or record.workspace_id != workspace_id:
            raise WorkspaceUnavailableError
        return record

    def list(self, query: WorkspaceQuery) -> WorkspacePage:
        rows = self._reads.scan(query)
        if len(rows) > query.limit + 1:
            raise WorkspaceUnavailableError
        previous = query.after_id or ""
        for record in rows:
            if not isinstance(record, Workspace) or record.workspace_id <= previous:
                raise WorkspaceUnavailableError
            previous = record.workspace_id
        items = rows[: query.limit]
        return WorkspacePage(items, items[-1].workspace_id if len(rows) > query.limit else None)
