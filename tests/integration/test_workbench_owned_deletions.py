"""Full owned catalog pins every project child before accepting erasure."""

from __future__ import annotations

import io
from collections.abc import AsyncIterator
from typing import BinaryIO

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo
from tests.integration.test_workbench_exports_postgres import WORKSPACE, book, create
from tests.integration.test_workbench_recovery_postgres import Probe
from tests.integration.test_workbench_recovery_postgres import probe as probe
from tests.integration.test_workbench_saved_assets_cleanup import BUCKET, Objects
from tests.integration.test_workbench_saved_assets_cleanup import objects as objects
from tests.integration.test_workbench_versions_postgres import Store

from speechnexus_backend.workbench.application.books import WorkbenchBooks
from speechnexus_backend.workbench.application.deletions import WorkbenchDeletions
from speechnexus_backend.workbench.domain.deletion import (
    DeletionDependencyConflictError,
    DeletionMode,
    DeletionPlan,
    DeletionPlanChangedError,
    DeletionRecord,
    DeletionTarget,
)
from speechnexus_backend.workbench.infrastructure.deletion_codec import plan_digest
from speechnexus_backend.workbench.infrastructure.postgres_books import PostgresBooks
from speechnexus_backend.workbench.infrastructure.postgres_content_writes import (
    PostgresContentWrites,
)
from speechnexus_backend.workbench.infrastructure.postgres_deletion_cleanup import (
    PostgresDeletionCleanup,
)
from speechnexus_backend.workbench.infrastructure.postgres_deletions import (
    PostgresDeletionUnitOfWork,
)
from speechnexus_backend.workbench.infrastructure.postgres_exports import PostgresExports
from speechnexus_backend.workbench.infrastructure.postgres_owned_deletions import (
    PostgresOwnedDeletionCatalog,
)
from speechnexus_backend.workbench.infrastructure.postgres_versions import PostgresVersionUnitOfWork

pytestmark = pytest.mark.integration


@pytest.fixture
async def owned_store(probe: Probe) -> AsyncIterator[Store]:
    yield Store(
        probe.owner_dsn,
        probe.schema,
        probe.role,
        PostgresVersionUnitOfWork(
            make_conninfo(probe.owner_dsn, user=probe.role), schema=probe.schema
        ),
    )


def deletions(store: Store) -> WorkbenchDeletions:
    return WorkbenchDeletions(
        PostgresDeletionUnitOfWork(store.uow, meetings=True, owned=PostgresOwnedDeletionCatalog()),
        digest_plan=plan_digest,
    )


async def preview(store: Store, project_id: str, *, mode: DeletionMode = "cascade") -> DeletionPlan:
    return await deletions(store).preview(
        workspace_id=WORKSPACE,
        target=DeletionTarget("workbench", "project", project_id, 1),
        mode=mode,
        key="owned-plan-key",
    )


async def accept(store: Store, plan: DeletionPlan) -> DeletionRecord:
    return await deletions(store).accept(
        workspace_id=WORKSPACE,
        plan_id=plan.id,
        expected_revision=1,
        digest=plan_digest(plan),
        targets=plan.confirmed_targets,
        key="owned-delete-key",
    )


async def insert_asset(store: Store, project_id: str, asset_id: str) -> None:
    async with await psycopg.AsyncConnection.connect(store.owner_dsn) as owner:
        await owner.execute(
            sql.SQL(
                "INSERT INTO {}.saved_assets (id,project_id,media_type,bytes,sha256,"
                "source_artifact_id,source_receipt_id,object_key,created_at)"
                " VALUES (%s,%s,'audio/wav',4,%s,'source_owned','receipt_owned',"
                " %s,clock_timestamp())"
            ).format(sql.Identifier(store.schema)),
            (asset_id, project_id, "sha256:" + "a" * 64, project_id + "/" + "a" * 64),
        )


async def test_ordinary_project_delete_blocks_owned_book_and_saved_asset(
    owned_store: Store,
) -> None:
    project_id, book_id, _ = await book(owned_store, suffix="owned", units=1)
    await insert_asset(owned_store, project_id, "asset_owned")
    plan = await preview(owned_store, project_id, mode="ordinary")
    assert {(d.target.kind, d.target.id, d.target.revision) for d in plan.dependencies} == {
        ("audiobook", book_id, 1),
        ("saved_asset", "asset_owned", 1),
    }
    assert not plan.eligible
    with pytest.raises(DeletionDependencyConflictError):
        await accept(owned_store, plan)


async def test_new_chapter_invalidates_plan_even_with_unchanged_book_revision(
    owned_store: Store,
) -> None:
    project_id, book_id, _ = await book(owned_store, suffix="chapter", units=1)
    plan = await preview(owned_store, project_id)
    await WorkbenchBooks(PostgresBooks(owned_store.uow)).create_chapter(
        workspace_id=WORKSPACE, book_id=book_id, title="第二章", ordinal=1, text="合成新增章节"
    )
    with pytest.raises(DeletionPlanChangedError):
        await accept(owned_store, plan)


@pytest.mark.parametrize("kind", ["book", "asset", "export"])
async def test_new_owned_resource_invalidates_previous_confirmation(
    owned_store: Store, kind: str
) -> None:
    project_id, book_id, _ = await book(owned_store, suffix="new-owned", units=1)
    plan = await preview(owned_store, project_id)
    if kind == "book":
        await WorkbenchBooks(PostgresBooks(owned_store.uow)).create_book(
            project_id=project_id, workspace_id=WORKSPACE, title="新书"
        )
    elif kind == "asset":
        await insert_asset(owned_store, project_id, "asset_late")
    else:
        await create(owned_store, book_id)
    with pytest.raises(DeletionPlanChangedError):
        await accept(owned_store, plan)


async def test_render_revision_change_invalidates_private_plan_snapshot(owned_store: Store) -> None:
    project_id, _, units = await book(owned_store, suffix="unit", units=1)
    plan = await preview(owned_store, project_id)
    async with await psycopg.AsyncConnection.connect(owned_store.owner_dsn) as owner:
        await owner.execute(
            sql.SQL("UPDATE {}.render_units SET revision=revision+1 WHERE id=%s").format(
                sql.Identifier(owned_store.schema)
            ),
            (units[0],),
        )
    with pytest.raises(DeletionPlanChangedError):
        await accept(owned_store, plan)


@pytest.mark.parametrize("status", ["queued", "running", "cancelling"])
async def test_active_export_blocks_project_erasure(owned_store: Store, status: str) -> None:
    project_id, book_id, _ = await book(owned_store, suffix="active", units=1)
    export = await create(owned_store, book_id)
    if status != "queued":
        async with await psycopg.AsyncConnection.connect(owned_store.owner_dsn) as owner:
            await owner.execute(
                sql.SQL("UPDATE {}.export_jobs SET status=%s WHERE id=%s").format(
                    sql.Identifier(owned_store.schema)
                ),
                (status, export.id),
            )
    plan = await preview(owned_store, project_id)
    assert any(
        d.relation == "active_execution" and d.disposition == "blocks" for d in plan.dependencies
    )
    with pytest.raises(DeletionDependencyConflictError):
        await accept(owned_store, plan)


@pytest.mark.parametrize("status", ["generating", "stale"])
async def test_generation_without_stop_evidence_blocks_even_after_unit_edit(
    owned_store: Store, status: str
) -> None:
    project_id, _, units = await book(owned_store, suffix="generation", units=1)
    async with await psycopg.AsyncConnection.connect(owned_store.owner_dsn) as owner:
        await owner.execute(
            sql.SQL("UPDATE {}.render_units SET status=%s,revision=2 WHERE id=%s").format(
                sql.Identifier(owned_store.schema)
            ),
            (status, units[0]),
        )
        await owner.execute(
            sql.SQL(
                "INSERT INTO {s}.render_unit_generations "
                "(id,unit_id,job_id,dependencies_sha256,revision,created_at) "
                "SELECT 'generation_unknown',id,'speech_unknown',dependencies_sha256,"
                "revision,clock_timestamp() FROM {s}.render_units WHERE id=%s"
            ).format(s=sql.Identifier(owned_store.schema)),
            (units[0],),
        )
    plan = await preview(owned_store, project_id)
    assert not plan.eligible
    assert any(d.relation == "active_execution" for d in plan.dependencies)


async def test_reserved_content_write_blocks_project_acceptance(owned_store: Store) -> None:
    project_id, _, _ = await book(owned_store, suffix="write", units=1)
    await PostgresContentWrites(owned_store.uow).reserve_asset(
        project_id=project_id,
        workspace_id=WORKSPACE,
        asset_id="asset_reserved",
        source_artifact_id="source_owned",
        source_receipt_id="receipt_owned",
        display_name=None,
        key="reserved-copy-key",
        intent_digest="a" * 64,
    )
    plan = await preview(owned_store, project_id)
    assert any(
        d.target.kind == "project"
        and d.relation == "active_execution"
        and d.disposition == "blocks"
        for d in plan.dependencies
    )
    with pytest.raises(DeletionDependencyConflictError):
        await accept(owned_store, plan)


@pytest.mark.parametrize("change", ["version", "tombstone"])
async def test_cleanup_refuses_changed_owned_version_or_missing_confirmation_fence(
    owned_store: Store, probe: Probe, change: str
) -> None:
    project_id, book_id, _ = await book(owned_store, suffix="cleanup-guard", units=1)
    plan = await preview(owned_store, project_id)
    record = await accept(owned_store, plan)
    async with await psycopg.AsyncConnection.connect(owned_store.owner_dsn) as owner:
        if change == "version":
            await owner.execute(
                sql.SQL(
                    "UPDATE {}.books SET casting_revision=casting_revision+1 WHERE id=%s"
                ).format(sql.Identifier(owned_store.schema)),
                (book_id,),
            )
        else:
            await owner.execute(
                sql.SQL(
                    "DELETE FROM {}.resource_tombstones WHERE resource_kind='audiobook' "
                    "AND resource_id=%s"
                ).format(sql.Identifier(owned_store.schema)),
                (book_id,),
            )
    worker = PostgresDeletionCleanup(
        PostgresVersionUnitOfWork(probe.cleanup_dsn, schema=probe.schema),
        meetings=True,
        owned=PostgresOwnedDeletionCatalog(),
    )
    with pytest.raises(ValueError):
        await worker.run(record.id)
    async with await psycopg.AsyncConnection.connect(owned_store.owner_dsn) as owner:
        row = await (
            await owner.execute(
                sql.SQL("SELECT count(*) FROM {}.books WHERE id=%s").format(
                    sql.Identifier(owned_store.schema)
                ),
                (book_id,),
            )
        ).fetchone()
    assert row == (1,)


async def test_cleanup_erases_only_pinned_legacy_export_prefixes(
    owned_store: Store, probe: Probe
) -> None:
    project_id, book_id, _ = await book(owned_store, suffix="legacy", units=1)
    export = await create(owned_store, book_id)
    exports = PostgresExports(owned_store.uow)
    await exports.cancel_export(export_id=export.id, workspace_id=WORKSPACE)
    await exports.settle_cancel_export(export_id=export.id)
    plan = await preview(owned_store, project_id)
    assert any(d.target.kind == "audiobook" for d in plan.dependencies)
    record = await accept(owned_store, plan)

    class Objects:
        def __init__(self) -> None:
            self.prefixes: list[str] = []

        async def put(self, *, key: str, body: BinaryIO, length: int) -> None:
            raise AssertionError("cleanup cannot write content")

        async def erase_prefix(self, *, prefix: str) -> int:
            self.prefixes.append(prefix)
            return 0

    objects = Objects()
    worker = PostgresDeletionCleanup(
        PostgresVersionUnitOfWork(probe.cleanup_dsn, schema=probe.schema),
        meetings=True,
        objects=objects,
        owned=PostgresOwnedDeletionCatalog(),
    )
    result = await worker.run(record.id)
    assert result is not None and set(result.deleted) == set(plan.confirmed_targets)
    assert set(objects.prefixes) == {project_id + "/", book_id + "/" + export.id + "/"}
    assert book_id + "/" not in objects.prefixes
    assert await worker.pending(limit=10) == ()


async def test_full_cleanup_preserves_other_project_copy_and_unconfirmed_legacy_job(
    owned_store: Store, probe: Probe, objects: Objects
) -> None:
    project_id, book_id, _ = await book(owned_store, suffix="delete-copy", units=1)
    other_project, _, _ = await book(owned_store, suffix="retain-copy", units=1)
    await insert_asset(owned_store, project_id, "asset_erased")
    await insert_asset(owned_store, other_project, "asset_retained")
    export = await create(owned_store, book_id)
    exports = PostgresExports(owned_store.uow)
    await exports.cancel_export(export_id=export.id, workspace_id=WORKSPACE)
    await exports.settle_cancel_export(export_id=export.id)
    erased_keys = [project_id + "/" + "a" * 64, f"{book_id}/{export.id}/old-artifact"]
    retained_keys = [other_project + "/" + "a" * 64, f"{book_id}/independent-job/other-artifact"]
    for key in erased_keys + retained_keys:
        await objects.zone.put(key=key, body=io.BytesIO(b"synthetic"), length=9)
    plan = await preview(owned_store, project_id)
    record = await accept(owned_store, plan)
    worker = PostgresDeletionCleanup(
        PostgresVersionUnitOfWork(probe.cleanup_dsn, schema=probe.schema),
        meetings=True,
        objects=objects.zone,
        owned=PostgresOwnedDeletionCatalog(),
    )
    try:
        assert await worker.run(record.id) is not None
        names = {item.object_name for item in objects.client.list_objects(BUCKET, recursive=True)}
        assert not {"saved/" + key for key in erased_keys} & names
        assert {"saved/" + key for key in retained_keys} <= names
        async with await psycopg.AsyncConnection.connect(owned_store.owner_dsn) as owner:
            row = await (
                await owner.execute(
                    sql.SQL("SELECT id FROM {}.saved_assets ORDER BY id").format(
                        sql.Identifier(owned_store.schema)
                    )
                )
            ).fetchall()
        assert row == [("asset_retained",)]
    finally:
        for key in erased_keys + retained_keys:
            objects.client.remove_object(BUCKET, "saved/" + key)
