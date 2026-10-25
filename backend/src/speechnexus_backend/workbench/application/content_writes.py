"""Durable ownership of content writes across download, object I/O and publication."""

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class ContentWrite:
    id: str
    project_id: str
    resource_id: str
    token: str


class ContentWrites(Protocol):
    async def reserve_asset(
        self,
        *,
        project_id: str,
        workspace_id: str,
        asset_id: str,
        source_artifact_id: str,
        source_receipt_id: str,
        display_name: str | None,
        key: str | None,
        intent_digest: str,
    ) -> ContentWrite: ...

    async def reserve_export(self, *, export_id: str, artifact_id: str) -> ContentWrite: ...

    async def writing(
        self, write: ContentWrite, *, object_key: str, sha256: str, size: int
    ) -> None: ...

    async def settle(self, write: ContentWrite) -> None:
        """Only the original caller with proof no object request remains may settle."""
        ...

    async def unknown(self, write: ContentWrite) -> None:
        """A failed object request may still commit; preserve its outstanding identity."""
        ...
