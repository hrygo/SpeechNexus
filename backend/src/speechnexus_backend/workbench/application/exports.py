"""Export request use cases over a narrow store port.

Reading and cancelling stay here; producing an artifact belongs to the export worker.
"""

from typing import Protocol, cast

from speechnexus_backend.workbench.domain.exports import (
    ExportArtifact,
    ExportError,
    ExportFormat,
    ExportJob,
    valid_identifier,
)
from speechnexus_backend.workbench.domain.versions import InvalidVersionChangeError

_FORMATS = frozenset({"wav", "mp3", "zip"})
_MAX_REVISION = 9999999999999999999


class ExportJobStore(Protocol):
    async def create_export(
        self,
        *,
        workspace_id: str,
        book_id: str,
        export_format: str,
        expected_casting_revision: int,
        unit_ids: list[str] | None,
        key: str | None,
    ) -> ExportJob: ...

    async def get_export(self, *, export_id: str, workspace_id: str) -> ExportJob: ...

    async def cancel_export(
        self, *, export_id: str, workspace_id: str
    ) -> tuple[ExportJob, bool]: ...

    async def start_export(self, *, export_id: str) -> ExportJob: ...

    async def record_export_progress(self, *, export_id: str, completed: int) -> ExportJob: ...

    async def complete_export(self, *, export_id: str, artifact: ExportArtifact) -> ExportJob: ...

    async def fail_export(self, *, export_id: str, error: ExportError) -> ExportJob: ...

    async def expire_export(self, *, export_id: str) -> ExportJob: ...

    async def claim_queued_exports(self, *, limit: int) -> list[ExportJob]: ...

    async def settle_cancel_export(self, *, export_id: str) -> ExportJob: ...


def _selection(unit_ids: list[str] | None) -> list[str] | None:
    if unit_ids is None:
        return None
    selection = list(unit_ids)
    if not selection or len(set(selection)) != len(selection):
        raise InvalidVersionChangeError("invalid workbench export selection")
    if any(not valid_identifier(unit) for unit in selection):
        raise InvalidVersionChangeError("invalid workbench export selection")
    return selection


class WorkbenchExports:
    def __init__(self, store: ExportJobStore) -> None:
        self._store = store

    async def create_export(
        self,
        *,
        workspace_id: str,
        book_id: str,
        export_format: str,
        expected_casting_revision: int,
        unit_ids: list[str] | None = None,
        key: str | None = None,
    ) -> ExportJob:
        if export_format not in _FORMATS:
            raise InvalidVersionChangeError("invalid workbench export format")
        if (
            type(expected_casting_revision) is not int
            or not 1 <= expected_casting_revision <= _MAX_REVISION
        ):
            raise InvalidVersionChangeError("invalid workbench export revision")
        if key is not None and not 8 <= len(key) <= 128:
            raise InvalidVersionChangeError("invalid workbench request key")
        return await self._store.create_export(
            workspace_id=workspace_id,
            book_id=book_id,
            export_format=cast(ExportFormat, export_format),
            expected_casting_revision=expected_casting_revision,
            unit_ids=_selection(unit_ids),
            key=key,
        )

    async def get_export(self, *, export_id: str, workspace_id: str) -> ExportJob:
        return await self._store.get_export(export_id=export_id, workspace_id=workspace_id)

    async def cancel_export(self, *, export_id: str, workspace_id: str) -> tuple[ExportJob, bool]:
        return await self._store.cancel_export(export_id=export_id, workspace_id=workspace_id)

    async def start_export(self, *, export_id: str) -> ExportJob:
        return await self._store.start_export(export_id=export_id)

    async def record_export_progress(self, *, export_id: str, completed: int) -> ExportJob:
        if type(completed) is not int or completed < 0:
            raise InvalidVersionChangeError("invalid workbench export progress")
        return await self._store.record_export_progress(export_id=export_id, completed=completed)

    async def complete_export(self, *, export_id: str, artifact: ExportArtifact) -> ExportJob:
        if type(artifact) is not ExportArtifact:
            raise InvalidVersionChangeError("invalid workbench export artifact")
        return await self._store.complete_export(export_id=export_id, artifact=artifact)

    async def fail_export(self, *, export_id: str, error: ExportError) -> ExportJob:
        if type(error) is not ExportError:
            raise InvalidVersionChangeError("invalid workbench export error")
        return await self._store.fail_export(export_id=export_id, error=error)

    async def expire_export(self, *, export_id: str) -> ExportJob:
        return await self._store.expire_export(export_id=export_id)
