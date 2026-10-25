"""Real durable task transactions on the isolated PG probe; LLM is synthetic."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from typing import Literal

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo
from tests.integration.test_workbench_deletion_cleanup import Cleanup
from tests.integration.test_workbench_deletion_cleanup import cleanup as cleanup
from tests.integration.test_workbench_deletions_postgres import (
    drop_tombstones as drop_tombstones,
)
from tests.integration.test_workbench_deletions_postgres import (
    ledger_entries as ledger_entries,
)
from tests.integration.test_workbench_deletions_postgres import restore_rows as restore_rows
from tests.integration.test_workbench_deletions_postgres import snapshot_rows as snapshot_rows
from tests.integration.test_workbench_listings_postgres import delete
from tests.integration.test_workbench_minutes_postgres import assert_no_runtime_connection
from tests.integration.test_workbench_sealing_postgres import BODY, content, upgrade
from tests.integration.test_workbench_versions_postgres import Store
from tests.integration.test_workbench_versions_postgres import store as store

from speechnexus_backend.workbench.application.llm import (
    LlmCompletion,
    LlmError,
    LlmProfile,
    LlmRequest,
    LlmUsage,
)
from speechnexus_backend.workbench.application.minutes_tasks import MinutesTasks, MinutesTaskView
from speechnexus_backend.workbench.application.versions import (
    IdempotencyConflictError,
    ResourceNotFoundError,
)
from speechnexus_backend.workbench.domain.deletion import DeletionTarget
from speechnexus_backend.workbench.domain.minutes_tasks import MinutesTaskConflictError
from speechnexus_backend.workbench.infrastructure.minutes_tasks_schema import (
    grant_minutes_cleanup_sql,
    grant_minutes_runtime_sql,
    minutes_tasks_migration_sql,
)
from speechnexus_backend.workbench.infrastructure.postgres_minutes_tasks import PostgresMinutesTasks
from speechnexus_backend.workbench.infrastructure.postgres_versions import VersionStoreError
from speechnexus_backend.workbench.runtime import _dispatch_minutes_once

pytestmark = pytest.mark.integration
PROFILE = LlmProfile(model="synthetic-model", template_revision=1, system_prompt="合成纪要模板")
BINDING = "sha256:" + "b" * 64


class Llm:
    profile = PROFILE
    binding_id = BINDING

    def __init__(self, store: Store) -> None:
        self.store = store
        self.calls: list[LlmRequest] = []
        self.entered, self.release, self.cancelled = (
            asyncio.Event(),
            asyncio.Event(),
            asyncio.Event(),
        )
        self.release.set()
        self.failure: Exception | None = None

    async def generate(self, request: LlmRequest) -> LlmCompletion:
        self.calls.append(request)
        await assert_no_runtime_connection(self.store)
        self.entered.set()
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.cancelled.set()
            raise
        if self.failure is not None:
            raise self.failure
        return LlmCompletion(PROFILE, "合成纪要候选", LlmUsage(10, 5))


@pytest.fixture
async def tasks(store: Store) -> PostgresMinutesTasks:
    await upgrade(store)
    async with await psycopg.AsyncConnection.connect(store.owner_dsn) as owner:
        await owner.execute(minutes_tasks_migration_sql(store.schema))
        await owner.execute(grant_minutes_runtime_sql(store.schema, store.role))
    return PostgresMinutesTasks(store.uow)


async def enqueue(store: Store, service: MinutesTasks, *, key: str = "create") -> MinutesTaskView:
    _, original = await content(store)
    await store.app.seal_transcript(transcript_id=original.id, expected_revision=1)
    return await service.enqueue(
        workspace_id="w", transcript_id=original.id, source_revision=2, template_revision=1, key=key
    )


async def test_same_key_concurrent_creation_claim_once_and_restart_query(
    store: Store, tasks: PostgresMinutesTasks
) -> None:
    llm = Llm(store)
    service = MinutesTasks(tasks, llm)
    initial = await enqueue(store, service)
    repeats = await asyncio.gather(
        *(
            service.enqueue(
                workspace_id="w",
                transcript_id=initial.task.transcript_id,
                source_revision=2,
                template_revision=1,
                key="create",
            )
            for _ in range(4)
        )
    )
    assert all(item == initial for item in repeats)
    assert await service.queued_ids("w") == (initial.task.id,)
    assert await service.queued_ids("other") == ()
    claims = await asyncio.gather(
        *(tasks.claim(initial.task.id, profile=PROFILE, binding_id=BINDING) for _ in range(3)),
        return_exceptions=True,
    )
    assert sum(isinstance(item, MinutesTaskConflictError) for item in claims) == 2
    # A new process/store can see running and cannot automatically redispatch it.
    restarted = MinutesTasks(PostgresMinutesTasks(store.uow), llm)
    assert (await restarted.get(initial.task.id)).task.status == "running"
    with pytest.raises(MinutesTaskConflictError):
        await restarted.execute(initial.task.id)
    assert not llm.calls


async def test_generation_publishes_once_and_source_revision_is_unchanged(
    store: Store, tasks: PostgresMinutesTasks
) -> None:
    llm = Llm(store)
    service = MinutesTasks(tasks, llm)
    initial = await enqueue(store, service)
    before = await store.app.get_transcript(initial.task.transcript_id)
    final = await service.execute(initial.task.id)
    assert final.task.status == "succeeded" and final.task.text == "合成纪要候选"
    assert (await service.execute(initial.task.id)) == final
    assert await service.get(initial.task.id) == final and len(llm.calls) == 1
    assert llm.calls[0].text == BODY and await store.app.get_transcript(before.id) == before
    rows = await tasks.attempts(initial.task.id)
    assert len(rows) == 1 and rows[0].usage == LlmUsage(10, 5) and rows[0].outcome == "succeeded"
    assert rows[0].completion_confirmed and rows[0].finished_at is not None


async def test_failure_retry_receipt_pins_original_profile_and_attempts(
    store: Store, tasks: PostgresMinutesTasks
) -> None:
    llm = Llm(store)
    service = MinutesTasks(tasks, llm)
    initial = await enqueue(store, service)
    llm.failure = LlmError("unavailable")
    failed = await service.execute(initial.task.id)
    assert failed.task.status == "failed" and failed.task.error == "unavailable"
    assert (await tasks.attempts(initial.task.id))[0].usage is None
    retried = await service.retry(initial.task.id, expected_attempt=1, key="retry")
    assert retried.task.status == "queued" and retried.profile == initial.profile
    assert await service.retry(initial.task.id, expected_attempt=1, key="retry") == retried
    with pytest.raises(IdempotencyConflictError):
        await service.retry(initial.task.id, expected_attempt=2, key="retry")
    llm.failure = None
    final = await service.execute(initial.task.id)
    assert final.task.status == "succeeded" and final.task.attempt == 2
    assert await service.retry(initial.task.id, expected_attempt=1, key="retry") == final
    assert [row.outcome for row in await tasks.attempts(initial.task.id)] == ["failed", "succeeded"]
    assert len(llm.calls) == 2 and llm.calls[0] == llm.calls[1]


async def test_queued_and_running_cancel_drops_body_and_interrupts_client(
    store: Store, tasks: PostgresMinutesTasks
) -> None:
    llm = Llm(store)
    service = MinutesTasks(tasks, llm, poll_seconds=0.01)
    initial = await enqueue(store, service)
    await service.cancel(initial.task.id)
    assert (await service.execute(initial.task.id)).task.status == "cancelled" and not llm.calls
    second = await service.enqueue(
        workspace_id="w",
        transcript_id=initial.task.transcript_id,
        source_revision=2,
        template_revision=1,
        key="second",
    )
    llm.release.clear()
    running = asyncio.create_task(service.execute(second.task.id))
    try:
        await asyncio.wait_for(llm.entered.wait(), 2)
        assert (await service.cancel(second.task.id)).task.status == "cancelled"
        result = await asyncio.wait_for(running, 2)
        assert (
            result.task.status == "cancelled"
            and result.task.text is None
            and llm.cancelled.is_set()
        )
        attempts = await tasks.attempts(second.task.id)
        assert attempts[0].outcome == "interrupted" and not attempts[0].completion_confirmed
    finally:
        if not running.done():
            running.cancel()
            await asyncio.gather(running, return_exceptions=True)


async def test_external_worker_cancel_records_interruption_without_requeue(
    store: Store, tasks: PostgresMinutesTasks
) -> None:
    llm = Llm(store)
    service = MinutesTasks(tasks, llm)
    initial = await enqueue(store, service)
    llm.release.clear()
    running = asyncio.create_task(service.execute(initial.task.id))
    await asyncio.wait_for(llm.entered.wait(), 2)
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running
    saved = await service.get(initial.task.id)
    assert saved.task.status == "failed" and saved.task.error == "interrupted"
    assert not await service.queued_ids("w") and len(llm.calls) == 1


async def test_source_revision_change_expires_saved_candidate_and_original_key_replay(
    store: Store, tasks: PostgresMinutesTasks
) -> None:
    service = MinutesTasks(tasks, Llm(store))
    initial = await enqueue(store, service)
    await service.execute(initial.task.id)
    await store.app.revise_transcript(
        transcript_id=initial.task.transcript_id, expected_revision=2, text="合成后续校对"
    )
    expired = await service.get(initial.task.id)
    assert expired.task.status == "expired" and expired.task.text is None
    assert (
        await service.enqueue(
            workspace_id="w",
            transcript_id=initial.task.transcript_id,
            source_revision=2,
            template_revision=1,
            key="create",
        )
        == expired
    )
    with pytest.raises(LlmError, match="source_changed"):
        await service.enqueue(
            workspace_id="w",
            transcript_id=initial.task.transcript_id,
            source_revision=2,
            template_revision=1,
            key="new",
        )


async def test_binding_change_and_changed_intent_never_replace_a_task(
    store: Store, tasks: PostgresMinutesTasks
) -> None:
    llm = Llm(store)
    service = MinutesTasks(tasks, llm)
    initial = await enqueue(store, service)
    llm.binding_id = "sha256:" + "c" * 64
    with pytest.raises(LlmError, match="profile_mismatch"):
        await service.execute(initial.task.id)
    with pytest.raises(IdempotencyConflictError):
        await service.enqueue(
            workspace_id="w",
            transcript_id=initial.task.transcript_id,
            source_revision=2,
            template_revision=1,
            key="create",
        )
    assert not llm.calls and (await service.get(initial.task.id)).task.attempt == 0


@pytest.mark.parametrize("kind", ["project", "transcript"])
async def test_delete_hides_and_cleanup_erases_successful_tasks_but_keeps_key_hashes(
    store: Store,
    tasks: PostgresMinutesTasks,
    cleanup: Cleanup,
    kind: Literal["project", "transcript"],
) -> None:
    service = MinutesTasks(tasks, Llm(store))
    initial = await enqueue(store, service)
    await service.execute(initial.task.id)
    parent = await store.app.get_project(initial.task.project_id)
    target = DeletionTarget(
        "workbench",
        kind,
        parent.id if kind == "project" else initial.task.transcript_id,
        1 if kind == "project" else 2,
    )
    record = await delete(store, parent, target)
    with pytest.raises(ResourceNotFoundError):
        await service.get(initial.task.id)
    async with await psycopg.AsyncConnection.connect(store.owner_dsn) as owner:
        await owner.execute(grant_minutes_cleanup_sql(store.schema, store.role + "_cleanup"))
    await cleanup.worker.run(record.id)
    async with await psycopg.AsyncConnection.connect(store.owner_dsn) as owner:
        for table, count in (
            ("minutes_tasks", 0),
            ("minutes_attempts", 1),
            ("minutes_intents", 1),
            ("minutes_request_keys", 1),
        ):
            assert await (
                await owner.execute(
                    sql.SQL("SELECT count(*) FROM {}.{}").format(
                        sql.Identifier(store.schema), sql.Identifier(table)
                    )
                )
            ).fetchone() == (count,)
    with pytest.raises(ResourceNotFoundError):
        await service.enqueue(
            workspace_id="w",
            transcript_id=initial.task.transcript_id,
            source_revision=2,
            template_revision=1,
            key="create",
        )


async def test_unconfirmed_attempt_cannot_be_silently_erased_as_stopped(
    store: Store, tasks: PostgresMinutesTasks, cleanup: Cleanup
) -> None:
    llm = Llm(store)
    llm.failure = LlmError("unavailable")
    service = MinutesTasks(tasks, llm)
    initial = await enqueue(store, service)
    await service.execute(initial.task.id)
    parent = await store.app.get_project(initial.task.project_id)
    record = await delete(
        store, parent, DeletionTarget("workbench", "transcript", initial.task.transcript_id, 2)
    )
    async with await psycopg.AsyncConnection.connect(store.owner_dsn) as owner:
        await owner.execute(grant_minutes_cleanup_sql(store.schema, store.role + "_cleanup"))
    with pytest.raises(VersionStoreError):
        await cleanup.worker.run(record.id)


async def test_runtime_cannot_change_frozen_intent_or_run_ddl(
    store: Store, tasks: PostgresMinutesTasks
) -> None:
    initial = await enqueue(store, MinutesTasks(tasks, Llm(store)))
    async with await psycopg.AsyncConnection.connect(
        make_conninfo(store.owner_dsn, user=store.role), autocommit=True
    ) as runtime:
        for query in (
            "UPDATE {s}.minutes_tasks SET profile = '{{}}'::jsonb",
            "DELETE FROM {s}.minutes_tasks",
            "DELETE FROM {s}.minutes_attempts",
            "DELETE FROM {s}.minutes_intents",
            "UPDATE {s}.minutes_intents SET model = 'other'",
            "CREATE TABLE {s}.forbidden (id int)",
        ):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                await runtime.execute(sql.SQL(query).format(s=sql.Identifier(store.schema)))
    assert (await tasks.get(initial.task.id)).profile == PROFILE


@pytest.mark.parametrize("barrier", ["revision", "delete"])
async def test_finished_attempt_replay_after_source_change_cannot_return_old_body(
    store: Store, tasks: PostgresMinutesTasks, barrier: str
) -> None:
    initial = await enqueue(store, MinutesTasks(tasks, Llm(store)))
    work = await tasks.claim(initial.task.id, profile=PROFILE, binding_id=BINDING)
    result = LlmCompletion(PROFILE, "合成旧纪要")
    await tasks.finish(work, result=result, error=None)
    if barrier == "revision":
        await store.app.revise_transcript(
            transcript_id=initial.task.transcript_id, expected_revision=2, text="合成新稿"
        )
    else:
        parent = await store.app.get_project(initial.task.project_id)
        await delete(
            store, parent, DeletionTarget("workbench", "transcript", initial.task.transcript_id, 2)
        )
    replay = await tasks.finish(work, result=result, error=None)
    assert replay.task.status == "expired" and replay.task.text is None


@pytest.mark.parametrize("operation", ["create", "claim", "finish"])
async def test_lost_commit_reply_recovers_original_identity_without_redispatch(
    store: Store, tasks: PostgresMinutesTasks, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    llm = Llm(store)
    service = MinutesTasks(tasks, llm)
    _, source = await content(store)
    await store.app.seal_transcript(transcript_id=source.id, expected_revision=1)

    async def create() -> MinutesTaskView:
        return await service.enqueue(
            workspace_id="w",
            transcript_id=source.id,
            source_revision=2,
            template_revision=1,
            key="original",
        )

    initial = await create() if operation != "create" else None
    work = (
        await tasks.claim(initial.task.id, profile=PROFILE, binding_id=BINDING)
        if initial is not None and operation == "finish"
        else None
    )
    result = LlmCompletion(PROFILE, "合成提交结果", LlmUsage(5, 3))
    transaction = psycopg.AsyncConnection.transaction
    commits = 0

    @asynccontextmanager
    async def lose_reply(
        connection: psycopg.AsyncConnection[tuple[object, ...]],
        savepoint_name: str | None = None,
        force_rollback: bool = False,
    ) -> AsyncIterator[psycopg.AsyncTransaction]:
        nonlocal commits
        async with transaction(connection, savepoint_name, force_rollback) as tx:
            yield tx
        commits += 1
        raise psycopg.OperationalError("synthetic private driver error")

    with monkeypatch.context() as patch:
        patch.setattr(psycopg.AsyncConnection, "transaction", lose_reply)
        with pytest.raises(VersionStoreError) as caught:
            if operation == "claim":
                assert initial is not None
                await tasks.claim(initial.task.id, profile=PROFILE, binding_id=BINDING)
            elif work is None:
                await create()
            else:
                await tasks.finish(work, result=result, error=None)
        assert caught.value.commit_unknown and commits == 1
        assert "private" not in str(caught.value)
    recovered = await create()
    assert (
        recovered.task.status
        == {"create": "queued", "claim": "running", "finish": "succeeded"}[operation]
    )
    if operation == "claim":
        with pytest.raises(MinutesTaskConflictError):
            await service.execute(recovered.task.id)
    assert not llm.calls
    if work is not None:
        assert await tasks.finish(work, result=result, error=None) == recovered
        with pytest.raises(IdempotencyConflictError):
            await tasks.finish(work, result=None, error="unavailable")
        assert (await tasks.get(recovered.task.id)).task.text == result.text
    async with await psycopg.AsyncConnection.connect(store.owner_dsn) as owner:
        assert await (
            await owner.execute(
                sql.SQL("SELECT count(*) FROM {}.minutes_tasks").format(
                    sql.Identifier(store.schema)
                )
            )
        ).fetchone() == (1,)


async def test_source_change_while_waiting_expires_and_cancels_client(
    store: Store, tasks: PostgresMinutesTasks
) -> None:
    llm = Llm(store)
    service = MinutesTasks(tasks, llm, poll_seconds=0.01)
    initial = await enqueue(store, service)
    llm.release.clear()
    running = asyncio.create_task(service.execute(initial.task.id))
    try:
        await asyncio.wait_for(llm.entered.wait(), 2)
        await store.app.revise_transcript(
            transcript_id=initial.task.transcript_id, expected_revision=2, text="合成新稿"
        )
        final = await asyncio.wait_for(running, 2)
        assert final.task.status == "expired" and final.task.text is None
        assert llm.cancelled.is_set() and len(llm.calls) == 1
        assert not (await tasks.attempts(initial.task.id))[0].completion_confirmed
    finally:
        if not running.done():
            running.cancel()
        await asyncio.gather(running, return_exceptions=True)


async def test_late_success_after_delete_records_completion_without_publishing_body(
    store: Store, tasks: PostgresMinutesTasks, cleanup: Cleanup
) -> None:
    initial = await enqueue(store, MinutesTasks(tasks, Llm(store)))
    work = await tasks.claim(initial.task.id, profile=PROFILE, binding_id=BINDING)
    parent = await store.app.get_project(initial.task.project_id)
    record = await delete(
        store, parent, DeletionTarget("workbench", "transcript", initial.task.transcript_id, 2)
    )
    result = LlmCompletion(PROFILE, "合成迟到纪要")
    final = await tasks.finish(work, result=result, error=None)
    assert final.task.status == "expired" and final.task.text is None
    assert await tasks.finish(work, result=result, error=None) == final
    with pytest.raises(ResourceNotFoundError):
        await tasks.get(initial.task.id)
    async with await psycopg.AsyncConnection.connect(store.owner_dsn) as owner:
        assert await (
            await owner.execute(
                sql.SQL(
                    "SELECT completion_confirmed FROM {}.minutes_attempts WHERE task_id=%s"
                ).format(sql.Identifier(store.schema)),
                (initial.task.id,),
            )
        ).fetchone() == (True,)
        await owner.execute(grant_minutes_cleanup_sql(store.schema, store.role + "_cleanup"))
    await cleanup.worker.run(record.id)
    with pytest.raises(ResourceNotFoundError):
        await tasks.finish(work, result=result, error=None)


@pytest.mark.parametrize("bad_result", ["profile", "output_budget"])
async def test_invalid_completion_cannot_publish_or_confirm_completion(
    store: Store, tasks: PostgresMinutesTasks, bad_result: str
) -> None:
    initial = await enqueue(store, MinutesTasks(tasks, Llm(store)))
    work = await tasks.claim(initial.task.id, profile=PROFILE, binding_id=BINDING)
    result = (
        LlmCompletion(replace(PROFILE, model="other"), "合成错误模型结果")
        if bad_result == "profile"
        else LlmCompletion(PROFILE, "合成超限结果", LlmUsage(1, PROFILE.max_output_tokens + 1))
    )
    final = await tasks.finish(work, result=result, error=None)
    assert final.task.status == "failed" and final.task.error == "invalid_response"
    assert final.task.text is None
    assert not (await tasks.attempts(initial.task.id))[0].completion_confirmed


@pytest.mark.parametrize("bad", [True, 0, 101])
async def test_queue_rejects_unbounded_or_boolean_limits(
    tasks: PostgresMinutesTasks, bad: int
) -> None:
    with pytest.raises(ValueError):
        await tasks.queued_ids("w", limit=bad)


async def test_simultaneous_first_enqueue_creates_only_one_task(
    store: Store, tasks: PostgresMinutesTasks
) -> None:
    _, source = await content(store)
    await store.app.seal_transcript(transcript_id=source.id, expected_revision=1)
    llm = Llm(store)
    service = MinutesTasks(tasks, llm)
    created = await asyncio.gather(
        *(
            service.enqueue(
                workspace_id="w",
                transcript_id=source.id,
                source_revision=2,
                template_revision=1,
                key="first",
            )
            for _ in range(4)
        )
    )
    assert all(item == created[0] for item in created)
    assert await service.queued_ids("w") == (created[0].task.id,) and not llm.calls


@pytest.mark.parametrize(
    "failure", [LlmError("invalid_response"), RuntimeError("private raw text")]
)
async def test_provider_failures_keep_only_safe_codes_and_never_auto_retry(
    store: Store, tasks: PostgresMinutesTasks, failure: Exception
) -> None:
    llm = Llm(store)
    llm.failure = failure
    service = MinutesTasks(tasks, llm)
    initial = await enqueue(store, service)
    result = await service.execute(initial.task.id)
    assert result.task.status == "failed" and result.task.text is None
    assert result.task.error == (
        "invalid_response" if isinstance(failure, LlmError) else "unavailable"
    )
    assert await service.execute(initial.task.id) == result and len(llm.calls) == 1
    assert "private raw text" not in repr(result) and not await service.queued_ids("w")


@pytest.mark.parametrize("complete_after_cancel", [False, True])
async def test_delete_during_poll_settles_attempt_without_returning_deleted_content(
    store: Store, tasks: PostgresMinutesTasks, complete_after_cancel: bool
) -> None:
    class LateLlm(Llm):
        async def generate(self, request: LlmRequest) -> LlmCompletion:
            try:
                return await super().generate(request)
            except asyncio.CancelledError:
                if not complete_after_cancel:
                    raise
                return LlmCompletion(PROFILE, "合成迟到完成", LlmUsage(7, 4))

    llm = LateLlm(store)
    service = MinutesTasks(tasks, llm, poll_seconds=0.01)
    initial = await enqueue(store, service)
    llm.release.clear()
    running = asyncio.create_task(service.execute(initial.task.id))
    try:
        await asyncio.wait_for(llm.entered.wait(), 2)
        parent = await store.app.get_project(initial.task.project_id)
        await delete(
            store, parent, DeletionTarget("workbench", "transcript", initial.task.transcript_id, 2)
        )
        result = await asyncio.wait_for(running, 2)
        assert result.task.status == "expired" and result.task.text is None
        with pytest.raises(ResourceNotFoundError):
            await service.get(initial.task.id)
        async with await psycopg.AsyncConnection.connect(store.owner_dsn) as owner:
            assert await (
                await owner.execute(
                    sql.SQL(
                        "SELECT outcome,completion_confirmed,input_tokens,output_tokens "
                        "FROM {}.minutes_attempts WHERE task_id=%s"
                    ).format(sql.Identifier(store.schema)),
                    (initial.task.id,),
                )
            ).fetchone() == (
                ("succeeded", True, 7, 4)
                if complete_after_cancel
                else ("interrupted", False, None, None)
            )
        assert len(llm.calls) == 1 and llm.cancelled.is_set()
    finally:
        if not running.done():
            running.cancel()
        await asyncio.gather(running, return_exceptions=True)


async def test_content_cleanup_keeps_minimal_execution_intent_and_usage(
    store: Store, tasks: PostgresMinutesTasks, cleanup: Cleanup
) -> None:
    initial = await enqueue(store, MinutesTasks(tasks, Llm(store)))
    work = await tasks.claim(initial.task.id, profile=PROFILE, binding_id=BINDING)
    result = LlmCompletion(PROFILE, "合成待删除正文", LlmUsage(7, 4))
    await tasks.finish(work, result=result, error=None)
    parent = await store.app.get_project(initial.task.project_id)
    record = await delete(
        store, parent, DeletionTarget("workbench", "transcript", initial.task.transcript_id, 2)
    )
    async with await psycopg.AsyncConnection.connect(store.owner_dsn) as owner:
        await owner.execute(grant_minutes_cleanup_sql(store.schema, store.role + "_cleanup"))
    await cleanup.worker.run(record.id)
    async with await psycopg.AsyncConnection.connect(store.owner_dsn) as owner:
        row = await (
            await owner.execute(
                sql.SQL(
                    "SELECT outcome,completion_confirmed,input_tokens,output_tokens "
                    "FROM {}.minutes_attempts WHERE task_id=%s"
                ).format(sql.Identifier(store.schema)),
                (initial.task.id,),
            )
        ).fetchone()
        assert row == ("succeeded", True, 7, 4)
        intent = await (
            await owner.execute(
                sql.SQL(
                    "SELECT source_revision_id,source_sha256,profile_sha256,binding_id,model,"
                    "template_revision FROM {}.minutes_intents WHERE task_id=%s"
                ).format(sql.Identifier(store.schema)),
                (initial.task.id,),
            )
        ).fetchone()
        assert intent == (
            initial.task.source_revision_id,
            initial.task.source_sha256,
            initial.task.profile_sha256,
            BINDING,
            PROFILE.model,
            PROFILE.template_revision,
        )
        for table in ("minutes_intents", "minutes_attempts", "minutes_request_keys"):
            payloads = await (
                await owner.execute(
                    sql.SQL("SELECT row_to_json(t)::text FROM {}.{} t").format(
                        sql.Identifier(store.schema), sql.Identifier(table)
                    )
                )
            ).fetchall()
            assert payloads and all(
                result.text not in row[0]
                and BODY not in row[0]
                and PROFILE.system_prompt not in row[0]
                for row in payloads
            )
        assert await (
            await owner.execute(
                sql.SQL("SELECT count(*) FROM {}.minutes_tasks").format(
                    sql.Identifier(store.schema)
                )
            )
        ).fetchone() == (0,)


@pytest.mark.parametrize("kind", ["project", "transcript"])
async def test_a_restored_queue_stays_unclaimable_while_the_ledger_records_erasure(
    store: Store,
    tasks: PostgresMinutesTasks,
    cleanup: Cleanup,
    kind: Literal["project", "transcript"],
) -> None:
    """Recovery drill: a snapshot restore must not make an erased task claimable again."""
    service = MinutesTasks(tasks, Llm(store))
    initial = await enqueue(store, service)
    workspace = initial.task.workspace_id
    assert initial.task.id in await tasks.queued_ids(workspace, limit=10)

    captured = await snapshot_rows(
        store,
        "projects",
        "project_versions",
        "transcripts",
        "transcript_revisions",
        "minutes_tasks",
    )
    parent = await store.app.get_project(initial.task.project_id)
    target = DeletionTarget(
        "workbench",
        kind,
        parent.id if kind == "project" else initial.task.transcript_id,
        1 if kind == "project" else 2,
    )
    record = await delete(store, parent, target)
    assert initial.task.id not in await tasks.queued_ids(workspace, limit=10)
    async with await psycopg.AsyncConnection.connect(store.owner_dsn) as owner:
        await owner.execute(grant_minutes_cleanup_sql(store.schema, store.role + "_cleanup"))
    assert await cleanup.worker.run(record.id) is not None

    # The snapshot predates the deletion: the rows return, its tombstones do not.
    await restore_rows(store, captured)
    assert record.id in await drop_tombstones(store)
    assert record.id in await ledger_entries(store)

    assert initial.task.id not in await tasks.queued_ids(workspace, limit=10)


async def test_the_serving_loop_dispatches_queued_work_and_stores_the_model_result(
    store: Store, tasks: PostgresMinutesTasks
) -> None:
    llm = Llm(store)
    service = MinutesTasks(tasks, llm)
    initial = await enqueue(store, service)
    await _dispatch_minutes_once(service, "w")
    final = await service.get(initial.task.id)
    assert final.task.status == "succeeded" and final.task.text == "合成纪要候选"
    assert len(llm.calls) == 1 and llm.calls[0].text == BODY
    assert await service.queued_ids("w") == ()
    attempts = await tasks.attempts(initial.task.id)
    assert attempts[-1].usage == LlmUsage(10, 5) and attempts[-1].outcome == "succeeded"
