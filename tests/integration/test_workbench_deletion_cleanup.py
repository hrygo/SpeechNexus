"""Owned text erasure preserves only intent/fencing and deletion evidence."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo
from tests.integration.test_workbench_deletions_postgres import content, service, target
from tests.integration.test_workbench_deletions_postgres import drop_tombstones as drop_tombstones
from tests.integration.test_workbench_deletions_postgres import ledger_entries as ledger_entries
from tests.integration.test_workbench_deletions_postgres import restore_rows as restore_rows
from tests.integration.test_workbench_deletions_postgres import snapshot_rows as snapshot_rows
from tests.integration.test_workbench_versions_postgres import Store
from tests.integration.test_workbench_versions_postgres import store as store

from speechnexus_backend.workbench.application.versions import (
    IdempotencyConflictError,
    ResourceNotFoundError,
)
from speechnexus_backend.workbench.domain.deletion import DeletionTarget
from speechnexus_backend.workbench.infrastructure.deletion_codec import plan_digest
from speechnexus_backend.workbench.infrastructure.deletion_schema import grant_cleanup_runtime_sql
from speechnexus_backend.workbench.infrastructure.postgres_deletion_cleanup import (
    PostgresDeletionCleanup,
)
from speechnexus_backend.workbench.infrastructure.postgres_versions import (
    PostgresVersionUnitOfWork,
    _Session,
)

pytestmark = pytest.mark.integration


@dataclass
class Cleanup:
    worker: PostgresDeletionCleanup
    dsn: str


@pytest.fixture
async def cleanup(store: Store) -> AsyncIterator[Cleanup]:
    role = store.role + "_cleanup"
    async with await psycopg.AsyncConnection.connect(store.owner_dsn, autocommit=True) as owner:
        await owner.execute(sql.SQL("CREATE ROLE {} LOGIN").format(sql.Identifier(role)))
        await owner.execute(grant_cleanup_runtime_sql(store.schema, role))
        dsn = make_conninfo(store.owner_dsn, user=role)
        try:
            yield Cleanup(
                PostgresDeletionCleanup(PostgresVersionUnitOfWork(dsn, schema=store.schema)), dsn
            )
        finally:
            await owner.execute(sql.SQL("DROP OWNED BY {}").format(sql.Identifier(role)))
            await owner.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(role)))


async def test_cleanup_erases_all_confirmed_text_keeps_key_identity_and_is_idempotent(
    store: Store,
    cleanup: Cleanup,
) -> None:
    project, text = await content(store)
    corrected = await store.app.revise_transcript(
        transcript_id=text.id,
        expected_revision=1,
        text="合成校对内容",
        idempotency_key="correct-key",
    )
    app = service(store)
    plan = await app.preview(
        workspace_id=project.workspace_id, target=target(project), mode="cascade", key="plan-key"
    )
    record = await app.accept(
        workspace_id=project.workspace_id,
        plan_id=plan.id,
        expected_revision=1,
        digest=plan_digest(plan),
        targets=plan.confirmed_targets,
        key="delete-key",
    )
    result = await cleanup.worker.run(record.id)
    assert result is not None
    assert set(result.deleted) == set(plan.confirmed_targets)
    assert not result.retained_independent_copies
    assert await cleanup.worker.run(record.id) == result
    assert (await app.get(project.workspace_id, record.id)).result == result
    # Accepted intent replays its original queued identity, never creates another cleanup.
    assert (
        await app.accept(
            workspace_id=project.workspace_id,
            plan_id=plan.id,
            expected_revision=1,
            digest=plan_digest(plan),
            targets=plan.confirmed_targets,
            key="delete-key",
        )
        == record
    )
    with pytest.raises(ResourceNotFoundError):
        await store.app.revise_transcript(
            transcript_id=text.id,
            expected_revision=1,
            text=corrected.head.text,
            idempotency_key="correct-key",
        )
    with pytest.raises(ResourceNotFoundError):
        await store.app.create_project(
            workspace_id=project.workspace_id, name="合成项目", idempotency_key="create-project"
        )
    async with await psycopg.AsyncConnection.connect(store.owner_dsn) as owner:
        for table in (
            "projects",
            "project_versions",
            "transcripts",
            "transcript_revisions",
            "mutation_receipts",
        ):
            row = await (
                await owner.execute(
                    sql.SQL("SELECT count(*) FROM {}.{}").format(
                        sql.Identifier(store.schema), sql.Identifier(table)
                    )
                )
            ).fetchone()
            assert row == (0,), table
        row = await (
            await owner.execute(
                sql.SQL("SELECT count(*) FROM {}.erased_mutation_receipts").format(
                    sql.Identifier(store.schema)
                )
            )
        ).fetchone()
        assert row == (3,)
        row = await (
            await owner.execute(
                sql.SQL("SELECT count(*) FROM {}.deletion_ledger").format(
                    sql.Identifier(store.schema)
                )
            )
        ).fetchone()
        assert row == (1,)


async def test_two_cleanup_workers_do_not_duplicate_completion(
    store: Store, cleanup: Cleanup
) -> None:
    project, _ = await content(store)
    app = service(store)
    plan = await app.preview(
        workspace_id=project.workspace_id, target=target(project), mode="cascade", key="plan-key"
    )
    record = await app.accept(
        workspace_id=project.workspace_id,
        plan_id=plan.id,
        expected_revision=1,
        digest=plan_digest(plan),
        targets=plan.confirmed_targets,
        key="delete-key",
    )
    a, b = await asyncio.gather(cleanup.worker.run(record.id), cleanup.worker.run(record.id))
    assert a == b and a is not None
    assert await cleanup.worker.run("nonexistent-deletion") is None


async def test_cleanup_role_cannot_erase_tombstones_or_ledger(
    store: Store, cleanup: Cleanup
) -> None:
    async with await psycopg.AsyncConnection.connect(cleanup.dsn, autocommit=True) as conn:
        for table in (
            "resource_tombstones",
            "erased_mutation_receipts",
            "deletion_ledger",
            "deletion_records",
            "deletion_receipts",
            "deletion_plans",
            "deletion_outbox",
        ):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                await conn.execute(
                    sql.SQL("DELETE FROM {}.{}").format(
                        sql.Identifier(store.schema), sql.Identifier(table)
                    )
                )
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            await conn.execute(
                sql.SQL("CREATE TABLE {}.forbidden(id text)").format(sql.Identifier(store.schema))
            )


async def test_failure_after_content_erasure_rolls_back_all_rows_and_completion(
    store: Store,
    cleanup: Cleanup,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, text = await content(store)
    app = service(store)
    plan = await app.preview(
        workspace_id=project.workspace_id, target=target(project), mode="cascade", key="plan-key"
    )
    record = await app.accept(
        workspace_id=project.workspace_id,
        plan_id=plan.id,
        expected_revision=1,
        digest=plan_digest(plan),
        targets=plan.confirmed_targets,
        key="delete-key",
    )
    original = _Session._execute

    async def fail_ledger(session: _Session, query: str, params: Sequence[object] = ()) -> None:
        if "INSERT INTO {s}.deletion_ledger" in query:
            raise RuntimeError("synthetic completion evidence failure")
        await original(session, query, params)

    with monkeypatch.context() as patch:
        patch.setattr(_Session, "_execute", fail_ledger)
        with pytest.raises(RuntimeError, match="synthetic"):
            await cleanup.worker.run(record.id)
    assert (await app.get(project.workspace_id, record.id)).result is None
    async with await psycopg.AsyncConnection.connect(store.owner_dsn) as owner:
        row = await (
            await owner.execute(
                sql.SQL("SELECT text FROM {}.transcript_revisions WHERE transcript_id = %s").format(
                    sql.Identifier(store.schema)
                ),
                (text.id,),
            )
        ).fetchone()
        assert row == (text.head.text,)
        row = await (
            await owner.execute(
                sql.SQL("SELECT count(*) FROM {}.erased_mutation_receipts").format(
                    sql.Identifier(store.schema)
                )
            )
        ).fetchone()
        assert row == (0,)
    assert await cleanup.worker.run(record.id)


async def test_pending_child_erasure_blocks_parent_until_its_own_cleanup_ends(
    store: Store,
    cleanup: Cleanup,
) -> None:
    project, text = await content(store)
    app = service(store)
    child = await app.preview(
        workspace_id=project.workspace_id,
        target=DeletionTarget("workbench", "transcript", text.id, 1),
        mode="ordinary",
        key="child-plan",
    )
    record = await app.accept(
        workspace_id=project.workspace_id,
        plan_id=child.id,
        expected_revision=1,
        digest=plan_digest(child),
        targets=child.confirmed_targets,
        key="child-delete",
    )
    blocked = await app.preview(
        workspace_id=project.workspace_id,
        target=target(project),
        mode="cascade",
        key="parent-plan-pending",
    )
    assert not blocked.eligible
    assert await cleanup.worker.run(record.id)
    ready = await app.preview(
        workspace_id=project.workspace_id,
        target=target(project),
        mode="cascade",
        key="parent-plan-ready",
    )
    assert ready.eligible and not ready.dependencies
    assert await store.app.get_project(project.id) == project


@pytest.mark.parametrize("changed_intent", [False, True])
async def test_original_key_cannot_recreate_project_while_receipt_is_being_erased(
    store: Store,
    cleanup: Cleanup,
    monkeypatch: pytest.MonkeyPatch,
    changed_intent: bool,
) -> None:
    project, _ = await content(store)
    app = service(store)
    plan = await app.preview(
        workspace_id=project.workspace_id, target=target(project), mode="cascade", key="plan-key"
    )
    record = await app.accept(
        workspace_id=project.workspace_id,
        plan_id=plan.id,
        expected_revision=1,
        digest=plan_digest(plan),
        targets=plan.confirmed_targets,
        key="delete-key",
    )
    read_receipt = asyncio.Event()
    resume = asyncio.Event()
    original = _Session._one

    async def pause[T](
        session: _Session, factory: type[T], query: str, params: Sequence[object] = ()
    ) -> T | None:
        row = await original(session, factory, query, params)
        if "FROM {s}.erased_mutation_receipts" in query and not read_receipt.is_set():
            read_receipt.set()
            await resume.wait()
        return row

    monkeypatch.setattr(_Session, "_one", pause)
    replay = asyncio.create_task(
        store.app.create_project(
            workspace_id=project.workspace_id,
            name="不同合成意图" if changed_intent else "合成项目",
            idempotency_key="create-project",
        )
    )
    try:
        await asyncio.wait_for(read_receipt.wait(), 2)
        assert await cleanup.worker.run(record.id)
        resume.set()
        with pytest.raises(IdempotencyConflictError if changed_intent else ResourceNotFoundError):
            await replay
    finally:
        resume.set()
        await asyncio.gather(replay, return_exceptions=True)


async def test_a_restored_transcript_stays_invisible_while_the_ledger_records_erasure(
    store: Store, cleanup: Cleanup
) -> None:
    """The drill for a transcript-level deletion, whose tombstone kind is its own."""
    project, text = await content(store)
    app = service(store)
    captured = await snapshot_rows(store, "transcripts", "transcript_revisions")
    plan = await app.preview(
        workspace_id=project.workspace_id,
        target=DeletionTarget("workbench", "transcript", text.id, 1),
        mode="ordinary",
        key="drill-transcript-plan-key",
    )
    record = await app.accept(
        workspace_id=project.workspace_id,
        plan_id=plan.id,
        expected_revision=1,
        digest=plan_digest(plan),
        targets=plan.confirmed_targets,
        key="drill-transcript-delete-key",
    )
    assert await cleanup.worker.run(record.id) is not None

    # The snapshot predates the deletion: its rows return, its tombstones do not.
    await restore_rows(store, captured)
    assert record.id in await drop_tombstones(store)
    assert record.id in await ledger_entries(store)

    page = await store.app.list_transcripts(project_id=project.id)
    assert page.items == ()
    with pytest.raises(ResourceNotFoundError):
        await store.app.get_transcript(text.id)
