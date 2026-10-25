"""Meeting and minutes links commit atomically on the isolated workbench database."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
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
from tests.integration.test_workbench_deletions_postgres import service as deletions_service
from tests.integration.test_workbench_deletions_postgres import snapshot_rows as snapshot_rows
from tests.integration.test_workbench_minutes_tasks_postgres import BINDING, PROFILE, Llm
from tests.integration.test_workbench_minutes_tasks_postgres import tasks as tasks
from tests.integration.test_workbench_sealing_postgres import content
from tests.integration.test_workbench_versions_postgres import Store
from tests.integration.test_workbench_versions_postgres import store as store

from speechnexus_backend.workbench.application.deletions import WorkbenchDeletions
from speechnexus_backend.workbench.application.llm import LlmError
from speechnexus_backend.workbench.application.meetings import MeetingView, WorkbenchMeetings
from speechnexus_backend.workbench.application.minutes_tasks import MinutesTasks
from speechnexus_backend.workbench.application.versions import (
    IdempotencyConflictError,
    ResourceNotFoundError,
)
from speechnexus_backend.workbench.domain.deletion import (
    ConfirmationMismatchError,
    DeletionPlan,
    DeletionPlanChangedError,
    DeletionRecord,
    DeletionTarget,
)
from speechnexus_backend.workbench.domain.meetings import Meeting
from speechnexus_backend.workbench.domain.minutes_tasks import MinutesTaskConflictError
from speechnexus_backend.workbench.domain.versions import Project, RevisionConflictError, Transcript
from speechnexus_backend.workbench.infrastructure.deletion_codec import plan_digest
from speechnexus_backend.workbench.infrastructure.meetings_schema import (
    grant_meetings_cleanup_sql,
    grant_meetings_runtime_sql,
    meetings_migration_sql,
)
from speechnexus_backend.workbench.infrastructure.minutes_tasks_schema import (
    grant_minutes_cleanup_sql,
)
from speechnexus_backend.workbench.infrastructure.postgres_deletion_cleanup import (
    PostgresDeletionCleanup,
)
from speechnexus_backend.workbench.infrastructure.postgres_deletions import (
    PostgresDeletionUnitOfWork,
)
from speechnexus_backend.workbench.infrastructure.postgres_meetings import PostgresMeetings
from speechnexus_backend.workbench.infrastructure.postgres_minutes_tasks import PostgresMinutesTasks
from speechnexus_backend.workbench.infrastructure.postgres_versions import (
    PostgresVersionUnitOfWork,
    VersionStoreError,
    _Session,
)

pytestmark = pytest.mark.integration


@pytest.fixture
async def meetings(store: Store, tasks: PostgresMinutesTasks) -> WorkbenchMeetings:
    async with await psycopg.AsyncConnection.connect(store.owner_dsn) as owner:
        await owner.execute(meetings_migration_sql(store.schema))
        await owner.execute(grant_meetings_runtime_sql(store.schema, store.role))
    return WorkbenchMeetings(PostgresMeetings(store.uow), Llm(store))


async def created(
    store: Store, service: WorkbenchMeetings
) -> tuple[Project, Transcript, MeetingView]:
    project, transcript = await content(store)
    view = await service.create(
        project_id=project.id,
        workspace_id="w",
        transcript_id=transcript.id,
        display_name="合成会议",
        key="meeting-original",
    )
    return project, transcript, view


async def sealed(
    store: Store, service: WorkbenchMeetings
) -> tuple[Project, Transcript, MeetingView]:
    project, transcript, view = await created(store, service)
    result = await service.seal(
        meeting_id=view.meeting.id, workspace_id="w", expected_revision=1, key="seal-original"
    )
    return project, transcript, result


async def test_concurrent_create_replays_original_capture_after_seal(
    store: Store, meetings: WorkbenchMeetings
) -> None:
    project, transcript, first = await created(store, meetings)
    repeats = await asyncio.gather(
        *(
            meetings.create(
                project_id=project.id,
                workspace_id="w",
                transcript_id=transcript.id,
                display_name="合成会议",
                key="meeting-original",
            )
            for _ in range(3)
        )
    )
    assert repeats == [first] * 3
    await meetings.seal(
        meeting_id=first.meeting.id, workspace_id="w", expected_revision=1, key="seal-original"
    )
    restarted = WorkbenchMeetings(PostgresMeetings(store.uow))
    replay = await restarted.create(
        project_id=project.id,
        workspace_id="w",
        transcript_id=transcript.id,
        display_name="合成会议",
        key="meeting-original",
    )
    assert replay == first and replay.status == "capturing"
    assert (await restarted.get(meeting_id=first.meeting.id, workspace_id="w")).status == "sealed"
    with pytest.raises(IdempotencyConflictError):
        await restarted.create(
            project_id=project.id,
            workspace_id="w",
            transcript_id=transcript.id,
            display_name="不同意图",
            key="meeting-original",
        )


async def test_scope_and_transcript_parent_are_checked(
    store: Store, meetings: WorkbenchMeetings
) -> None:
    project, transcript, view = await created(store, meetings)
    other = await store.app.create_project(workspace_id="w", name="另一个项目")
    for parent, workspace in ((other.id, "w"), (project.id, "other")):
        with pytest.raises(ResourceNotFoundError):
            await meetings.create(
                project_id=parent,
                workspace_id=workspace,
                transcript_id=transcript.id,
                display_name="合成会议",
                key="other-source",
            )
    with pytest.raises(ResourceNotFoundError):
        await meetings.get(meeting_id=view.meeting.id, workspace_id="other")


async def test_concurrent_seal_cas_and_original_reply_do_not_overwrite_history(
    store: Store, meetings: WorkbenchMeetings
) -> None:
    _, transcript, view = await created(store, meetings)
    before = await store.app.get_transcript(transcript.id)
    results = await asyncio.gather(
        *(
            meetings.seal(
                meeting_id=view.meeting.id,
                workspace_id="w",
                expected_revision=1,
                text=" 精确 e\u0301😀\n ",
                key="seal-original",
            )
            for _ in range(3)
        )
    )
    assert all(r == results[0] for r in results)
    assert results[0].meeting.sealed_revision == 2
    assert await store.app.get_revision(transcript.id, 1) == before.head
    assert (await store.app.get_revision(transcript.id, 2)).text == " 精确 e\u0301😀\n "
    await store.app.revise_transcript(
        transcript_id=transcript.id, expected_revision=2, text="后续校对"
    )
    assert (
        await meetings.seal(
            meeting_id=view.meeting.id,
            workspace_id="w",
            expected_revision=1,
            text=" 精确 e\u0301😀\n ",
            key="seal-original",
        )
        == results[0]
    )
    with pytest.raises(RevisionConflictError):
        await meetings.seal(
            meeting_id=view.meeting.id, workspace_id="w", expected_revision=2, key="new-seal-key"
        )


async def test_minutes_link_and_original_202_survive_execution_config_change(
    store: Store, meetings: WorkbenchMeetings, tasks: PostgresMinutesTasks
) -> None:
    _, transcript, view = await sealed(store, meetings)
    initial = await meetings.create_minutes(
        meeting_id=view.meeting.id, workspace_id="w", template_revision=1, key="minutes-original"
    )
    linked = await meetings.get(meeting_id=view.meeting.id, workspace_id="w")
    assert linked.status == "minutes_pending" and linked.meeting.minutes_id == initial.task.id
    worker = MinutesTasks(tasks, Llm(store))
    final = await worker.execute(initial.task.id)
    assert final.task.status == "succeeded"
    assert (
        await meetings.get(meeting_id=view.meeting.id, workspace_id="w")
    ).status == "minutes_ready"
    disabled = WorkbenchMeetings(PostgresMeetings(store.uow))
    assert (
        await disabled.create_minutes(
            meeting_id=view.meeting.id,
            workspace_id="w",
            template_revision=1,
            key="minutes-original",
        )
        == initial
    )
    assert (
        await disabled.get_minutes(
            meeting_id=view.meeting.id, workspace_id="w", minutes_id=initial.task.id
        )
        == final
    )
    assert (await store.app.get_transcript(transcript.id)).head.number == 2
    with pytest.raises(LlmError, match="disabled"):
        await disabled.create_minutes(
            meeting_id=view.meeting.id, workspace_id="w", template_revision=1, key="new-minutes-key"
        )


async def test_minutes_failure_and_expired_source_never_make_meeting_capture_failed(
    store: Store, meetings: WorkbenchMeetings, tasks: PostgresMinutesTasks
) -> None:
    _, transcript, view = await sealed(store, meetings)
    initial = await meetings.create_minutes(
        meeting_id=view.meeting.id, workspace_id="w", template_revision=1, key="minutes-original"
    )
    llm = Llm(store)
    llm.failure = LlmError("unavailable")
    final = await MinutesTasks(tasks, llm).execute(initial.task.id)
    assert final.task.status == "failed"
    assert (
        await meetings.get(meeting_id=view.meeting.id, workspace_id="w")
    ).status == "minutes_pending"
    await store.app.revise_transcript(
        transcript_id=transcript.id, expected_revision=2, text="更改源"
    )
    expired = await meetings.get_minutes(
        meeting_id=view.meeting.id, workspace_id="w", minutes_id=initial.task.id
    )
    assert expired.task.status == "expired" and expired.task.text is None
    current = await meetings.get(meeting_id=view.meeting.id, workspace_id="w")
    assert current.meeting.sealed_revision == 2 and current.status == "minutes_pending"


async def test_minutes_rejects_wrong_revision_profile_and_foreign_meeting_link(
    store: Store, meetings: WorkbenchMeetings, tasks: PostgresMinutesTasks
) -> None:
    _, transcript, view = await created(store, meetings)
    with pytest.raises(LlmError, match="source_not_sealed"):
        await meetings.create_minutes(
            meeting_id=view.meeting.id,
            workspace_id="w",
            template_revision=1,
            key="minutes-original",
        )
    await meetings.seal(
        meeting_id=view.meeting.id, workspace_id="w", expected_revision=1, key="seal-original"
    )
    for template, model in ((2, None), (1, "unapproved-model")):
        with pytest.raises(LlmError, match="profile_mismatch"):
            await meetings.create_minutes(
                meeting_id=view.meeting.id,
                workspace_id="w",
                template_revision=template,
                llm_model=model,
                key="minutes-original",
            )
    initial = await meetings.create_minutes(
        meeting_id=view.meeting.id, workspace_id="w", template_revision=1, key="minutes-original"
    )
    other = await meetings.create(
        project_id=view.meeting.project_id,
        workspace_id="w",
        transcript_id=transcript.id,
        display_name="不同会议",
        key="other-meeting",
    )
    with pytest.raises(ResourceNotFoundError):
        await meetings.get_minutes(
            meeting_id=other.meeting.id, workspace_id="w", minutes_id=initial.task.id
        )
    with pytest.raises(IdempotencyConflictError):
        await meetings.create_minutes(
            meeting_id=view.meeting.id,
            workspace_id="w",
            template_revision=1,
            llm_model=PROFILE.model,
            key="minutes-original",
        )


async def test_minutes_link_failure_rolls_back_enqueue_and_key(
    store: Store,
    meetings: WorkbenchMeetings,
    tasks: PostgresMinutesTasks,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, _, view = await sealed(store, meetings)
    original = PostgresMeetings._save

    async def fail_link(
        self: PostgresMeetings, base: _Session, previous: Meeting, changed: Meeting
    ) -> None:
        if changed.minutes_id is not None:
            raise RuntimeError("synthetic link failure")
        await original(self, base, previous, changed)

    with monkeypatch.context() as context:
        context.setattr(PostgresMeetings, "_save", fail_link)
        with pytest.raises(RuntimeError, match="synthetic link failure"):
            await meetings.create_minutes(
                meeting_id=view.meeting.id,
                workspace_id="w",
                template_revision=1,
                key="minutes-original",
            )
    assert not await tasks.queued_ids("w", limit=100)
    assert (await meetings.get(meeting_id=view.meeting.id, workspace_id="w")).status == "sealed"
    initial = await meetings.create_minutes(
        meeting_id=view.meeting.id, workspace_id="w", template_revision=1, key="minutes-original"
    )
    assert await tasks.queued_ids("w", limit=100) == (initial.task.id,)


async def test_runtime_cannot_mutate_meeting_identity_receipts_or_schema(
    store: Store, meetings: WorkbenchMeetings
) -> None:
    await created(store, meetings)
    async with await psycopg.AsyncConnection.connect(
        make_conninfo(store.owner_dsn, user=store.role), autocommit=True
    ) as runtime:
        for query in (
            "UPDATE {s}.meetings SET display_name='other'",
            "DELETE FROM {s}.meetings",
            "UPDATE {s}.meeting_receipts SET intent_digest=repeat('a',64)",
            "DELETE FROM {s}.meeting_receipts",
            "CREATE TABLE {s}.forbidden(id int)",
        ):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                await runtime.execute(sql.SQL(query).format(s=sql.Identifier(store.schema)))


@pytest.mark.parametrize("operation", ["create", "seal", "minutes"])
async def test_meeting_lost_commit_reply_replays_one_original_intent(
    store: Store,
    meetings: WorkbenchMeetings,
    tasks: PostgresMinutesTasks,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    project, source = await content(store)
    initial = None
    if operation != "create":
        initial = await meetings.create(
            project_id=project.id,
            workspace_id="w",
            transcript_id=source.id,
            display_name="合成会议",
            key="meeting-original",
        )
    if operation == "minutes":
        assert initial is not None
        initial = await meetings.seal(
            meeting_id=initial.meeting.id,
            workspace_id="w",
            expected_revision=1,
            key="seal-original",
        )

    async def command() -> str:
        if operation == "create":
            result = await meetings.create(
                project_id=project.id,
                workspace_id="w",
                transcript_id=source.id,
                display_name="合成会议",
                key="meeting-original",
            )
            return result.meeting.id
        assert initial is not None
        if operation == "seal":
            result = await meetings.seal(
                meeting_id=initial.meeting.id,
                workspace_id="w",
                expected_revision=1,
                key="seal-original",
            )
            return result.meeting.sealed_revision_id or ""
        accepted = await meetings.create_minutes(
            meeting_id=initial.meeting.id,
            workspace_id="w",
            template_revision=1,
            key="minutes-original",
        )
        assert accepted.task.status == "queued"
        return accepted.task.id

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
        raise psycopg.OperationalError("synthetic private reply loss")

    with monkeypatch.context() as patch:
        patch.setattr(psycopg.AsyncConnection, "transaction", lose_reply)
        with pytest.raises(VersionStoreError) as caught:
            await command()
        assert caught.value.commit_unknown and commits == 1
        assert "private" not in str(caught.value)
    recovered = await command()
    assert recovered and await command() == recovered
    async with await psycopg.AsyncConnection.connect(store.owner_dsn) as owner:
        for table, count in (
            ("meetings", 1),
            ("transcript_revisions", 1 if operation == "create" else 2),
            ("minutes_tasks", int(operation == "minutes")),
        ):
            row = await (
                await owner.execute(
                    sql.SQL("SELECT count(*) FROM {}.{}").format(
                        sql.Identifier(store.schema), sql.Identifier(table)
                    )
                )
            ).fetchone()
            assert row == (count,), table


async def test_reseal_preserves_end_and_owns_old_minutes_history(
    store: Store,
    meetings: WorkbenchMeetings,
    tasks: PostgresMinutesTasks,
) -> None:
    _, source, first = await sealed(store, meetings)
    job = await meetings.create_minutes(
        meeting_id=first.meeting.id, workspace_id="w", template_revision=1, key="minutes-original"
    )
    with pytest.raises(MinutesTaskConflictError):
        await meetings.create_minutes(
            meeting_id=first.meeting.id, workspace_id="w", template_revision=1, key="different-key"
        )
    work = await tasks.claim(job.task.id, profile=PROFILE, binding_id=BINDING)
    with pytest.raises(MinutesTaskConflictError):
        await meetings.create_minutes(
            meeting_id=first.meeting.id, workspace_id="w", template_revision=1, key="different-key"
        )
    await store.app.revise_transcript(transcript_id=source.id, expected_revision=2, text="合成修订")
    next_seal = await meetings.seal(
        meeting_id=first.meeting.id, workspace_id="w", expected_revision=3, key="reseal-original"
    )
    assert next_seal.meeting.ended_at == first.meeting.ended_at
    assert next_seal.meeting.sealed_revision == 4 and next_seal.meeting.minutes_id is None
    old = await meetings.get_minutes(
        meeting_id=first.meeting.id, workspace_id="w", minutes_id=job.task.id
    )
    assert old.task.status == "expired" and old.task.text is None
    assert work.view.task.id == job.task.id
    new = await meetings.create_minutes(
        meeting_id=first.meeting.id, workspace_id="w", template_revision=1, key=None
    )
    assert new.task.id != job.task.id and new.task.source_revision == 4
    assert (
        await meetings.get_minutes(
            meeting_id=first.meeting.id, workspace_id="w", minutes_id=new.task.id
        )
    ).task == new.task


def deletion_service(store: Store) -> WorkbenchDeletions:
    return WorkbenchDeletions(
        PostgresDeletionUnitOfWork(store.uow, meetings=True), digest_plan=plan_digest
    )


async def accept_plan(app: WorkbenchDeletions, plan: DeletionPlan, *, key: str) -> DeletionRecord:
    return await app.accept(
        workspace_id="w",
        plan_id=plan.id,
        expected_revision=1,
        digest=plan_digest(plan),
        targets=plan.confirmed_targets,
        key=key,
    )


@pytest.mark.parametrize("kind", ["project", "transcript"])
async def test_meeting_addition_and_minutes_link_invalidate_delete_plan(
    store: Store,
    meetings: WorkbenchMeetings,
    kind: Literal["project", "transcript"],
) -> None:
    project, source = await content(store)
    target = DeletionTarget("workbench", kind, project.id if kind == "project" else source.id, 1)
    app = deletion_service(store)
    before = await app.preview(workspace_id="w", target=target, mode="cascade", key="before")
    view = await meetings.create(
        project_id=project.id,
        workspace_id="w",
        transcript_id=source.id,
        display_name="合成会议",
        key="meeting-original",
    )
    with pytest.raises(DeletionPlanChangedError):
        await accept_plan(app, before, key="before-delete")
    ordinary = await app.preview(workspace_id="w", target=target, mode="ordinary", key="ordinary")
    assert not ordinary.eligible
    assert any(
        d.target.kind == "meeting" and d.disposition == "blocks" for d in ordinary.dependencies
    )
    await meetings.seal(
        meeting_id=view.meeting.id, workspace_id="w", expected_revision=1, key="seal-original"
    )
    target = DeletionTarget("workbench", kind, target.id, 1 if kind == "project" else 2)
    after_seal = await app.preview(workspace_id="w", target=target, mode="cascade", key="sealed")
    await meetings.create_minutes(
        meeting_id=view.meeting.id, workspace_id="w", template_revision=1, key="minutes-original"
    )
    with pytest.raises(DeletionPlanChangedError):
        await accept_plan(app, after_seal, key="sealed-delete")
    plan = await app.preview(workspace_id="w", target=target, mode="cascade", key="linked")
    assert any(t.kind == "meeting" and t.id == view.meeting.id for t in plan.confirmed_targets)
    with pytest.raises(ConfirmationMismatchError):
        await app.accept(
            workspace_id="w",
            plan_id=plan.id,
            expected_revision=1,
            digest=plan_digest(plan),
            targets=tuple(t for t in plan.confirmed_targets if t.kind != "meeting"),
            key="missing-meeting",
        )
    await accept_plan(app, plan, key="delete-linked")
    for operation in (
        meetings.get(meeting_id=view.meeting.id, workspace_id="w"),
        meetings.create(
            project_id=project.id,
            workspace_id="w",
            transcript_id=source.id,
            display_name="合成会议",
            key="meeting-original",
        ),
        meetings.seal(
            meeting_id=view.meeting.id, workspace_id="w", expected_revision=1, key="seal-original"
        ),
        meetings.create_minutes(
            meeting_id=view.meeting.id,
            workspace_id="w",
            template_revision=1,
            key="minutes-original",
        ),
    ):
        with pytest.raises(ResourceNotFoundError):
            await operation


async def meeting_cleanup(store: Store, cleanup: Cleanup) -> PostgresDeletionCleanup:
    async with await psycopg.AsyncConnection.connect(store.owner_dsn) as owner:
        role = store.role + "_cleanup"
        await owner.execute(grant_meetings_cleanup_sql(store.schema, role))
        await owner.execute(grant_minutes_cleanup_sql(store.schema, role))
    return PostgresDeletionCleanup(
        PostgresVersionUnitOfWork(cleanup.dsn, schema=store.schema), meetings=True
    )


@pytest.mark.parametrize("running", [False, True])
async def test_confirmed_cleanup_erases_meetings_but_never_unconfirmed_llm(
    store: Store,
    meetings: WorkbenchMeetings,
    tasks: PostgresMinutesTasks,
    cleanup: Cleanup,
    running: bool,
) -> None:
    project, source, view = await sealed(store, meetings)
    job = await meetings.create_minutes(
        meeting_id=view.meeting.id, workspace_id="w", template_revision=1, key="minutes-original"
    )
    if running:
        await tasks.claim(job.task.id, profile=PROFILE, binding_id=BINDING)
    else:
        await MinutesTasks(tasks, Llm(store)).execute(job.task.id)
    app = deletion_service(store)
    plan = await app.preview(
        workspace_id="w",
        target=DeletionTarget("workbench", "project", project.id, 1),
        mode="cascade",
        key="cleanup-preview",
    )
    record = await accept_plan(app, plan, key="cleanup-delete")
    worker = await meeting_cleanup(store, cleanup)
    if running:
        with pytest.raises(VersionStoreError):
            await worker.run(record.id)
        async with await psycopg.AsyncConnection.connect(store.owner_dsn) as owner:
            assert await (
                await owner.execute(
                    sql.SQL("SELECT count(*) FROM {}.meetings").format(sql.Identifier(store.schema))
                )
            ).fetchone() == (1,)
            assert await (
                await owner.execute(
                    sql.SQL("SELECT count(*) FROM {}.deletion_ledger").format(
                        sql.Identifier(store.schema)
                    )
                )
            ).fetchone() == (0,)
        return
    result = await worker.run(record.id)
    assert result is not None and set(result.deleted) == set(plan.confirmed_targets)
    assert await worker.run(record.id) == result
    async with await psycopg.AsyncConnection.connect(store.owner_dsn) as owner:
        for table in ("meetings", "minutes_tasks", "transcript_revisions", "projects"):
            assert await (
                await owner.execute(
                    sql.SQL("SELECT count(*) FROM {}.{}").format(
                        sql.Identifier(store.schema), sql.Identifier(table)
                    )
                )
            ).fetchone() == (0,), table
        receipts = await (
            await owner.execute(
                sql.SQL(
                    "SELECT meeting_id, key_digest, intent_digest FROM {}.meeting_receipts"
                ).format(sql.Identifier(store.schema))
            )
        ).fetchall()
        assert len(receipts) == 3
        assert all(r[0] is None and len(r[1]) == len(r[2]) == 64 for r in receipts)
    with pytest.raises(ResourceNotFoundError):
        await meetings.create(
            project_id=project.id,
            workspace_id="w",
            transcript_id=source.id,
            display_name="合成会议",
            key="meeting-original",
        )


async def test_delete_racing_meeting_creation_serializes_on_parent(
    store: Store,
    meetings: WorkbenchMeetings,
) -> None:
    project, source = await content(store)
    app = deletion_service(store)
    plan = await app.preview(
        workspace_id="w",
        target=DeletionTarget("workbench", "project", project.id, 1),
        mode="cascade",
        key="race-preview",
    )
    deletion, creation = await asyncio.gather(
        accept_plan(app, plan, key="race-delete"),
        meetings.create(
            project_id=project.id,
            workspace_id="w",
            transcript_id=source.id,
            display_name="合成并发会议",
            key="race-meeting",
        ),
        return_exceptions=True,
    )
    if isinstance(deletion, DeletionRecord):
        assert isinstance(creation, ResourceNotFoundError)
        with pytest.raises(ResourceNotFoundError):
            await store.app.get_transcript(source.id)
    else:
        assert isinstance(deletion, DeletionPlanChangedError)
        assert isinstance(creation, MeetingView)
        assert await meetings.get(meeting_id=creation.meeting.id, workspace_id="w") == creation


async def test_cancellation_after_minutes_link_rolls_back_every_write(
    store: Store,
    meetings: WorkbenchMeetings,
    tasks: PostgresMinutesTasks,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, _, view = await sealed(store, meetings)
    original = PostgresMeetings._save

    async def cancel(
        self: PostgresMeetings, base: _Session, previous: Meeting, changed: Meeting
    ) -> None:
        await original(self, base, previous, changed)
        raise asyncio.CancelledError

    with monkeypatch.context() as patch:
        patch.setattr(PostgresMeetings, "_save", cancel)
        with pytest.raises(asyncio.CancelledError):
            await meetings.create_minutes(
                meeting_id=view.meeting.id,
                workspace_id="w",
                template_revision=1,
                key="minutes-original",
            )
    assert not await tasks.queued_ids("w", limit=100)
    assert await meetings.get(meeting_id=view.meeting.id, workspace_id="w") == view
    job = await meetings.create_minutes(
        meeting_id=view.meeting.id, workspace_id="w", template_revision=1, key="minutes-original"
    )
    assert await tasks.queued_ids("w", limit=100) == (job.task.id,)


async def test_cleanup_never_erases_meeting_omitted_by_unconfigured_dependency_graph(
    store: Store,
    meetings: WorkbenchMeetings,
    cleanup: Cleanup,
) -> None:
    project, _, view = await sealed(store, meetings)
    app = WorkbenchDeletions(PostgresDeletionUnitOfWork(store.uow), digest_plan=plan_digest)
    plan = await app.preview(
        workspace_id="w",
        target=DeletionTarget("workbench", "project", project.id, 1),
        mode="cascade",
        key="unconfigured-preview",
    )
    assert all(t.kind != "meeting" for t in plan.confirmed_targets)
    record = await accept_plan(app, plan, key="unconfigured-delete")
    worker = await meeting_cleanup(store, cleanup)
    with pytest.raises(ValueError, match="unconfirmed or missing meeting"):
        await worker.run(record.id)
    async with await psycopg.AsyncConnection.connect(store.owner_dsn) as owner:
        assert await (
            await owner.execute(
                sql.SQL("SELECT id FROM {}.meetings").format(sql.Identifier(store.schema))
            )
        ).fetchone() == (view.meeting.id,)
        assert await (
            await owner.execute(
                sql.SQL("SELECT count(*) FROM {}.deletion_ledger").format(
                    sql.Identifier(store.schema)
                )
            )
        ).fetchone() == (0,)


async def test_minutes_expected_source_rejects_other_reseal_before_enqueue(
    store: Store,
    meetings: WorkbenchMeetings,
    tasks: PostgresMinutesTasks,
) -> None:
    _, source, view = await sealed(store, meetings)
    await store.app.revise_transcript(
        transcript_id=source.id, expected_revision=2, text="另一次校对"
    )
    resealed = await meetings.seal(
        meeting_id=view.meeting.id, workspace_id="w", expected_revision=3, key="other-seal"
    )
    assert resealed.meeting.sealed_revision == 4
    with pytest.raises(LlmError, match="source_changed"):
        await meetings.create_minutes(
            meeting_id=view.meeting.id,
            workspace_id="w",
            template_revision=1,
            expected_source_revision=2,
            key="pinned-source",
        )
    assert not await tasks.queued_ids("w", limit=100)
    job = await meetings.create_minutes(
        meeting_id=view.meeting.id,
        workspace_id="w",
        template_revision=1,
        expected_source_revision=4,
        key="pinned-source",
    )
    assert job.task.source_revision == 4
    disabled = WorkbenchMeetings(PostgresMeetings(store.uow))
    await store.app.revise_transcript(
        transcript_id=source.id, expected_revision=4, text="又一次校对"
    )
    assert (
        await disabled.create_minutes(
            meeting_id=view.meeting.id,
            workspace_id="w",
            template_revision=1,
            expected_source_revision=4,
            key="pinned-source",
        )
        == job
    )
    with pytest.raises(IdempotencyConflictError):
        await meetings.create_minutes(
            meeting_id=view.meeting.id,
            workspace_id="w",
            template_revision=1,
            expected_source_revision=2,
            key="pinned-source",
        )


async def test_a_restored_meeting_stays_invisible_while_the_ledger_records_erasure(
    store: Store, meetings: WorkbenchMeetings, cleanup: Cleanup
) -> None:
    """Recovery drill for meetings: an erased project keeps its meetings invisible."""
    project, _, view = await created(store, meetings)
    captured = await snapshot_rows(
        store,
        "projects",
        "project_versions",
        "transcripts",
        "transcript_revisions",
        "meetings",
        "meeting_receipts",
    )
    deletions = deletions_service(store)
    plan = await deletions.preview(
        workspace_id=project.workspace_id,
        target=DeletionTarget("workbench", "project", project.id, 1),
        mode="cascade",
        key="drill-meeting-plan-key",
    )
    record = await deletions.accept(
        workspace_id=project.workspace_id,
        plan_id=plan.id,
        expected_revision=1,
        digest=plan_digest(plan),
        targets=plan.confirmed_targets,
        key="drill-meeting-delete-key",
    )
    async with await psycopg.AsyncConnection.connect(store.owner_dsn) as owner:
        await owner.execute(grant_meetings_cleanup_sql(store.schema, store.role + "_cleanup"))
    assert await cleanup.worker.run(record.id) is not None

    # The snapshot predates the deletion: rows return, tombstones do not.
    await restore_rows(store, captured)
    assert record.id in await drop_tombstones(store)
    assert record.id in await ledger_entries(store)

    with pytest.raises(ResourceNotFoundError):
        await meetings.get(meeting_id=view.meeting.id, workspace_id="w")
