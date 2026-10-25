"""Real bounded metadata reads, only against the disposable workbench probe."""

import asyncio
from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import psycopg
import pytest
from psycopg import sql
from tests.integration.test_workbench_deletion_cleanup import Cleanup
from tests.integration.test_workbench_deletion_cleanup import cleanup as cleanup
from tests.integration.test_workbench_deletions_postgres import service
from tests.integration.test_workbench_versions_postgres import Store
from tests.integration.test_workbench_versions_postgres import store as store

from speechnexus_backend.workbench.application.listings import InvalidListingError
from speechnexus_backend.workbench.application.versions import ResourceNotFoundError
from speechnexus_backend.workbench.domain.deletion import DeletionRecord, DeletionTarget
from speechnexus_backend.workbench.domain.versions import (
    Project,
    ProjectChanges,
    SpeechSource,
    Transcript,
)
from speechnexus_backend.workbench.infrastructure.deletion_codec import plan_digest
from speechnexus_backend.workbench.infrastructure.postgres_versions import (
    VersionStoreError,
    _Session,
)

pytestmark = pytest.mark.integration
NOW = datetime(2026, 10, 23, tzinfo=UTC)


async def project(store: Store, index: int, *, workspace: str = "workspace-one") -> Project:
    value = Project.create(
        project_id=f"project-{index:03}", workspace_id=workspace, name="合成项目", now=NOW
    )
    async with store.uow.transaction() as session:
        await session.save_project(value)
    return value


async def transcript(
    store: Store, parent: Project, index: int, *, source: SpeechSource | None = None
) -> Transcript:
    value = Transcript.create(
        transcript_id=f"transcript-{index:03}",
        project_id=parent.id,
        display_name="合成稿件",
        revision_id=f"revision-{index:03}",
        text="文" * 1_000_000,
        now=NOW,
        source=source,
    )
    async with store.uow.transaction() as session:
        assert await session.get_project(parent.id, for_update=True) is not None
        await session.save_transcript(value)
    return value


async def delete(store: Store, parent: Project, resource: DeletionTarget) -> DeletionRecord:
    app = service(store)
    plan = await app.preview(
        workspace_id=parent.workspace_id,
        target=resource,
        mode="cascade",
        key="preview-" + resource.id,
    )
    return await app.accept(
        workspace_id=parent.workspace_id,
        plan_id=plan.id,
        expected_revision=1,
        digest=plan_digest(plan),
        targets=plan.confirmed_targets,
        key="delete-" + resource.id,
    )


async def test_runtime_role_project_pagination_reads_current_heads_and_scopes(store: Store) -> None:
    rows = [await project(store, index) for index in range(5)]
    await project(store, 99, workspace="workspace-two")
    first = await store.app.list_projects(workspace_id="workspace-one", limit=2)
    assert first.items == (rows[4], rows[3])
    assert first.next_position is not None
    with pytest.raises(InvalidListingError):
        await store.app.list_projects(workspace_id="workspace-two", after=first.next_position)
    updated = await store.app.edit_project(
        project_id=rows[1].id, expected_revision=1, changes=ProjectChanges(status="archived")
    )
    second = await store.app.list_projects(
        workspace_id="workspace-one", limit=2, after=first.next_position
    )
    last = await store.app.list_projects(
        workspace_id="workspace-one", limit=2, after=second.next_position
    )
    assert second.items == (rows[2], updated)
    assert last.items == (rows[0],) and last.next_position is None
    assert (await store.app.list_projects(workspace_id="missing")).items == ()


async def test_transcript_metadata_pagination_preserves_source_and_never_needs_text_permission(
    store: Store,
) -> None:
    parent = await project(store, 1)
    other = await project(store, 2)
    assert (await store.app.list_transcripts(project_id=parent.id)).items == ()
    with pytest.raises(ResourceNotFoundError):
        await store.app.list_transcripts(project_id="missing")
    source = SpeechSource("artifact-synthetic", "receipt-synthetic")
    rows = [await transcript(store, parent, index, source=source) for index in range(3)]
    await transcript(store, other, 99)
    changed = await store.app.revise_transcript(
        transcript_id=rows[1].id, expected_revision=1, text="修订"
    )
    # The database itself rejects reading text under this role. Metadata must still work.
    async with await psycopg.AsyncConnection.connect(store.owner_dsn, autocommit=True) as owner:
        await owner.execute(
            sql.SQL("REVOKE SELECT ON {}.transcript_revisions FROM {}").format(
                sql.Identifier(store.schema), sql.Identifier(store.role)
            )
        )
        await owner.execute(
            sql.SQL(
                "GRANT SELECT (id, transcript_id, revision, created_at, "
                "source_artifact_id, source_receipt_id) ON {}.transcript_revisions TO {}"
            ).format(sql.Identifier(store.schema), sql.Identifier(store.role))
        )
    first = await store.app.list_transcripts(project_id=parent.id, limit=2)
    assert [row.id for row in first.items] == [rows[2].id, rows[1].id]
    current = first.items[1]
    assert current.head_revision == 2 and current.updated_at == changed.head.created_at
    assert current.created_at == NOW and current.source == source
    assert not hasattr(current, "text") and not hasattr(current, "head")
    last = await store.app.list_transcripts(
        project_id=parent.id, limit=1, after=first.next_position
    )
    assert [row.id for row in last.items] == [rows[0].id] and last.next_position is None
    with pytest.raises(InvalidListingError):
        await store.app.list_transcripts(project_id=other.id, after=first.next_position)
    with pytest.raises(VersionStoreError) as denied:
        await store.app.get_transcript(rows[0].id)
    assert denied.value.sqlstate == "42501"


async def test_project_tombstone_filters_before_limit_and_cleaned_anchor_can_continue(
    store: Store, cleanup: Cleanup
) -> None:
    rows = [await project(store, index) for index in range(5)]
    first = await store.app.list_projects(workspace_id="workspace-one", limit=2)
    # Drop the last read anchor and the next unread row through the real deletion path.
    for row in (rows[3], rows[2]):
        record = await delete(store, row, DeletionTarget("workbench", "project", row.id, 1))
        if row == rows[3]:
            assert await cleanup.worker.run(record.id) is not None
    following = await store.app.list_projects(
        workspace_id="workspace-one", limit=2, after=first.next_position
    )
    assert following.items == (rows[1], rows[0]) and following.next_position is None
    assert (await store.app.list_projects(workspace_id="workspace-one", limit=100)).items == (
        rows[4],
        rows[1],
        rows[0],
    )


async def test_transcript_tombstone_and_cascade_parent_are_hidden_before_and_after_cleanup(
    store: Store, cleanup: Cleanup
) -> None:
    parent = await project(store, 1)
    rows = [await transcript(store, parent, index) for index in range(4)]
    first = await store.app.list_transcripts(project_id=parent.id, limit=1)
    record = await delete(store, parent, DeletionTarget("workbench", "transcript", rows[3].id, 1))
    assert await cleanup.worker.run(record.id) is not None
    pending = await delete(store, parent, DeletionTarget("workbench", "transcript", rows[2].id, 1))
    following = await store.app.list_transcripts(
        project_id=parent.id, limit=2, after=first.next_position
    )
    assert [row.id for row in following.items] == [rows[1].id, rows[0].id]
    assert following.next_position is None
    assert await cleanup.worker.run(pending.id) is not None
    record = await delete(store, parent, DeletionTarget("workbench", "project", parent.id, 1))
    for cleaned in (False, True):
        if cleaned:
            assert await cleanup.worker.run(record.id) is not None
        with pytest.raises(ResourceNotFoundError):
            await store.app.list_transcripts(project_id=parent.id)


async def test_list_reads_committed_snapshot_without_waiting_for_writer_row_locks(
    store: Store,
) -> None:
    parent = await project(store, 1)
    text = await transcript(store, parent, 1)
    async with store.uow.transaction() as writer:
        locked = await writer.get_project(parent.id, for_update=True)
        assert locked is not None
        changed = locked.edit(
            expected_revision=1,
            changes=ProjectChanges(name="未提交"),
            now=NOW + timedelta(seconds=1),
        )
        await writer.save_project(changed)
        locked_text = await writer.get_transcript(text.id, for_update=True)
        assert locked_text is not None
        await writer.save_transcript(
            locked_text.revise(
                expected_revision=1,
                revision_id="revision-two",
                text="未提交正文",
                now=NOW + timedelta(seconds=1),
            )
        )
        projects = await asyncio.wait_for(store.app.list_projects(workspace_id="workspace-one"), 1)
        transcripts = await asyncio.wait_for(store.app.list_transcripts(project_id=parent.id), 1)
        assert projects.items == (parent,)
        assert transcripts.items[0].head_revision == 1 and transcripts.items[0].updated_at == NOW
    assert (await store.app.list_projects(workspace_id="workspace-one")).items == (changed,)
    assert (await store.app.list_transcripts(project_id=parent.id)).items[0].head_revision == 2


async def test_newly_committed_creation_does_not_shift_seek_position(store: Store) -> None:
    rows = [await project(store, index) for index in range(3)]
    first = await store.app.list_projects(workspace_id="workspace-one", limit=1)
    async with store.uow.transaction() as session:
        newest = replace(
            rows[2],
            id="project-new",
            created_at=NOW + timedelta(seconds=1),
            updated_at=NOW + timedelta(seconds=1),
        )
        await session.save_project(newest)
    following = await store.app.list_projects(
        workspace_id="workspace-one", after=first.next_position
    )
    assert following.items == (rows[1], rows[0])
    assert (await store.app.list_projects(workspace_id="workspace-one", limit=1)).items == (newest,)


async def test_parent_and_children_share_one_statement_when_cleanup_commits_during_read(
    store: Store, cleanup: Cleanup, monkeypatch: pytest.MonkeyPatch
) -> None:
    parent = await project(store, 1)
    text = await transcript(store, parent, 1)
    original = _Session._many
    statements = 0

    async def delete_after_read[T](
        session: _Session, factory: type[T], query: str, params: Sequence[object]
    ) -> tuple[T, ...]:
        nonlocal statements
        rows = await original(session, factory, query, params)
        statements += 1
        if statements == 1:
            record = await delete(
                store, parent, DeletionTarget("workbench", "project", parent.id, 1)
            )
            assert await cleanup.worker.run(record.id) is not None
        return rows

    with monkeypatch.context() as patch:
        patch.setattr(_Session, "_many", delete_after_read)
        before_delete = await store.app.list_transcripts(project_id=parent.id)
    # A read linearizes at its SQL snapshot. A subsequent cleanup does not combine a
    # post-delete parent check with a pre-delete child result, or re-read content.
    assert statements == 1
    assert [row.id for row in before_delete.items] == [text.id]
    assert before_delete.next_position is None
    with pytest.raises(ResourceNotFoundError):
        await store.app.list_transcripts(project_id=parent.id)
