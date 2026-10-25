"""Version transaction and replay outcomes using an explicitly test-only store."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime

import pytest

from speechnexus_backend.workbench.application.version_ports import VersionSession
from speechnexus_backend.workbench.application.versions import (
    IdempotencyConflictError,
    ResourceNotFoundError,
    WorkbenchVersions,
)
from speechnexus_backend.workbench.domain.versions import (
    InvalidVersionChangeError,
    ProjectChanges,
    RevisionConflictError,
    Transcript,
)
from tests.fake.workbench_versions import MemoryVersionUnitOfWork

NOW = datetime(2026, 10, 24, tzinfo=UTC)


@pytest.fixture
def store() -> MemoryVersionUnitOfWork:
    return MemoryVersionUnitOfWork(now=NOW)


@pytest.mark.parametrize("bad", ["synthetic\x00body", "synthetic\ud800body", "synthetic\udfffbody"])
async def test_invalid_content_rolls_back_and_keeps_request_keys_available(
    store: MemoryVersionUnitOfWork, bad: str
) -> None:
    service = WorkbenchVersions(store)
    with pytest.raises(InvalidVersionChangeError):
        await service.create_project(workspace_id="w", name=bad, idempotency_key="project")
    assert not store.projects and not store.receipts
    project = await service.create_project(workspace_id="w", name="合成", idempotency_key="project")
    with pytest.raises(InvalidVersionChangeError):
        await service.create_manual_transcript(
            project_id=project.id, display_name="合成", text=bad, idempotency_key="text"
        )
    assert not store.transcripts and not store.revisions and len(store.receipts) == 1
    original = await service.create_manual_transcript(
        project_id=project.id, display_name="合成", text="原文", idempotency_key="text"
    )
    with pytest.raises(InvalidVersionChangeError):
        await service.revise_transcript(
            transcript_id=original.id, expected_revision=1, text=bad, idempotency_key="edit"
        )
    assert await service.get_transcript(original.id) == original
    assert len(store.revisions) == 1 and len(store.receipts) == 2
    changed = await service.revise_transcript(
        transcript_id=original.id, expected_revision=1, text="校对", idempotency_key="edit"
    )
    assert changed.head.number == 2
    assert (await service.get_revision(original.id, 1)).text == "原文"


async def test_project_creation_replays_original_snapshot_after_later_metadata_change(
    store: MemoryVersionUnitOfWork,
) -> None:
    service = WorkbenchVersions(store)
    first = await service.create_project(
        workspace_id="workspace-test", name="合成项目", idempotency_key="create-project"
    )
    updated = await service.edit_project(
        project_id=first.id, expected_revision=1, changes=ProjectChanges(name="新标题")
    )
    replay = await service.create_project(
        workspace_id="workspace-test", name="合成项目", idempotency_key="create-project"
    )
    assert replay == first
    assert updated.metadata_revision == 2
    assert (await service.get_project(first.id)).name == "新标题"
    assert len(store.projects) == 1


async def test_reusing_key_with_different_content_fails_without_leaking_content(
    store: MemoryVersionUnitOfWork,
) -> None:
    service = WorkbenchVersions(store)
    await service.create_project(
        workspace_id="workspace-test", name="合成项目", idempotency_key="request-test"
    )
    with pytest.raises(IdempotencyConflictError) as caught:
        await service.create_project(
            workspace_id="workspace-test", name="其他合成正文", idempotency_key="request-test"
        )
    assert "其他合成正文" not in str(caught.value)
    assert len(store.projects) == 1


async def test_key_scope_separates_workspaces_and_operations(
    store: MemoryVersionUnitOfWork,
) -> None:
    service = WorkbenchVersions(store)
    first = await service.create_project(
        workspace_id="workspace-one", name="一", idempotency_key="request-test"
    )
    second = await service.create_project(
        workspace_id="workspace-two", name="二", idempotency_key="request-test"
    )
    changed = await service.edit_project(
        project_id=first.id,
        expected_revision=1,
        changes=ProjectChanges(name="新标题"),
        idempotency_key="request-test",
    )
    assert first.id != second.id
    assert changed.metadata_revision == 2


async def test_parallel_project_edits_accept_one_current_revision(
    store: MemoryVersionUnitOfWork,
) -> None:
    service = WorkbenchVersions(store)
    original = await service.create_project(workspace_id="workspace-test", name="合成项目")
    results = await asyncio.gather(
        *(
            service.edit_project(
                project_id=original.id, expected_revision=1, changes=ProjectChanges(name=name)
            )
            for name in ("一", "二")
        ),
        return_exceptions=True,
    )
    assert sum(isinstance(result, RevisionConflictError) for result in results) == 1
    assert (await service.get_project(original.id)).metadata_revision == 2


async def test_transcript_replay_precedes_stale_head_check_and_keeps_history(
    store: MemoryVersionUnitOfWork,
) -> None:
    service = WorkbenchVersions(store)
    project = await service.create_project(workspace_id="workspace-test", name="合成项目")
    original = await service.create_manual_transcript(
        project_id=project.id,
        display_name="合成转写",
        text="原文",
        idempotency_key="create-transcript",
    )
    corrected = await service.revise_transcript(
        transcript_id=original.id,
        expected_revision=1,
        text="校对",
        idempotency_key="correct-transcript",
    )
    await service.revise_transcript(transcript_id=original.id, expected_revision=2, text="再次校对")
    replay = await service.revise_transcript(
        transcript_id=original.id,
        expected_revision=1,
        text="校对",
        idempotency_key="correct-transcript",
    )
    assert replay == corrected
    assert (await service.get_transcript(original.id)).head.number == 3
    assert (await service.get_revision(original.id, 1)).text == "原文"
    assert (await service.get_revision(original.id, 2)).text == "校对"
    assert len(store.revisions) == 3


async def test_revision_source_is_resolved_within_its_transcript(
    store: MemoryVersionUnitOfWork,
) -> None:
    service = WorkbenchVersions(store)
    project = await service.create_project(workspace_id="workspace-test", name="合成项目")
    original = await service.create_manual_transcript(
        project_id=project.id, display_name="合成转写", text="原文"
    )
    await service.revise_transcript(transcript_id=original.id, expected_revision=1, text="校对")
    third = await service.revise_transcript(
        transcript_id=original.id, expected_revision=2, text="重做", source_revision=1
    )
    assert third.head.source_revision == 1
    with pytest.raises(ResourceNotFoundError):
        await service.revise_transcript(
            transcript_id=original.id,
            expected_revision=3,
            text="失败",
            source_revision=99,
            idempotency_key="retry-test",
        )
    successful = await service.revise_transcript(
        transcript_id=original.id,
        expected_revision=3,
        text="成功",
        source_revision=1,
        idempotency_key="retry-test",
    )
    assert successful.head.number == 4


async def test_parallel_transcript_edits_do_not_publish_orphan_revisions(
    store: MemoryVersionUnitOfWork,
) -> None:
    service = WorkbenchVersions(store)
    project = await service.create_project(workspace_id="workspace-test", name="合成项目")
    original = await service.create_manual_transcript(
        project_id=project.id, display_name="合成转写", text="原文"
    )
    results = await asyncio.gather(
        *(
            service.revise_transcript(
                transcript_id=original.id,
                expected_revision=1,
                text=text,
                idempotency_key=f"edit-{index}",
            )
            for index, text in enumerate(("一", "二"))
        ),
        return_exceptions=True,
    )
    assert sum(isinstance(result, RevisionConflictError) for result in results) == 1
    assert (await service.get_transcript(original.id)).head.number == 2
    assert len(store.revisions) == 2
    assert len(store.receipts) == 1


async def test_commit_failure_rolls_back_content_head_and_idempotency_together(
    store: MemoryVersionUnitOfWork,
) -> None:
    service = WorkbenchVersions(store)
    project = await service.create_project(workspace_id="workspace-test", name="合成项目")
    original = await service.create_manual_transcript(
        project_id=project.id, display_name="合成转写", text="原文"
    )
    store.fail_commit = True
    with pytest.raises(RuntimeError, match="synthetic commit failure"):
        await service.revise_transcript(
            transcript_id=original.id,
            expected_revision=1,
            text="校对",
            idempotency_key="retry-edit",
        )
    store.fail_commit = False
    assert (await service.get_transcript(original.id)).head.number == 1
    assert len(store.revisions) == 1
    assert not store.receipts
    retried = await service.revise_transcript(
        transcript_id=original.id, expected_revision=1, text="校对", idempotency_key="retry-edit"
    )
    assert retried.head.number == 2


async def test_omitted_patch_and_explicit_null_are_different_intents(
    store: MemoryVersionUnitOfWork,
) -> None:
    service = WorkbenchVersions(store)
    project = await service.create_project(
        workspace_id="workspace-test", name="合成项目", description="说明"
    )
    await service.edit_project(
        project_id=project.id,
        expected_revision=1,
        changes=ProjectChanges(name="校对"),
        idempotency_key="edit-project",
    )
    with pytest.raises(IdempotencyConflictError):
        await service.edit_project(
            project_id=project.id,
            expected_revision=1,
            changes=ProjectChanges(name="校对", description=None),
            idempotency_key="edit-project",
        )
    assert (await service.get_project(project.id)).description == "说明"


async def test_missing_resources_do_not_create_content_or_replay_receipts(
    store: MemoryVersionUnitOfWork,
) -> None:
    service = WorkbenchVersions(store)
    with pytest.raises(ResourceNotFoundError):
        await service.create_manual_transcript(
            project_id="project-missing",
            display_name="合成",
            text="原文",
            idempotency_key="missing-test",
        )
    with pytest.raises(ResourceNotFoundError):
        await service.get_project("project-missing")
    with pytest.raises(ResourceNotFoundError):
        await service.get_transcript("transcript-missing")
    with pytest.raises(ResourceNotFoundError):
        await service.get_revision("transcript-missing", 1)
    with pytest.raises(ResourceNotFoundError):
        await service.edit_project(
            project_id="project-missing", expected_revision=1, changes=ProjectChanges(name="新")
        )
    with pytest.raises(ResourceNotFoundError):
        await service.revise_transcript(
            transcript_id="transcript-missing", expected_revision=1, text="新"
        )
    assert not store.transcripts
    assert not store.receipts


class PausedCommitUnitOfWork(MemoryVersionUnitOfWork):
    """Allow cancellation after writes but before a transaction commits."""

    def __init__(self) -> None:
        super().__init__(now=NOW)
        self.pause_commit = False
        self.writes_done = asyncio.Event()
        self.release_commit = asyncio.Event()

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[VersionSession]:
        async with super().transaction() as session:
            yield session
            if self.pause_commit:
                self.writes_done.set()
                await self.release_commit.wait()


async def test_cancelled_mutation_does_not_commit_content_or_receipt() -> None:
    store = PausedCommitUnitOfWork()
    service = WorkbenchVersions(store)
    store.pause_commit = True
    task = asyncio.create_task(
        service.create_project(
            workspace_id="workspace-test", name="合成项目", idempotency_key="cancelled-create"
        )
    )
    try:
        await asyncio.wait_for(store.writes_done.wait(), timeout=2)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert not store.projects
    assert not store.receipts
    store.pause_commit = False
    retried = await service.create_project(
        workspace_id="workspace-test", name="合成项目", idempotency_key="cancelled-create"
    )
    assert retried.metadata_revision == 1
    assert len(store.projects) == 1


@pytest.mark.parametrize("target", ["project", "new_transcript", "transcript"])
async def test_adapter_rejects_writes_without_resource_locks(target: str) -> None:
    store = MemoryVersionUnitOfWork(now=NOW)
    service = WorkbenchVersions(store)
    project = await service.create_project(workspace_id="workspace-test", name="合成项目")
    transcript = await service.create_manual_transcript(
        project_id=project.id, display_name="合成转写", text="原文"
    )
    with pytest.raises(AssertionError, match="locked read"):
        async with store.transaction() as session:
            if target == "project":
                await session.get_project(project.id)
                changed = project.edit(
                    expected_revision=1, changes=ProjectChanges(name="新标题"), now=NOW
                )
                await session.save_project(changed)
            elif target == "new_transcript":
                await session.get_project(project.id)
                new_transcript = Transcript.create(
                    transcript_id="transcript-new",
                    project_id=project.id,
                    display_name="合成",
                    revision_id="revision-new",
                    text="新",
                    now=NOW,
                )
                await session.save_transcript(new_transcript)
            else:
                await session.get_transcript(transcript.id)
                corrected = transcript.revise(
                    expected_revision=1, revision_id="revision-new", text="校对", now=NOW
                )
                await session.save_transcript(corrected)
    assert store.projects[project.id].metadata_revision == 1
    assert store.transcripts[transcript.id].head.number == 1
    assert len(store.revisions) == 1
