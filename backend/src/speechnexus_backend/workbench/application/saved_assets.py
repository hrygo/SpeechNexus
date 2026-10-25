"""Saved-asset application boundary: no object write or download inside a transaction."""

from __future__ import annotations

import hashlib
import json
from typing import BinaryIO, Protocol
from uuid import uuid4

from speechnexus_backend.workbench.application.content_writes import ContentWrites
from speechnexus_backend.workbench.application.speech_sources import (
    ArtifactCopy,
    SpeechArtifactSource,
)
from speechnexus_backend.workbench.domain.saved_assets import (
    SavedAsset,
    SavedAssetCopy,
    saved_object_key,
    validate_saved_asset_request,
)
from speechnexus_backend.workbench.domain.versions import InvalidVersionChangeError


class ObjectZoneUnavailableError(Exception):
    """The private object zone could not accept or return the committed bytes."""

    def __init__(self) -> None:
        super().__init__("workbench object zone unavailable")


class SavedAssetStore(Protocol):
    """Transactions own the receipt check; every method is short and keyed by project."""

    @property
    def content_writes(self) -> ContentWrites: ...

    async def get(self, *, asset_id: str, workspace_id: str) -> SavedAsset: ...

    async def check_project(self, *, project_id: str, workspace_id: str) -> None:
        """Fail before any download or object write when the project is not visible."""
        ...

    async def replay(
        self, *, project_id: str, workspace_id: str, key: str, intent_digest: str
    ) -> SavedAsset | None:
        """Return the committed copy for an already-noted key, or None.

        A stored key with a different intent raises the idempotency conflict; the check
        happens before any download so a replay never refetches artifact bytes.
        """
        ...

    async def commit(
        self,
        *,
        asset_id: str,
        project_id: str,
        workspace_id: str,
        object_key: str,
        copy: SavedAssetCopy,
        key: str | None,
        intent_digest: str | None,
    ) -> SavedAsset:
        """Publish the row that references the already-written object."""
        ...


class ObjectZone(Protocol):
    async def put(self, *, key: str, body: BinaryIO, length: int) -> None:
        """Write exactly ``length`` bytes to a private staging-to-published object."""
        ...

    async def erase_prefix(self, *, prefix: str) -> int:
        """Erase every object under a project prefix; idempotent and re-runnable."""
        ...


def _intent_digest(
    source_artifact_id: str, source_receipt_id: str, display_name: str | None
) -> str:
    return hashlib.sha256(
        json.dumps(
            ["saved_asset.save", source_artifact_id, source_receipt_id, display_name],
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()


class WorkbenchSavedAssets:
    """Typed commands; transport parsing, object addressing and receipts stay outside."""

    def __init__(
        self, store: SavedAssetStore, source: SpeechArtifactSource, objects: ObjectZone
    ) -> None:
        self._store = store
        self._source = source
        self._objects = objects

    async def get(self, *, asset_id: str, workspace_id: str) -> SavedAsset:
        return await self._store.get(asset_id=asset_id, workspace_id=workspace_id)

    async def save(
        self,
        *,
        project_id: str,
        workspace_id: str,
        source_artifact_id: str,
        source_receipt_id: str,
        display_name: str | None = None,
        key: str | None = None,
    ) -> SavedAsset:
        try:
            validate_saved_asset_request(
                source_artifact_id=source_artifact_id,
                source_receipt_id=source_receipt_id,
                display_name=display_name,
            )
        except ValueError:
            raise InvalidVersionChangeError("invalid saved asset request") from None
        if key is not None and not 8 <= len(key) <= 128:
            raise InvalidVersionChangeError("invalid saved asset request key")
        intent = _intent_digest(source_artifact_id, source_receipt_id, display_name)
        # Visibility and receipt reads are optimizations. The durable reservation
        # under the project lock is the barrier against a concurrent deletion.
        await self._store.check_project(project_id=project_id, workspace_id=workspace_id)
        if key is not None:
            # Outside any transaction: an accepted replay must not refetch the artifact.
            existing = await self._store.replay(
                project_id=project_id, workspace_id=workspace_id, key=key, intent_digest=intent
            )
            if existing is not None:
                return existing
        writes = self._store.content_writes
        write = await writes.reserve_asset(
            project_id=project_id,
            workspace_id=workspace_id,
            asset_id=uuid4().hex,
            source_artifact_id=source_artifact_id,
            source_receipt_id=source_receipt_id,
            display_name=display_name,
            key=key,
            intent_digest=intent,
        )
        copy: ArtifactCopy | None = None
        put_started, put_confirmed = False, False
        try:
            copy = await self._source.copy(
                artifact_id=source_artifact_id,
                receipt_id=source_receipt_id,
                workspace_id=workspace_id,
            )
            object_key = saved_object_key(project_id, copy.sha256)
            await writes.writing(write, object_key=object_key, sha256=copy.sha256, size=copy.bytes)
            # Object first, database reference second: a rollback never publishes a row
            # whose bytes were not durably written.
            put_started = True
            await self._objects.put(key=object_key, body=copy.body, length=copy.bytes)
            put_confirmed = True
            return await self._store.commit(
                asset_id=write.resource_id,
                project_id=project_id,
                workspace_id=workspace_id,
                object_key=object_key,
                copy=self._copy(copy, display_name),
                key=key,
                intent_digest=intent if key is not None else None,
            )
        finally:
            if copy is not None:
                copy.close()
            if not put_started or put_confirmed:
                await writes.settle(write)
            else:
                await writes.unknown(write)

    @staticmethod
    def _copy(copy: ArtifactCopy, display_name: str | None) -> SavedAssetCopy:
        return SavedAssetCopy(
            source_artifact_id=copy.artifact_id,
            source_receipt_id=copy.receipt_id,
            media_type=copy.media_type,
            bytes=copy.bytes,
            sha256=copy.sha256,
            display_name=display_name,
        )
