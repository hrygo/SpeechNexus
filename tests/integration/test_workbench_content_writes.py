"""Real PG content-write ownership versus deletion acceptance."""

import asyncio
import hashlib
import io
from typing import BinaryIO

import pytest
from tests.integration.test_workbench_owned_deletions import deletions
from tests.integration.test_workbench_owned_deletions import owned_store as owned_store
from tests.integration.test_workbench_recovery_postgres import probe as probe
from tests.integration.test_workbench_versions_postgres import Store

from speechnexus_backend.workbench.application.content_writes import ContentWrite
from speechnexus_backend.workbench.application.saved_assets import (
    ObjectZoneUnavailableError,
    WorkbenchSavedAssets,
)
from speechnexus_backend.workbench.application.speech_sources import ArtifactCopy
from speechnexus_backend.workbench.domain.deletion import (
    DeletionPlan,
    DeletionPlanChangedError,
    DeletionTarget,
)
from speechnexus_backend.workbench.infrastructure.deletion_codec import plan_digest
from speechnexus_backend.workbench.infrastructure.postgres_content_writes import (
    PostgresContentWrites,
)
from speechnexus_backend.workbench.infrastructure.postgres_saved_assets import PostgresSavedAssets
from speechnexus_backend.workbench.infrastructure.postgres_versions import VersionStoreError

pytestmark = pytest.mark.integration
WORKSPACE = "workspace-exports"
BODY = b"synthetic project copy"
DIGEST = "sha256:" + hashlib.sha256(BODY).hexdigest()


async def preview(store: Store, identity: str, key: str) -> DeletionPlan:
    return await deletions(store).preview(
        workspace_id=WORKSPACE,
        target=DeletionTarget("workbench", "project", identity, 1),
        mode="cascade",
        key=key,
    )


class Source:
    def __init__(self, *, pause: bool = False) -> None:
        self.entered, self.release = asyncio.Event(), asyncio.Event()
        self.calls = 0
        if not pause:
            self.release.set()

    async def copy(self, *, artifact_id: str, receipt_id: str, workspace_id: str) -> ArtifactCopy:
        self.calls += 1
        self.entered.set()
        await self.release.wait()
        return ArtifactCopy(
            artifact_id, receipt_id, "audio/wav", len(BODY), DIGEST, io.BytesIO(BODY)
        )


class Zone:
    def __init__(self, *, lost_reply: bool = False) -> None:
        self.objects: dict[str, bytes] = {}
        self.lost_reply = lost_reply

    async def put(self, *, key: str, body: BinaryIO, length: int) -> None:
        value = body.read()
        assert len(value) == length
        self.objects[key] = value
        if self.lost_reply:
            raise ObjectZoneUnavailableError

    async def erase_prefix(self, *, prefix: str) -> int:
        keys = [key for key in self.objects if key.startswith(prefix)]
        for key in keys:
            del self.objects[key]
        return len(keys)


async def test_download_in_progress_invalidates_delete_plan_before_object_write(
    owned_store: Store,
) -> None:
    identity = (await owned_store.app.create_project(workspace_id=WORKSPACE, name="合成项目")).id
    before = await preview(owned_store, identity, "before")
    source, zone = Source(pause=True), Zone()
    saves = WorkbenchSavedAssets(PostgresSavedAssets(owned_store.uow), source, zone)
    attempt = asyncio.create_task(
        saves.save(
            project_id=identity,
            workspace_id=WORKSPACE,
            source_artifact_id="artifact-copy-one",
            source_receipt_id="receipt-copy-one",
            key="original-save-key",
        )
    )
    try:
        await asyncio.wait_for(source.entered.wait(), timeout=5)
        assert zone.objects == {}
        assert not (await preview(owned_store, identity, "during")).eligible
        with pytest.raises(DeletionPlanChangedError):
            await deletions(owned_store).accept(
                workspace_id=WORKSPACE,
                plan_id=before.id,
                expected_revision=before.revision,
                digest=plan_digest(before),
                targets=before.confirmed_targets,
                key="old-plan",
            )
    finally:
        source.release.set()
        saved = await attempt
    assert saved.project_id == identity and len(zone.objects) == 1
    assert (await preview(owned_store, identity, "after")).eligible


async def test_unknown_put_keeps_original_write_and_prevents_deletion(owned_store: Store) -> None:
    identity = (await owned_store.app.create_project(workspace_id=WORKSPACE, name="合成项目")).id
    source, zone = Source(), Zone(lost_reply=True)
    saves = WorkbenchSavedAssets(PostgresSavedAssets(owned_store.uow), source, zone)
    with pytest.raises(ObjectZoneUnavailableError):
        await saves.save(
            project_id=identity,
            workspace_id=WORKSPACE,
            source_artifact_id="artifact-copy-one",
            source_receipt_id="receipt-copy-one",
            key="original-save-key",
        )
    assert len(zone.objects) == 1
    assert not (await preview(owned_store, identity, "unknown")).eligible
    with pytest.raises(VersionStoreError):
        await saves.save(
            project_id=identity,
            workspace_id=WORKSPACE,
            source_artifact_id="artifact-copy-one",
            source_receipt_id="receipt-copy-one",
            key="original-save-key",
        )
    assert source.calls == 1


async def test_reserved_replay_fences_old_owner_before_put_and_cannot_release_new_owner(
    owned_store: Store,
) -> None:
    identity = (await owned_store.app.create_project(workspace_id=WORKSPACE, name="合成项目")).id
    writes = PostgresContentWrites(owned_store.uow)

    async def reserve(resource: str) -> ContentWrite:
        return await writes.reserve_asset(
            project_id=identity,
            workspace_id=WORKSPACE,
            asset_id=resource,
            source_artifact_id="artifact-copy-one",
            source_receipt_id="receipt-copy-one",
            display_name=None,
            key="original-save-key",
            intent_digest="a" * 64,
        )

    first = await reserve("first-resource")
    second = await reserve("replacement-resource")
    assert first.id == second.id and first.resource_id == second.resource_id
    assert first.token != second.token
    with pytest.raises(VersionStoreError):
        await writes.writing(first, object_key=identity + "/copy", sha256=DIGEST, size=len(BODY))
    await writes.settle(first)
    assert not (await preview(owned_store, identity, "stale-release")).eligible
    await writes.writing(second, object_key=identity + "/copy", sha256=DIGEST, size=len(BODY))
    await writes.settle(first)
    assert not (await preview(owned_store, identity, "stale-writing-release")).eligible
    await writes.settle(second)
    assert (await preview(owned_store, identity, "owner-settled")).eligible
