"""Read metadata in short transactions; object I/O happens after they close."""

from speechnexus_backend.workbench.application.downloads import DownloadContent, DownloadKind
from speechnexus_backend.workbench.infrastructure.postgres_exports import PostgresExports
from speechnexus_backend.workbench.infrastructure.postgres_saved_assets import PostgresSavedAssets
from speechnexus_backend.workbench.infrastructure.postgres_versions import PostgresVersionUnitOfWork


class PostgresDownloads:
    def __init__(self, versions: PostgresVersionUnitOfWork) -> None:
        self._assets = PostgresSavedAssets(versions)
        self._exports = PostgresExports(versions)

    async def content(
        self, *, kind: DownloadKind, ident: str, workspace_id: str
    ) -> DownloadContent:
        if kind == "export":
            return await self._exports.download_content(export_id=ident, workspace_id=workspace_id)
        asset = await self._assets.get(asset_id=ident, workspace_id=workspace_id)
        return DownloadContent(asset.object_key, asset.copy.bytes, asset.copy.sha256)
