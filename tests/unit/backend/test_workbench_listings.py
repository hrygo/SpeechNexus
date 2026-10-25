"""Bounded metadata pages; internal positions are not public cursors."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from speechnexus_backend.workbench.application.listings import InvalidListingError, ListingPosition
from speechnexus_backend.workbench.application.versions import (
    ResourceNotFoundError,
    WorkbenchVersions,
)
from speechnexus_backend.workbench.domain.versions import ProjectChanges
from tests.fake.workbench_versions import MemoryVersionUnitOfWork

NOW = datetime(2026, 10, 24, tzinfo=UTC)


async def test_tied_project_pages_are_scoped_and_have_no_duplicate_or_missing_ids() -> None:
    memory = MemoryVersionUnitOfWork(now=NOW)
    app = WorkbenchVersions(memory)
    projects = [
        await app.create_project(workspace_id="workspace-one", name="合成项目") for _ in range(5)
    ]
    await app.create_project(workspace_id="workspace-two", name="其他作用域")
    expected = sorted((p.id for p in projects), reverse=True)
    first = await app.list_projects(workspace_id="workspace-one", limit=2)
    assert [p.id for p in first.items] == expected[:2]
    assert first.next_position == ListingPosition("projects", "workspace-one", NOW, expected[1])
    second = await app.list_projects(
        workspace_id="workspace-one", limit=2, after=first.next_position
    )
    last = await app.list_projects(
        workspace_id="workspace-one", limit=2, after=second.next_position
    )
    assert [p.id for p in (*first.items, *second.items, *last.items)] == expected
    assert last.next_position is None
    assert (await app.list_projects(workspace_id="missing")).items == ()


async def test_transcript_pages_only_expose_current_metadata_and_sources() -> None:
    memory = MemoryVersionUnitOfWork(now=NOW)
    app = WorkbenchVersions(memory)
    project = await app.create_project(workspace_id="workspace-one", name="合成项目")
    assert (await app.list_transcripts(project_id=project.id)).items == ()
    transcript = await app.create_manual_transcript(
        project_id=project.id, display_name="合成稿件", text="合成正文"
    )
    memory.current_time += timedelta(seconds=1)
    revised = await app.revise_transcript(
        transcript_id=transcript.id, expected_revision=1, text="修订正文"
    )
    (summary,) = (await app.list_transcripts(project_id=project.id)).items
    assert summary.id == transcript.id
    assert summary.project_id == project.id
    assert summary.display_name == transcript.display_name
    assert summary.created_at == transcript.created_at
    assert summary.head_revision == 2
    assert summary.updated_at == revised.head.created_at
    assert summary.source is None
    assert not hasattr(summary, "text") and not hasattr(summary, "head")
    assert "合成" not in repr(summary)
    with pytest.raises(ResourceNotFoundError):
        await app.list_transcripts(project_id="missing")


async def test_metadata_edits_do_not_move_items_and_later_insert_requires_first_page_refresh() -> (
    None
):
    memory = MemoryVersionUnitOfWork(now=NOW)
    app = WorkbenchVersions(memory)
    projects = []
    for _ in range(3):
        memory.current_time += timedelta(seconds=1)
        projects.append(await app.create_project(workspace_id="workspace-one", name="合成项目"))
    first = await app.list_projects(workspace_id="workspace-one", limit=1)
    memory.current_time += timedelta(seconds=1)
    changed = await app.edit_project(
        project_id=projects[1].id, expected_revision=1, changes=ProjectChanges(name="已修改")
    )
    newest = await app.create_project(workspace_id="workspace-one", name="最新项目")
    next_page = await app.list_projects(workspace_id="workspace-one", after=first.next_position)
    assert next_page.items == (changed, projects[0])
    assert next_page.next_position is None
    assert (await app.list_projects(workspace_id="workspace-one", limit=1)).items == (newest,)


@pytest.mark.parametrize("limit", [0, 101, -1, True, 1.0, "2", None])
async def test_invalid_limit_is_rejected_before_transaction(limit: object) -> None:
    class NoTransaction:
        def transaction(self) -> None:
            pytest.fail("invalid request reached a transaction")

    app = WorkbenchVersions(NoTransaction())  # type: ignore[arg-type]
    with pytest.raises(InvalidListingError, match=r"^invalid workbench listing$"):
        await app.list_projects(workspace_id="workspace-one", limit=limit)  # type: ignore[arg-type]
    with pytest.raises(InvalidListingError):
        await app.list_transcripts(project_id="project-one", limit=limit)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "field,value",
    [
        ("kind", "transcripts"),
        ("parent_id", "workspace-two"),
        ("created_at", NOW.replace(tzinfo=None)),
        ("item_id", ""),
        ("item_id", "x" * 129),
    ],
)
async def test_positions_cannot_be_reused_in_another_scope_or_with_invalid_bounds(
    field: str, value: object
) -> None:
    app = WorkbenchVersions(MemoryVersionUnitOfWork(now=NOW))
    position = replace(
        ListingPosition("projects", "workspace-one", NOW, "project-one"),
        **{field: value},  # type: ignore[arg-type]
    )
    with pytest.raises(InvalidListingError):
        await app.list_projects(workspace_id="workspace-one", after=position)


async def test_default_limit_and_exact_full_last_page_do_not_invent_more_items() -> None:
    app = WorkbenchVersions(MemoryVersionUnitOfWork(now=NOW))
    for _ in range(51):
        await app.create_project(workspace_id="workspace-one", name="合成项目")
    first = await app.list_projects(workspace_id="workspace-one")
    assert len(first.items) == 50
    assert first.next_position is not None
    last = await app.list_projects(workspace_id="workspace-one", limit=1, after=first.next_position)
    assert len(last.items) == 1 and last.next_position is None
    assert len((await app.list_projects(workspace_id="workspace-one", limit=100)).items) == 51
