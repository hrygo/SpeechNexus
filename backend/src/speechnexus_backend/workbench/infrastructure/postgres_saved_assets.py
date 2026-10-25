"""Saved-asset rows and request receipts share the bounded workbench transaction."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from speechnexus_backend.workbench.application.versions import (
    IdempotencyConflictError,
    ResourceNotFoundError,
)
from speechnexus_backend.workbench.domain.saved_assets import SavedAsset, SavedAssetCopy
from speechnexus_backend.workbench.infrastructure.postgres_content_writes import (
    PostgresContentWrites,
)
from speechnexus_backend.workbench.infrastructure.postgres_versions import (
    PostgresVersionUnitOfWork,
    _Session,
)

_COLUMNS = """
    id, project_id, media_type, bytes, sha256, source_artifact_id, source_receipt_id,
    display_name, object_key, created_at
"""


@dataclass(frozen=True)
class _Row:
    id: str
    project_id: str
    media_type: str
    bytes: Decimal
    sha256: str
    source_artifact_id: str
    source_receipt_id: str
    display_name: str | None
    object_key: str
    created_at: datetime

    def domain(self) -> SavedAsset:
        return SavedAsset(
            id=self.id,
            project_id=self.project_id,
            object_key=self.object_key,
            copy=SavedAssetCopy(
                source_artifact_id=self.source_artifact_id,
                source_receipt_id=self.source_receipt_id,
                media_type=self.media_type,
                bytes=int(self.bytes),
                sha256=self.sha256,
                display_name=self.display_name,
            ),
            created_at=self.created_at,
        )


@dataclass(frozen=True)
class _ReceiptRow:
    intent_digest: str
    asset_id: str
    created_at: datetime


class PostgresSavedAssets:
    """One project lock serializes receipt publication; objects are written before it."""

    def __init__(self, versions: PostgresVersionUnitOfWork) -> None:
        self._versions = versions
        self.content_writes = PostgresContentWrites(versions)

    async def _project(
        self, base: _Session, project_id: str, workspace_id: str, *, lock: bool
    ) -> None:
        # A tombstoned project is invisible here, so deletion wins without extra state.
        project = await base.get_project(project_id, for_update=lock)
        if project is None or project.workspace_id != workspace_id:
            raise ResourceNotFoundError

    async def _asset(self, base: _Session, asset_id: str) -> _Row | None:
        return await base._one(
            _Row,
            f"SELECT {_COLUMNS} FROM {{s}}.saved_assets WHERE id = %s",
            (asset_id,),
        )

    async def _receipt(self, base: _Session, project_id: str, digest: str) -> _ReceiptRow | None:
        # Join the copy so a receipt can never outlive the row it publishes.
        return await base._one(
            _ReceiptRow,
            """
            SELECT r.intent_digest, a.id AS asset_id, a.created_at
            FROM {s}.saved_asset_receipts r
            JOIN {s}.saved_assets a ON a.id = r.asset_id
            WHERE r.project_id = %s AND r.key_digest = %s
        """,
            (project_id, digest),
        )

    async def check_project(self, *, project_id: str, workspace_id: str) -> None:
        async with self._versions.transaction() as base:
            assert isinstance(base, _Session)
            await self._project(base, project_id, workspace_id, lock=False)

    async def get(self, *, asset_id: str, workspace_id: str) -> SavedAsset:
        async with self._versions.transaction() as base:
            assert isinstance(base, _Session)
            row = await self._asset(base, asset_id)
            if row is None:
                raise ResourceNotFoundError
            await self._project(base, row.project_id, workspace_id, lock=False)
            return row.domain()

    async def replay(
        self, *, project_id: str, workspace_id: str, key: str, intent_digest: str
    ) -> SavedAsset | None:
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
        async with self._versions.transaction() as base:
            assert isinstance(base, _Session)
            await self._project(base, project_id, workspace_id, lock=False)
            receipt = await self._receipt(base, project_id, digest)
            if receipt is None:
                return None
            if receipt.intent_digest != intent_digest:
                raise IdempotencyConflictError
            row = await self._asset(base, receipt.asset_id)
            if row is None:
                raise ResourceNotFoundError
            return row.domain()

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
        digest = None if key is None else hashlib.sha256(key.encode("utf-8")).hexdigest()
        async with self._versions.transaction() as base:
            assert isinstance(base, _Session)
            await self._project(base, project_id, workspace_id, lock=True)
            if digest is not None:
                assert intent_digest is not None
                receipt = await self._receipt(base, project_id, digest)
                if receipt is not None:
                    if receipt.intent_digest != intent_digest:
                        raise IdempotencyConflictError
                    row = await self._asset(base, receipt.asset_id)
                    if row is None:
                        raise ResourceNotFoundError
                    return row.domain()
            now = await base.now()
            asset = SavedAsset(
                id=asset_id,
                project_id=project_id,
                object_key=object_key,
                copy=copy,
                created_at=now,
            )
            await base._execute(
                """
                INSERT INTO {s}.saved_assets
                    (id, project_id, media_type, bytes, sha256, source_artifact_id,
                        source_receipt_id, display_name, object_key, created_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
                (
                    asset.id,
                    asset.project_id,
                    asset.copy.media_type,
                    asset.copy.bytes,
                    asset.copy.sha256,
                    asset.copy.source_artifact_id,
                    asset.copy.source_receipt_id,
                    asset.copy.display_name,
                    asset.object_key,
                    asset.created_at,
                ),
            )
            if digest is not None:
                assert intent_digest is not None
                await base._execute(
                    """
                    INSERT INTO {s}.saved_asset_receipts
                        (project_id, key_digest, intent_digest, asset_id, created_at)
                    VALUES (%s, %s, %s, %s, %s)
                """,
                    (project_id, digest, intent_digest, asset.id, now),
                )
            return asset
