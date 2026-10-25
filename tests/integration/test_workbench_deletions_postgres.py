"""Deletion acceptance closes reads/writes/replays in the same real PG transaction."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import Any

import psycopg
import pytest
from psycopg import sql
from psycopg.types.json import Jsonb
from tests.integration.test_workbench_versions_postgres import Store
from tests.integration.test_workbench_versions_postgres import store as store

from speechnexus_backend.workbench.application.deletions import WorkbenchDeletions
from speechnexus_backend.workbench.application.versions import (
    IdempotencyConflictError,
    ResourceNotFoundError,
)
from speechnexus_backend.workbench.domain.deletion import (
    ConfirmationMismatchError,
    DeletionDependencyConflictError,
    DeletionPlan,
    DeletionPlanChangedError,
    DeletionRecord,
    DeletionTarget,
)
from speechnexus_backend.workbench.domain.versions import Project, ProjectChanges, Transcript
from speechnexus_backend.workbench.infrastructure.deletion_codec import plan_digest
from speechnexus_backend.workbench.infrastructure.postgres_deletions import (
    PostgresDeletionUnitOfWork,
    _DeletionSession,
)

pytestmark = pytest.mark.integration


def service(store: Store) -> WorkbenchDeletions:
    return WorkbenchDeletions(PostgresDeletionUnitOfWork(store.uow), digest_plan=plan_digest)


def target(project: Project) -> DeletionTarget:
    return DeletionTarget("workbench", "project", project.id, project.metadata_revision)


async def content(store: Store) -> tuple[Project, Transcript]:
    project = await store.app.create_project(
        workspace_id="workspace-synthetic", name="合成项目", idempotency_key="create-project"
    )
    text = await store.app.create_manual_transcript(
        project_id=project.id,
        display_name="合成稿件",
        text="必须清理的合成正文",
        idempotency_key="create-transcript",
    )
    return project, text


async def test_ordinary_blocks_children_but_cascade_tombstones_exact_confirmed_set(
    store: Store,
) -> None:
    project, text = await content(store)
    app = service(store)
    ordinary = await app.preview(
        workspace_id=project.workspace_id,
        target=target(project),
        mode="ordinary",
        key="ordinary-key",
    )
    assert not ordinary.eligible
    with pytest.raises(DeletionDependencyConflictError):
        await app.accept(
            workspace_id=project.workspace_id,
            plan_id=ordinary.id,
            expected_revision=1,
            digest=plan_digest(ordinary),
            targets=(target(project),),
            key="ordinary-delete",
        )
    cascade = await app.preview(
        workspace_id=project.workspace_id, target=target(project), mode="cascade", key="cascade-key"
    )
    with pytest.raises(ConfirmationMismatchError):
        await app.accept(
            workspace_id=project.workspace_id,
            plan_id=cascade.id,
            expected_revision=1,
            digest=plan_digest(cascade),
            targets=(target(project),),
            key="missing-confirmation",
        )
    record = await app.accept(
        workspace_id=project.workspace_id,
        plan_id=cascade.id,
        expected_revision=1,
        digest=plan_digest(cascade),
        targets=cascade.confirmed_targets,
        key="accepted-delete",
    )
    assert await app.get(project.workspace_id, record.id) == record
    for read in (
        store.app.get_project(project.id),
        store.app.get_transcript(text.id),
        store.app.get_revision(text.id, 1),
    ):
        with pytest.raises(ResourceNotFoundError):
            await read
    with pytest.raises(ResourceNotFoundError):
        await store.app.create_project(
            workspace_id=project.workspace_id, name="合成项目", idempotency_key="create-project"
        )
    with pytest.raises(ResourceNotFoundError):
        await store.app.create_manual_transcript(
            project_id=project.id,
            display_name="合成稿件",
            text="必须清理的合成正文",
            idempotency_key="create-transcript",
        )
    with pytest.raises(IdempotencyConflictError):
        await store.app.create_project(
            workspace_id=project.workspace_id, name="不同意图", idempotency_key="create-project"
        )


async def test_dependency_addition_and_revision_change_require_new_preview(store: Store) -> None:
    project, text = await content(store)
    app = service(store)
    plan = await app.preview(
        workspace_id=project.workspace_id, target=target(project), mode="cascade", key="preview-key"
    )
    await store.app.revise_transcript(transcript_id=text.id, expected_revision=1, text="新版本")
    with pytest.raises(DeletionPlanChangedError):
        await app.accept(
            workspace_id=project.workspace_id,
            plan_id=plan.id,
            expected_revision=1,
            digest=plan_digest(plan),
            targets=plan.confirmed_targets,
            key="delete-key",
        )
    assert await store.app.get_project(project.id) == project
    refreshed = await app.preview(
        workspace_id=project.workspace_id,
        target=target(project),
        mode="cascade",
        key="refreshed-preview",
    )
    await store.app.create_manual_transcript(
        project_id=project.id, display_name="新依赖", text="合成"
    )
    with pytest.raises(DeletionPlanChangedError):
        await app.accept(
            workspace_id=project.workspace_id,
            plan_id=refreshed.id,
            expected_revision=1,
            digest=plan_digest(refreshed),
            targets=refreshed.confirmed_targets,
            key="new-dependency-delete",
        )


async def test_accepted_replay_precedes_plan_lookup_and_recheck(
    store: Store,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, _ = await content(store)
    app = service(store)
    plan = await app.preview(
        workspace_id=project.workspace_id, target=target(project), mode="cascade", key="preview-key"
    )
    record = await app.accept(
        workspace_id=project.workspace_id,
        plan_id=plan.id,
        expected_revision=1,
        digest=plan_digest(plan),
        targets=plan.confirmed_targets,
        key="delete-key",
    )

    async def no_plan_read(*args: object, **kwargs: object) -> None:
        pytest.fail("accepted-key replay must not consult an expired or changed plan")

    monkeypatch.setattr(_DeletionSession, "get_plan", no_plan_read)
    assert (
        await app.accept(
            workspace_id=project.workspace_id,
            plan_id=plan.id,
            expected_revision=1,
            digest=plan_digest(plan),
            targets=tuple(reversed(plan.confirmed_targets)),
            key="delete-key",
        )
        == record
    )


async def test_failed_receipt_write_rolls_back_tombstones_intent_and_outbox(
    store: Store,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, text = await content(store)
    app = service(store)
    plan = await app.preview(
        workspace_id=project.workspace_id, target=target(project), mode="cascade", key="preview-key"
    )

    async def fail_receipt(*args: object, **kwargs: object) -> None:
        raise RuntimeError("synthetic receipt failure")

    with monkeypatch.context() as patch:
        patch.setattr(_DeletionSession, "save_receipt", fail_receipt)
        with pytest.raises(RuntimeError, match="synthetic"):
            await app.accept(
                workspace_id=project.workspace_id,
                plan_id=plan.id,
                expected_revision=1,
                digest=plan_digest(plan),
                targets=plan.confirmed_targets,
                key="delete-key",
            )
    assert await store.app.get_project(project.id) == project
    assert await store.app.get_transcript(text.id) == text
    # The same original key can then commit; no partial tombstone/intent survived.
    assert await app.accept(
        workspace_id=project.workspace_id,
        plan_id=plan.id,
        expected_revision=1,
        digest=plan_digest(plan),
        targets=plan.confirmed_targets,
        key="delete-key",
    )


async def test_parallel_deletion_intents_cannot_accept_same_resources_twice(store: Store) -> None:
    project, _ = await content(store)
    app = service(store)
    plan = await app.preview(
        workspace_id=project.workspace_id, target=target(project), mode="cascade", key="preview-key"
    )
    results = await asyncio.gather(
        *(
            app.accept(
                workspace_id=project.workspace_id,
                plan_id=plan.id,
                expected_revision=1,
                digest=plan_digest(plan),
                targets=plan.confirmed_targets,
                key=f"delete-{index}",
            )
            for index in range(3)
        ),
        return_exceptions=True,
    )
    assert sum(isinstance(result, DeletionRecord) for result in results) == 1
    assert sum(isinstance(result, DeletionPlanChangedError) for result in results) == 2


async def test_transcript_delete_keeps_project_and_checks_workspace(store: Store) -> None:
    project, text = await content(store)
    app = service(store)
    transcript_target = DeletionTarget("workbench", "transcript", text.id, 1)
    with pytest.raises(ResourceNotFoundError):
        await app.preview(
            workspace_id="workspace-other",
            target=transcript_target,
            mode="ordinary",
            key="preview-key",
        )
    with pytest.raises(DeletionPlanChangedError):
        await app.preview(
            workspace_id=project.workspace_id,
            target=replace(transcript_target, revision=2),
            mode="ordinary",
            key="stale-target",
        )
    plan = await app.preview(
        workspace_id=project.workspace_id,
        target=transcript_target,
        mode="ordinary",
        key="preview-key",
    )
    await app.accept(
        workspace_id=project.workspace_id,
        plan_id=plan.id,
        expected_revision=1,
        digest=plan_digest(plan),
        targets=plan.confirmed_targets,
        key="delete-key",
    )
    assert await store.app.get_project(project.id) == project
    with pytest.raises(ResourceNotFoundError):
        await store.app.edit_project(
            project_id=text.id, expected_revision=1, changes=ProjectChanges(name="不存在的项目")
        )


async def test_waiting_new_child_sees_tombstone_after_parent_lock_is_released(
    store: Store,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, _ = await content(store)
    app = service(store)
    plan = await app.preview(
        workspace_id=project.workspace_id, target=target(project), mode="cascade", key="plan-key"
    )
    accepted = asyncio.Event()
    release = asyncio.Event()
    original = _DeletionSession.accept

    async def hold(
        session: _DeletionSession, candidate: DeletionPlan, record: DeletionRecord
    ) -> None:
        await original(session, candidate, record)
        accepted.set()
        await release.wait()

    monkeypatch.setattr(_DeletionSession, "accept", hold)
    deleting = asyncio.create_task(
        app.accept(
            workspace_id=project.workspace_id,
            plan_id=plan.id,
            expected_revision=1,
            digest=plan_digest(plan),
            targets=plan.confirmed_targets,
            key="delete-key",
        )
    )
    writer = None
    try:
        await asyncio.wait_for(accepted.wait(), 2)
        writer = asyncio.create_task(
            store.app.create_manual_transcript(
                project_id=project.id, display_name="删除屏障之后", text="不能加入项目"
            )
        )
        async with await psycopg.AsyncConnection.connect(store.owner_dsn, autocommit=True) as owner:
            deadline = asyncio.get_running_loop().time() + 2
            while True:
                waiting = await (
                    await owner.execute(
                        "SELECT EXISTS (SELECT 1 FROM pg_stat_activity WHERE "
                        "application_name = 'speechnexus-workbench-versions' "
                        "AND cardinality(pg_blocking_pids(pid)) > 0)"
                    )
                ).fetchone()
                if waiting == (True,):
                    break
                assert asyncio.get_running_loop().time() < deadline, "no real parent lock wait"
                await asyncio.sleep(0.005)
        release.set()
        assert await deleting
        with pytest.raises(ResourceNotFoundError):
            await writer
    finally:
        release.set()
        await asyncio.gather(
            deleting, *([] if writer is None else [writer]), return_exceptions=True
        )


async def snapshot_rows(
    store: Store, *tables: str
) -> dict[str, tuple[list[tuple[str, int]], list[tuple[Any, ...]]]]:
    """Capture the rows a snapshot taken before the deletion would still hold."""
    captured: dict[str, tuple[list[tuple[str, int]], list[tuple[Any, ...]]]] = {}
    async with await psycopg.AsyncConnection.connect(store.owner_dsn) as owner:
        for table in tables:
            cursor = await owner.execute(
                sql.SQL("SELECT * FROM {}.{}").format(
                    sql.Identifier(store.schema), sql.Identifier(table)
                )
            )
            captured[table] = (
                [(item.name, item.type_code) for item in cursor.description or ()],
                list(await cursor.fetchall()),
            )
    return captured


# json/jsonb go back through Jsonb, while a real array must stay an array.
_JSON_TYPES = frozenset({114, 3802})


async def restore_rows(
    store: Store, captured: dict[str, tuple[list[tuple[str, int]], list[tuple[Any, ...]]]]
) -> None:
    """Put the captured rows back, exactly as restoring a snapshot would."""
    async with await psycopg.AsyncConnection.connect(store.owner_dsn) as owner:
        for table, (columns, rows) in captured.items():
            for row in rows:
                values = tuple(
                    Jsonb(value) if code in _JSON_TYPES and value is not None else value
                    for value, (_, code) in zip(row, columns, strict=True)
                )
                await owner.execute(
                    sql.SQL("INSERT INTO {}.{} ({}) VALUES ({}) ON CONFLICT DO NOTHING").format(
                        sql.Identifier(store.schema),
                        sql.Identifier(table),
                        sql.SQL(", ").join(sql.Identifier(name) for name, _ in columns),
                        sql.SQL(", ").join([sql.Placeholder()] * len(columns)),
                    ),
                    values,
                )


async def drop_tombstones(store: Store) -> list[str]:
    """A snapshot predating the deletion holds no tombstones for it either."""
    async with await psycopg.AsyncConnection.connect(store.owner_dsn) as owner:
        cursor = await owner.execute(
            sql.SQL("DELETE FROM {}.resource_tombstones RETURNING deletion_id").format(
                sql.Identifier(store.schema)
            )
        )
        return [row[0] for row in await cursor.fetchall()]


async def ledger_entries(store: Store) -> list[str]:
    """The durable evidence that survives a snapshot: the deletion ledger."""
    async with await psycopg.AsyncConnection.connect(store.owner_dsn) as owner:
        cursor = await owner.execute(
            sql.SQL("SELECT deletion_id FROM {}.deletion_ledger").format(
                sql.Identifier(store.schema)
            )
        )
        return [row[0] for row in await cursor.fetchall()]
