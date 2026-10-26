"""Render-unit provenance over the disposable PostgreSQL store.

These tests hold the adapter to its two claims: a unit pins what the database really
holds (chapter revision, voice/model binding) and its dependency digest is recomputed
from stored state, never accepted from a caller.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import psycopg
import pytest
from psycopg import sql
from psycopg.rows import dict_row
from speechnexus_contracts.workbench_http import validate_response
from tests.integration.test_workbench_deletion_cleanup import Cleanup
from tests.integration.test_workbench_deletion_cleanup import cleanup as cleanup
from tests.integration.test_workbench_deletions_postgres import drop_tombstones as drop_tombstones
from tests.integration.test_workbench_deletions_postgres import ledger_entries as ledger_entries
from tests.integration.test_workbench_deletions_postgres import restore_rows as restore_rows
from tests.integration.test_workbench_deletions_postgres import service as deletions_service
from tests.integration.test_workbench_deletions_postgres import snapshot_rows as snapshot_rows
from tests.integration.test_workbench_deletions_postgres import target
from tests.integration.test_workbench_saved_assets_postgres import (
    ARTIFACT,
    RECEIPT,
    Objects,
)
from tests.integration.test_workbench_saved_assets_postgres import (
    WORKSPACE as SAVED_WORKSPACE,
)
from tests.integration.test_workbench_saved_assets_postgres import objects as objects
from tests.integration.test_workbench_saved_assets_postgres import public_http as public_http
from tests.integration.test_workbench_versions_postgres import Store
from tests.integration.test_workbench_versions_postgres import store as store

from speechnexus_backend.workbench.application.books import WorkbenchBooks
from speechnexus_backend.workbench.application.render_units import WorkbenchRenderUnits
from speechnexus_backend.workbench.application.saved_assets import WorkbenchSavedAssets
from speechnexus_backend.workbench.application.versions import (
    IdempotencyConflictError,
    ResourceNotFoundError,
    WorkbenchVersions,
)
from speechnexus_backend.workbench.domain.render_units import RenderUnit
from speechnexus_backend.workbench.domain.versions import (
    InvalidVersionChangeError,
    RevisionConflictError,
)
from speechnexus_backend.workbench.infrastructure.books_schema import (
    books_migration_sql,
    grant_books_runtime_sql,
)
from speechnexus_backend.workbench.infrastructure.deletion_codec import plan_digest
from speechnexus_backend.workbench.infrastructure.postgres_books import PostgresBooks
from speechnexus_backend.workbench.infrastructure.postgres_deletion_cleanup import (
    PostgresDeletionCleanup,
)
from speechnexus_backend.workbench.infrastructure.postgres_render_units import (
    PostgresRenderUnits,
)
from speechnexus_backend.workbench.infrastructure.postgres_saved_assets import PostgresSavedAssets
from speechnexus_backend.workbench.infrastructure.postgres_versions import (
    PostgresVersionUnitOfWork,
)
from speechnexus_backend.workbench.infrastructure.public_artifact_source import (
    PublicArtifactSource,
)
from speechnexus_backend.workbench.infrastructure.render_units_schema import (
    grant_render_units_runtime_sql,
    render_units_migration_sql,
)
from speechnexus_backend.workbench.infrastructure.saved_assets_schema import (
    grant_saved_assets_runtime_sql,
    saved_assets_migration_sql,
)
from speechnexus_backend.workbench.interfaces.render_unit_views import render_unit_payload

pytestmark = pytest.mark.integration
WORKSPACE = "workspace-render"
OTHER_WORKSPACE = "workspace-render-other"
CREATE_KEY = "render-request-key-0001"
CHAPTER_TEXT = "合成章节正文"


@pytest.fixture
async def render_store(store: Store) -> AsyncIterator[Store]:
    """The shared partial fixture plus the book/chapter and render-unit fragments."""
    async with await psycopg.AsyncConnection.connect(store.owner_dsn, autocommit=True) as owner:
        await owner.execute(books_migration_sql(store.schema))
        await owner.execute(grant_books_runtime_sql(store.schema, store.role))
        await owner.execute(render_units_migration_sql(store.schema))
        await owner.execute(grant_render_units_runtime_sql(store.schema, store.role))
        # A unit's read path reports the workbench-owned copy of its result, so this
        # fragment is part of the read surface, not an optional extra.
        await owner.execute(saved_assets_migration_sql(store.schema))
        await owner.execute(grant_saved_assets_runtime_sql(store.schema, store.role))
    yield store


def render_service(store: Store) -> WorkbenchRenderUnits:
    return WorkbenchRenderUnits(PostgresRenderUnits(store.uow))


async def chapter(store: Store) -> tuple[str, str, int]:
    versions = WorkbenchVersions(store.uow)
    project = await versions.create_project(
        workspace_id=WORKSPACE, name="合成有声书项目", idempotency_key="render-project-key"
    )
    books = WorkbenchBooks(PostgresBooks(store.uow))
    book = await books.create_book(
        project_id=project.id, workspace_id=WORKSPACE, title="合成有声书", key="render-book-key"
    )
    made = await books.create_chapter(
        book_id=book.id,
        workspace_id=WORKSPACE,
        title="第一章",
        ordinal=0,
        text=CHAPTER_TEXT,
        key="render-chapter-key",
    )
    return project.id, made.id, made.text_revision


async def stored(store: Store, unit_id: str) -> dict[str, Any]:
    async with await psycopg.AsyncConnection.connect(
        store.owner_dsn, row_factory=dict_row
    ) as owner:
        row = await (
            await owner.execute(
                sql.SQL(
                    "SELECT revision, status, text, dependencies_sha256,"
                    " text_plan_revision, original_revision, working_revision"
                    " FROM {}.render_units WHERE id = %s"
                ).format(sql.Identifier(store.schema)),
                (unit_id,),
            )
        ).fetchone()
    assert row is not None
    return row


async def create(
    store: Store,
    chapter_id: str,
    *,
    key: str | None = CREATE_KEY,
    ordinal: int = 0,
    text: str = "合成旁白单元",
    workspace: str = WORKSPACE,
) -> RenderUnit:
    return await render_service(store).create_unit(
        workspace_id=workspace,
        chapter_id=chapter_id,
        ordinal=ordinal,
        text=text,
        voice_id="voice_demo",
        voice_revision=1,
        model="tts-quality",
        model_binding_id="binding_demo",
        model_binding_revision=3,
        key=key,
    )


async def test_create_pins_the_stored_chapter_revision_and_replays_its_receipt(
    render_store: Store,
) -> None:
    _, chapter_id, revision = await chapter(render_store)
    unit = await create(render_store, chapter_id)

    assert (unit.revision, unit.status, unit.generations, unit.source) == (1, "pending", (), None)
    pinned = unit.dependencies
    assert pinned.chapter_id == chapter_id
    assert (pinned.text_plan.revision, pinned.original.revision, pinned.working.revision) == (
        revision,
        revision,
        revision,
    )
    assert (pinned.voice.id, pinned.voice.revision) == ("voice_demo", 1)
    assert (pinned.model, pinned.model_binding.id, pinned.model_binding.revision) == (
        "tts-quality",
        "binding_demo",
        3,
    )
    row = await stored(render_store, unit.id)
    # The digest the row stores is the one the domain recomputes from stored state.
    assert row["dependencies_sha256"] == pinned.sha256
    assert (row["revision"], row["status"], row["text"]) == (
        1,
        "pending",
        "合成旁白单元",
    )

    replay = await create(render_store, chapter_id)
    assert replay == unit
    with pytest.raises(IdempotencyConflictError):
        await create(render_store, chapter_id, text="另一个合成旁白单元")


async def test_get_refuses_missing_units_and_foreign_workspaces(render_store: Store) -> None:
    _, chapter_id, _ = await chapter(render_store)
    unit = await create(render_store, chapter_id)
    service = render_service(render_store)

    fetched = await service.get_unit(unit_id=unit.id, workspace_id=WORKSPACE)
    assert fetched == unit
    with pytest.raises(ResourceNotFoundError):
        await service.get_unit(unit_id="unit_absent", workspace_id=WORKSPACE)
    # The project row decides visibility, so another workspace never sees the unit.
    with pytest.raises(ResourceNotFoundError):
        await service.get_unit(unit_id=unit.id, workspace_id=OTHER_WORKSPACE)


async def test_update_revises_the_pins_and_marks_the_unit_stale(render_store: Store) -> None:
    _, chapter_id, _ = await chapter(render_store)
    unit = await create(render_store, chapter_id)
    service = render_service(render_store)

    revised = await service.update_unit(
        unit_id=unit.id, workspace_id=WORKSPACE, expected_revision=1, text="改写后的旁白单元"
    )
    assert (revised.revision, revised.status) == (2, "stale")
    assert revised.dependencies.text == "改写后的旁白单元"
    assert revised.dependencies.sha256 != unit.dependencies.sha256
    row = await stored(render_store, unit.id)
    assert (row["revision"], row["status"], row["text"]) == (2, "stale", "改写后的旁白单元")
    assert row["dependencies_sha256"] == revised.dependencies.sha256

    with pytest.raises(RevisionConflictError):
        await service.update_unit(
            unit_id=unit.id, workspace_id=WORKSPACE, expected_revision=1, model="tts-other"
        )
    with pytest.raises(InvalidVersionChangeError):
        await service.update_unit(unit_id=unit.id, workspace_id=WORKSPACE, expected_revision=2)
    fresh = await service.get_unit(unit_id=unit.id, workspace_id=WORKSPACE)
    assert fresh == revised


async def test_start_records_one_attempt_and_rejects_a_stale_revision(
    render_store: Store,
) -> None:
    _, chapter_id, _ = await chapter(render_store)
    unit = await create(render_store, chapter_id)
    store = PostgresRenderUnits(render_store.uow)

    started = await store.start_unit(
        unit_id=unit.id,
        workspace_id=WORKSPACE,
        expected_revision=1,
        job_id="job_1",
        generation_id="gen:abc",
    )
    assert (started.revision, started.status) == (2, "generating")
    assert started.generations[-1].job_id == "job_1"
    row = await stored(render_store, unit.id)
    assert (int(row["revision"]), row["status"]) == (2, "generating")

    with pytest.raises(RevisionConflictError):
        await store.start_unit(
            unit_id=unit.id,
            workspace_id=WORKSPACE,
            expected_revision=1,
            job_id="job_2",
            generation_id="gen:def",
        )
    with pytest.raises(ResourceNotFoundError):
        await store.start_unit(
            unit_id=unit.id,
            workspace_id="workspace-other",
            expected_revision=2,
            job_id="job_3",
            generation_id="gen:ghi",
        )


async def test_units_keep_independent_positions_inside_one_chapter(render_store: Store) -> None:
    _, chapter_id, _ = await chapter(render_store)
    first = await create(render_store, chapter_id, ordinal=0)
    second = await create(render_store, chapter_id, ordinal=1, key="render-request-key-0002")
    assert first.id != second.id
    assert (first.ordinal, second.ordinal) == (0, 1)
    assert first.dependencies.sha256 == second.dependencies.sha256


async def test_listing_units_pages_newest_first_and_hides_deleted_projects(
    render_store: Store, cleanup: Cleanup
) -> None:
    store = render_store
    project = await WorkbenchVersions(store.uow).create_project(
        workspace_id=WORKSPACE, name="单元列表项目", idempotency_key="unit-list-project-key"
    )
    books = WorkbenchBooks(PostgresBooks(store.uow))
    book = await books.create_book(
        project_id=project.id, workspace_id=WORKSPACE, title="合成有声书", key="unit-list-book-key"
    )
    first_chapter = await books.create_chapter(
        book_id=book.id,
        workspace_id=WORKSPACE,
        title="第一章",
        ordinal=0,
        text=CHAPTER_TEXT,
        key="unit-list-chapter-key",
    )
    created = [
        await create(store, first_chapter.id, key=f"unit-list-key-{index}", ordinal=index)
        for index in range(3)
    ]

    service = render_service(store)
    first = await service.list_units(chapter_id=first_chapter.id, workspace_id=WORKSPACE, limit=2)
    assert [item.id for item in first.items] == [created[2].id, created[1].id]
    assert first.next_position is not None and first.next_position.kind == "render_units"
    second = await service.list_units(
        chapter_id=first_chapter.id,
        workspace_id=WORKSPACE,
        limit=2,
        after=first.next_position,
    )
    assert [item.id for item in second.items] == [created[0].id]
    assert second.next_position is None
    # The listing carries the whole pinned unit, not just its identity.
    listed = second.items[0]
    assert listed.unit.id == created[0].id
    assert listed.unit.ordinal == 0 and listed.unit.dependencies.text == "合成旁白单元"
    assert listed.created_at is not None

    with pytest.raises(ResourceNotFoundError):
        await service.list_units(
            chapter_id=first_chapter.id, workspace_id=OTHER_WORKSPACE, limit=10
        )
    with pytest.raises(ResourceNotFoundError):
        await service.list_units(chapter_id="chapter-absent", workspace_id=WORKSPACE, limit=10)

    empty_chapter = await books.create_chapter(
        book_id=book.id,
        workspace_id=WORKSPACE,
        title="第二章",
        ordinal=1,
        text=CHAPTER_TEXT,
        key="unit-list-empty-chapter-key",
    )
    empty = await service.list_units(chapter_id=empty_chapter.id, workspace_id=WORKSPACE, limit=10)
    assert empty.items == () and empty.next_position is None

    # An accepted cascade tombstone hides the chapter's units from listing too.
    deletions = deletions_service(store)
    plan = await deletions.preview(
        workspace_id=WORKSPACE, target=target(project), mode="cascade", key="unit-list-plan-key"
    )
    await deletions.accept(
        workspace_id=WORKSPACE,
        plan_id=plan.id,
        expected_revision=1,
        digest=plan_digest(plan),
        targets=plan.confirmed_targets,
        key="unit-list-delete-key",
    )
    with pytest.raises(ResourceNotFoundError):
        await service.list_units(chapter_id=first_chapter.id, workspace_id=WORKSPACE, limit=10)


async def test_listing_units_carries_the_recorded_attempt(render_store: Store) -> None:
    store = render_store
    _, chapter_id, _ = await chapter(store)
    unit = await create(store, chapter_id, key="unit-attempt-key")

    # Nothing on this side writes a render attempt yet (dispatch belongs to the
    # speech pipeline), so the attempt is written directly against the real schema
    # and the listing read path is what this test exercises.
    async with await psycopg.AsyncConnection.connect(store.owner_dsn) as owner:
        await owner.execute(
            sql.SQL(
                "INSERT INTO {}.render_unit_generations"
                " (id, unit_id, job_id, dependencies_sha256, revision, created_at)"
                " VALUES (%s, %s, %s, %s, %s, now())"
            ).format(sql.Identifier(store.schema)),
            (
                "generation-synthetic-one",
                unit.id,
                "job-synthetic-one",
                unit.dependencies.sha256,
                1,
            ),
        )
        await owner.execute(
            sql.SQL("UPDATE {}.render_units SET status = %s WHERE id = %s").format(
                sql.Identifier(store.schema)
            ),
            ("generating", unit.id),
        )

    page = await render_service(store).list_units(
        chapter_id=chapter_id, workspace_id=WORKSPACE, limit=10
    )
    listed = page.items[0].unit
    assert listed.status == "generating"
    assert listed.generation is not None
    assert listed.generation.job_id == "job-synthetic-one"


async def recorded_result(store: Store, unit_id: str, *, receipt: str) -> None:
    """Record a produced result the way the future dispatch path will.

    Nothing on this side writes a render attempt yet, so the result is written
    directly against the real schema; the listing and read paths are under test.
    """
    async with await psycopg.AsyncConnection.connect(store.owner_dsn) as owner:
        row = await (
            await owner.execute(
                sql.SQL(
                    "SELECT dependencies_sha256, revision FROM {}.render_units WHERE id = %s"
                ).format(sql.Identifier(store.schema)),
                (unit_id,),
            )
        ).fetchone()
        assert row is not None
        digest, revision = row
        await owner.execute(
            sql.SQL(
                "INSERT INTO {}.render_unit_generations"
                " (id, unit_id, job_id, dependencies_sha256, revision, created_at)"
                " VALUES (%s, %s, %s, %s, %s, now())"
            ).format(sql.Identifier(store.schema)),
            (f"generation-{unit_id}", unit_id, f"job-{unit_id}", digest, revision),
        )
        await owner.execute(
            sql.SQL(
                "UPDATE {}.render_units SET status = %s, speech_job_id = %s,"
                " source_artifact_id = %s, source_receipt_id = %s WHERE id = %s"
            ).format(sql.Identifier(store.schema)),
            ("ready", f"job-{unit_id}", ARTIFACT, receipt, unit_id),
        )


async def copy_chapter(store: Store) -> tuple[str, str]:
    """A book chapter in the workspace the loopback artifact source serves."""
    project = await WorkbenchVersions(store.uow).create_project(
        workspace_id=SAVED_WORKSPACE, name="合成副本项目", idempotency_key="copy-project-key"
    )
    books = WorkbenchBooks(PostgresBooks(store.uow))
    book = await books.create_book(
        project_id=project.id, workspace_id=SAVED_WORKSPACE, title="合成有声书", key="copy-book-key"
    )
    made = await books.create_chapter(
        book_id=book.id,
        workspace_id=SAVED_WORKSPACE,
        title="第一章",
        ordinal=0,
        text=CHAPTER_TEXT,
        key="copy-chapter-key",
    )
    return project.id, made.id


async def test_a_unit_reports_only_its_own_projects_retained_copy(
    render_store: Store,
    objects: Objects,
    public_http: tuple[Any, PublicArtifactSource],
) -> None:
    _, source = public_http
    project_id, chapter_id = await copy_chapter(render_store)
    linked = await create(render_store, chapter_id, key="unit-copy-key", workspace=SAVED_WORKSPACE)
    unlinked = await create(
        render_store,
        chapter_id,
        key="unit-copy-other-key",
        ordinal=1,
        workspace=SAVED_WORKSPACE,
    )
    await recorded_result(render_store, linked.id, receipt=RECEIPT)
    await recorded_result(render_store, unlinked.id, receipt="receipt-other-one")

    copies = WorkbenchSavedAssets(PostgresSavedAssets(render_store.uow), source, objects.zone)
    # A second project holds a copy of the very same producer result.
    elsewhere = await WorkbenchVersions(render_store.uow).create_project(
        workspace_id=SAVED_WORKSPACE,
        name="别处项目",
        idempotency_key="unit-copy-elsewhere-project",
    )
    await copies.save(
        project_id=elsewhere.id,
        workspace_id=SAVED_WORKSPACE,
        source_artifact_id=ARTIFACT,
        source_receipt_id=RECEIPT,
        display_name="别处副本",
        key="unit-copy-elsewhere-key",
    )

    service = render_service(render_store)
    # Neither read path may see the other project's copy of the same result.
    leaked = await service.get_unit(unit_id=linked.id, workspace_id=SAVED_WORKSPACE)
    assert leaked.source is not None
    assert leaked.source.saved_asset_id is None
    assert render_unit_payload(leaked)["saved_asset_id"] is None

    before = await service.list_units(chapter_id=chapter_id, workspace_id=SAVED_WORKSPACE, limit=10)
    assert {
        item.unit.id: item.unit.source.saved_asset_id for item in before.items if item.unit.source
    } == {
        linked.id: None,
        unlinked.id: None,
    }

    asset = await copies.save(
        project_id=project_id,
        workspace_id=SAVED_WORKSPACE,
        source_artifact_id=ARTIFACT,
        source_receipt_id=RECEIPT,
        display_name="合成旁白副本",
        key="unit-copy-asset-key",
    )
    fetched = await service.get_unit(unit_id=linked.id, workspace_id=SAVED_WORKSPACE)
    assert fetched.source is not None
    assert fetched.source.saved_asset_id == asset.id
    payload = render_unit_payload(fetched)
    assert payload["saved_asset_id"] == asset.id
    # The served contract accepts the copy on the wire, not only in Python.
    envelope = {"body": payload, "headers": {"X-Request-ID": "request-demo"}}
    assert validate_response("getRenderUnitV2", 200, envelope) == envelope

    # The very same artifact under another receipt stays unlinked.
    other = await service.get_unit(unit_id=unlinked.id, workspace_id=SAVED_WORKSPACE)
    assert other.source is not None
    assert other.source.saved_asset_id is None
    assert render_unit_payload(other)["saved_asset_id"] is None

    after = await service.list_units(chapter_id=chapter_id, workspace_id=SAVED_WORKSPACE, limit=10)
    assert {
        item.unit.id: item.unit.source.saved_asset_id for item in after.items if item.unit.source
    } == {
        linked.id: asset.id,
        unlinked.id: None,
    }


async def test_a_restored_unit_stays_invisible_while_the_ledger_records_erasure(
    render_store: Store, cleanup: Cleanup
) -> None:
    """The same recovery drill for the render-unit read path."""
    store = render_store
    project = await WorkbenchVersions(store.uow).create_project(
        workspace_id=WORKSPACE, name="复原演练项目", idempotency_key="drill-project-key"
    )
    books = WorkbenchBooks(PostgresBooks(store.uow))
    book = await books.create_book(
        project_id=project.id, workspace_id=WORKSPACE, title="合成有声书", key="drill-book-key"
    )
    made = await books.create_chapter(
        book_id=book.id,
        workspace_id=WORKSPACE,
        title="第一章",
        ordinal=0,
        text=CHAPTER_TEXT,
        key="drill-chapter-key",
    )
    unit = await create(store, made.id, key="drill-unit-key")
    captured = await snapshot_rows(
        store, "projects", "project_versions", "books", "chapters", "render_units"
    )

    deletions = deletions_service(store)
    plan = await deletions.preview(
        workspace_id=WORKSPACE, target=target(project), mode="cascade", key="drill-plan-key"
    )
    record = await deletions.accept(
        workspace_id=WORKSPACE,
        plan_id=plan.id,
        expected_revision=1,
        digest=plan_digest(plan),
        targets=plan.confirmed_targets,
        key="drill-delete-key",
    )
    worker = PostgresDeletionCleanup(PostgresVersionUnitOfWork(cleanup.dsn, schema=store.schema))
    assert await worker.run(record.id) is not None

    await restore_rows(store, captured)
    assert record.id in await drop_tombstones(store)
    assert record.id in await ledger_entries(store)

    service = render_service(store)
    with pytest.raises(ResourceNotFoundError):
        await service.get_unit(unit_id=unit.id, workspace_id=WORKSPACE)
    with pytest.raises(ResourceNotFoundError):
        await service.list_units(chapter_id=made.id, workspace_id=WORKSPACE, limit=10)
