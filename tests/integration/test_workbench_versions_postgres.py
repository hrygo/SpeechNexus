"""Module-owned PG snapshots; only the disposable probe may supply this DSN."""

from __future__ import annotations

import asyncio
import hashlib
import os
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo
from speechnexus_contracts import ContractValidationError

from speechnexus_backend.workbench.application.version_ports import MutationReceipt
from speechnexus_backend.workbench.application.versions import (
    IdempotencyConflictError,
    WorkbenchVersions,
)
from speechnexus_backend.workbench.domain.versions import (
    InvalidVersionChangeError,
    Project,
    ProjectChanges,
    RevisionConflictError,
    SpeechSource,
    Transcript,
)
from speechnexus_backend.workbench.infrastructure.deletion_schema import (
    deletion_migration_sql,
    grant_deletion_runtime_sql,
)
from speechnexus_backend.workbench.infrastructure.postgres_versions import (
    PostgresVersionUnitOfWork,
    VersionStoreError,
)
from speechnexus_backend.workbench.infrastructure.version_schema import (
    grant_runtime_sql,
    migration_sql,
)
from speechnexus_backend.workbench.interfaces.project_commands import ProjectCommandBoundary

pytestmark = pytest.mark.integration


@dataclass
class Store:
    owner_dsn: str
    schema: str
    role: str
    uow: PostgresVersionUnitOfWork

    @property
    def app(self) -> WorkbenchVersions:
        return WorkbenchVersions(self.uow)


@pytest.fixture
async def store() -> AsyncIterator[Store]:
    dsn = os.environ.get("SPEECHNEXUS_WORKBENCH_TEST_DSN")
    if not dsn:
        pytest.skip("disposable workbench PG probe not configured")
    async with await psycopg.AsyncConnection.connect(dsn, autocommit=True) as owner:
        row = await (
            await owner.execute(
                "SELECT current_database(), current_user, current_setting('listen_addresses'), "
                "current_setting('speechnexus.disposable_workbench_probe', true)"
            )
        ).fetchone()
        assert row == ("speechnexus_workbench_test", "workbench_test_owner", "", "on"), (
            "refusing a database outside the disposable Unix-socket-only probe"
        )
        schema = "wb_" + uuid4().hex
        role = "wb_runtime_" + uuid4().hex
        await owner.execute(sql.SQL("CREATE ROLE {} LOGIN").format(sql.Identifier(role)))
        await owner.execute(migration_sql(schema))
        await owner.execute(deletion_migration_sql(schema))
        await owner.execute(grant_runtime_sql(schema, role))
        await owner.execute(grant_deletion_runtime_sql(schema, role))
        try:
            yield Store(
                dsn,
                schema,
                role,
                PostgresVersionUnitOfWork(
                    make_conninfo(dsn, user=role),
                    schema=schema,
                ),
            )
        finally:
            await owner.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))
            await owner.execute(sql.SQL("DROP OWNED BY {}").format(sql.Identifier(role)))
            await owner.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(role)))


async def create_project(store: Store) -> Project:
    return await store.app.create_project(
        workspace_id="workspace-synthetic", name="合成项目", idempotency_key="project-key"
    )


@pytest.mark.parametrize("bad", ["synthetic\x00body", "synthetic\ud800body", "synthetic\udfffbody"])
async def test_invalid_text_rolls_back_without_consuming_keys_or_publishing_versions(
    store: Store, bad: str
) -> None:
    service = store.app
    boundary = ProjectCommandBoundary(service, default_workspace_id="workspace-synthetic")
    for field in ("name", "description"):
        with pytest.raises(InvalidVersionChangeError) as caught:
            await boundary.create_project(
                {"name": "合成", field: bad}, idempotency_key=f"create-{field}"
            )
        assert "synthetic" not in str(caught.value)
        project = await boundary.create_project({"name": "合成"}, idempotency_key=f"create-{field}")
        with pytest.raises(InvalidVersionChangeError):
            await boundary.update_project(
                project.id, {"expected_metadata_revision": 1, field: bad}, idempotency_key="edit"
            )
        assert await service.get_project(project.id) == project
        changed = await boundary.update_project(
            project.id, {"expected_metadata_revision": 1, field: "修改"}, idempotency_key="edit"
        )
        assert changed.metadata_revision == 2

    for field in ("display_name", "text"):
        request = {"display_name": "合成", "text": "原文", field: bad}
        with pytest.raises(InvalidVersionChangeError):
            await service.create_manual_transcript(
                project_id=project.id, **request, idempotency_key=f"text-{field}"
            )
        original = await service.create_manual_transcript(
            project_id=project.id, display_name="合成", text="原文", idempotency_key=f"text-{field}"
        )
    with pytest.raises(InvalidVersionChangeError):
        await service.revise_transcript(
            transcript_id=original.id, expected_revision=1, text=bad, idempotency_key="revision"
        )
    assert await service.get_transcript(original.id) == original
    changed_text = await service.revise_transcript(
        transcript_id=original.id, expected_revision=1, text="校对", idempotency_key="revision"
    )
    assert changed_text.head.number == 2
    assert (await service.get_revision(original.id, 1)).text == "原文"
    async with await psycopg.AsyncConnection.connect(store.owner_dsn) as conn:
        row = await (
            await conn.execute(
                sql.SQL(
                    "SELECT (SELECT count(*) FROM {}.project_versions), "
                    "(SELECT count(*) FROM {}.transcript_revisions), "
                    "(SELECT count(*) FROM {}.mutation_receipts)"
                ).format(*[sql.Identifier(store.schema)] * 3)
            )
        ).fetchone()
    assert row == (4, 3, 7)


async def test_supported_unicode_roundtrips_without_normalization(store: Store) -> None:
    text = " \t中文\r\n🙂e\u0301\u0001\u007f\uffff\U0010ffff "
    project = await store.app.create_project(workspace_id="w", name=text, description=text)
    transcript = await store.app.create_manual_transcript(
        project_id=project.id, display_name=text, text=text
    )
    assert await store.app.get_project(project.id) == project
    assert await store.app.get_transcript(transcript.id) == transcript
    revised = await store.app.revise_transcript(
        transcript_id=transcript.id, expected_revision=1, text=text
    )
    assert (await store.app.get_transcript(transcript.id)).head == revised.head
    assert revised.head.text == text


async def test_generated_project_requests_reach_runtime_role_and_replay_historical_snapshot(
    store: Store,
) -> None:
    boundary = ProjectCommandBoundary(store.app, default_workspace_id="workspace-synthetic")
    payload = {"name": "合成边界项目", "description": "合成说明"}
    originals = await asyncio.gather(
        *(boundary.create_project(payload, idempotency_key="boundary-create") for _ in range(4))
    )
    assert len({project.id for project in originals}) == 1
    original = originals[0]
    invalid = {"expected_metadata_revision": "1", "name": "应拒绝"}
    with pytest.raises(ContractValidationError, match=r"^Invalid ProjectPatch$"):
        await boundary.update_project(original.id, invalid, idempotency_key="boundary-edit")
    patch = {"expected_metadata_revision": 1.0, "description": None}
    edited = await boundary.update_project(original.id, patch, idempotency_key="boundary-edit")
    assert edited.description is None
    assert edited.name == original.name
    await boundary.update_project(
        original.id, {"expected_metadata_revision": 2, "status": "archived"}
    )
    assert await boundary.create_project(payload, idempotency_key="boundary-create") == original
    assert (
        await boundary.update_project(
            original.id,
            {"expected_metadata_revision": 1, "description": None},
            idempotency_key="boundary-edit",
        )
        == edited
    )
    with pytest.raises(RevisionConflictError):
        await boundary.update_project(original.id, patch)
    with pytest.raises(IdempotencyConflictError):
        await boundary.update_project(
            original.id,
            {"expected_metadata_revision": 1, "name": "另一意图"},
            idempotency_key="boundary-edit",
        )
    current = await store.app.get_project(original.id)
    assert current.metadata_revision == 3
    assert current.status == "archived"
    async with await psycopg.AsyncConnection.connect(store.owner_dsn) as conn:
        row = await (
            await conn.execute(
                sql.SQL(
                    "SELECT (SELECT count(*) FROM {}.project_versions), "
                    "(SELECT count(*) FROM {}.mutation_receipts)"
                ).format(sql.Identifier(store.schema), sql.Identifier(store.schema))
            )
        ).fetchone()
    assert row == (3, 2)


async def test_real_roundtrip_historical_receipts_and_explicit_source(store: Store) -> None:
    original = await create_project(store)
    edited = await store.app.edit_project(
        project_id=original.id,
        expected_revision=1,
        changes=ProjectChanges(name="新名称", description="说明"),
        idempotency_key="edit-key",
    )
    assert edited.metadata_revision == 2
    assert await create_project(store) == original
    assert await store.app.get_project(original.id) == edited
    first = await store.app.create_manual_transcript(
        project_id=original.id, display_name="合成稿件", text="原文", idempotency_key="text-key"
    )
    second = await store.app.revise_transcript(
        transcript_id=first.id, expected_revision=1, text="校对", idempotency_key="revise-key"
    )
    third = await store.app.revise_transcript(
        transcript_id=first.id, expected_revision=2, source_revision=1, text="另一个版本"
    )
    assert await store.app.get_revision(first.id, 1) == first.head
    assert await store.app.get_revision(first.id, 2) == second.head
    assert await store.app.get_transcript(first.id) == third
    assert third.head.source_revision == 1
    assert (
        await store.app.revise_transcript(
            transcript_id=first.id, expected_revision=1, text="校对", idempotency_key="revise-key"
        )
        == second
    )
    async with store.uow.transaction() as session:
        assert await session.get_project("absent") is None
        assert await session.get_transcript("absent") is None
        assert await session.get_revision(first.id, 99) is None
    async with await psycopg.AsyncConnection.connect(store.owner_dsn) as conn:
        receipt = await (
            await conn.execute(
                sql.SQL(
                    "SELECT key_digest FROM {}.mutation_receipts WHERE operation = 'project.create'"
                ).format(sql.Identifier(store.schema))
            )
        ).fetchone()
        assert receipt == (hashlib.sha256(b"project-key").hexdigest(),)


async def test_parallel_absent_key_claims_one_identity_and_detects_changed_intent(
    store: Store,
) -> None:
    projects = await asyncio.gather(*(create_project(store) for _ in range(8)))
    assert len({p.id for p in projects}) == 1
    with pytest.raises(IdempotencyConflictError):
        await store.app.create_project(
            workspace_id="workspace-synthetic", name="不同意图", idempotency_key="project-key"
        )
    async with await psycopg.AsyncConnection.connect(store.owner_dsn) as conn:
        counts = await (
            await conn.execute(
                sql.SQL(
                    "SELECT (SELECT count(*) FROM {}.projects), "
                    "(SELECT count(*) FROM {}.mutation_receipts)"
                ).format(sql.Identifier(store.schema), sql.Identifier(store.schema))
            )
        ).fetchone()
        assert counts == (1, 1)


async def test_parallel_distinct_keys_prevent_lost_update(store: Store) -> None:
    project = await create_project(store)
    results = await asyncio.gather(
        *(
            store.app.edit_project(
                project_id=project.id,
                expected_revision=1,
                changes=ProjectChanges(name=f"合成 {index}"),
                idempotency_key=f"edit-key-{index}",
            )
            for index in range(4)
        ),
        return_exceptions=True,
    )
    assert sum(isinstance(result, Project) for result in results) == 1
    assert sum(isinstance(result, RevisionConflictError) for result in results) == 3
    assert (await store.app.get_project(project.id)).metadata_revision == 2


async def test_cancellation_rolls_back_content_and_receipt_and_releases_claim(store: Store) -> None:
    ready = asyncio.Event()
    scope = ("project.create", "workspace-synthetic")

    async def cancelled() -> None:
        async with store.uow.transaction() as session:
            assert await session.claim_receipt(scope, "cancelled-key") is None
            project = Project.create(
                project_id="cancelled-project",
                workspace_id=scope[1],
                name="合成",
                now=await session.now(),
            )
            await session.save_project(project)
            await session.save_receipt(scope, "cancelled-key", MutationReceipt("a" * 64, project))
            ready.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(cancelled())
    await asyncio.wait_for(ready.wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    async with store.uow.transaction() as session:
        assert await session.get_project("cancelled-project") is None
        assert await session.claim_receipt(scope, "cancelled-key") is None
    assert await create_project(store)


async def test_database_failure_rolls_back_header_revision_and_receipt(store: Store) -> None:
    # Real FK failure at commit, after all INSERTs: content must not survive.
    async with await psycopg.AsyncConnection.connect(store.owner_dsn, autocommit=True) as conn:
        await conn.execute(
            sql.SQL(
                "ALTER TABLE {}.projects ADD CONSTRAINT fail_commit "
                "FOREIGN KEY (id, metadata_revision) "
                "REFERENCES {}.project_versions(project_id, revision) "
                "DEFERRABLE INITIALLY DEFERRED"
            ).format(sql.Identifier(store.schema), sql.Identifier(store.schema))
        )
        # Deleting the newly inserted version is owner-only test fault injection.
        await conn.execute(
            sql.SQL(
                "CREATE FUNCTION {}.test_discard_version() RETURNS trigger LANGUAGE plpgsql "
                "SECURITY DEFINER SET search_path = pg_catalog AS $$ "
                "BEGIN DELETE FROM {}.project_versions WHERE project_id = NEW.id; "
                "RETURN NEW; END $$; "
                "CREATE CONSTRAINT TRIGGER discard_version AFTER INSERT ON {}.projects "
                "DEFERRABLE INITIALLY DEFERRED FOR EACH ROW "
                "EXECUTE FUNCTION {}.test_discard_version()"
            ).format(*(sql.Identifier(store.schema) for _ in range(4)))
        )
    with pytest.raises(VersionStoreError) as error:
        await create_project(store)
    assert error.value.sqlstate == "23503"
    assert not error.value.commit_unknown
    assert "合成" not in str(error.value)
    async with await psycopg.AsyncConnection.connect(store.owner_dsn) as conn:
        counts = await (
            await conn.execute(
                sql.SQL(
                    "SELECT (SELECT count(*) FROM {}.projects), "
                    "(SELECT count(*) FROM {}.project_versions), "
                    "(SELECT count(*) FROM {}.mutation_receipts)"
                ).format(*(sql.Identifier(store.schema) for _ in range(3)))
            )
        ).fetchone()
        assert counts == (0, 0, 0)


async def test_runtime_role_cannot_mutate_history_or_ddl(store: Store) -> None:
    project = await create_project(store)
    await store.app.create_manual_transcript(
        project_id=project.id, display_name="稿件", text="合成"
    )
    forbidden = [
        "DELETE FROM {}.projects",
        "UPDATE {}.project_versions SET name = '覆盖'",
        "DELETE FROM {}.transcript_revisions",
        "DELETE FROM {}.mutation_receipts",
        "TRUNCATE {}.projects CASCADE",
        "CREATE TABLE {}.forbidden(id text)",
        "UPDATE {}.transcripts SET display_name = '覆盖'",
    ]
    for statement in forbidden:
        async with await psycopg.AsyncConnection.connect(
            make_conninfo(store.owner_dsn, user=store.role),
            autocommit=True,
        ) as conn:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                await conn.execute(sql.SQL(statement).format(sql.Identifier(store.schema)))


async def test_session_requires_locks_and_rejects_forged_receipts(store: Store) -> None:
    project = await create_project(store)
    async with store.uow.transaction() as session:
        with pytest.raises(ValueError, match="locked"):
            await session.save_project(
                project.edit(
                    expected_revision=1,
                    changes=ProjectChanges(name="无锁"),
                    now=await session.now(),
                )
            )
        with pytest.raises(ValueError, match="claimed"):
            await session.save_receipt(
                ("project.create", "workspace"),
                "key",
                MutationReceipt(
                    "a" * 64,
                    project,
                ),
            )
        scope = ("project.create", "workspace")
        await session.claim_receipt(scope, "forged-key")
        with pytest.raises(ValueError, match="snapshot"):
            await session.save_receipt(
                scope,
                "forged-key",
                MutationReceipt(
                    "a" * 64,
                    replace(project, name="伪造"),
                ),
            )
    with pytest.raises(RuntimeError, match="closed"):
        await session.now()


async def test_sealed_source_and_full_19_digit_revision_roundtrip(store: Store) -> None:
    project = await create_project(store)
    async with store.uow.transaction() as session:
        await session.get_project(project.id, for_update=True)
        transcript = Transcript.create(
            transcript_id="imported-transcript",
            project_id=project.id,
            display_name="合成导入",
            revision_id="imported-revision",
            text="",
            now=await session.now(),
            source=SpeechSource("artifact-synthetic", "receipt-synthetic"),
        )
        await session.save_transcript(transcript)
    assert await store.app.get_transcript(transcript.id) == transcript
    corrected = await store.app.revise_transcript(
        transcript_id=transcript.id, expected_revision=1, text="人工校对"
    )
    assert corrected.head.source == transcript.head.source
    # Owner-only boundary fixture; runtime may never jump its counters.
    async with await psycopg.AsyncConnection.connect(store.owner_dsn) as conn:
        await conn.execute(
            sql.SQL(
                "INSERT INTO {}.project_versions SELECT project_id, %s, name, description, status, "
                "updated_at FROM {}.project_versions WHERE project_id = %s AND revision = 1"
            ).format(sql.Identifier(store.schema), sql.Identifier(store.schema)),
            (10**19 - 1, project.id),
        )
        await conn.execute(
            sql.SQL("UPDATE {}.projects SET metadata_revision = %s WHERE id = %s").format(
                sql.Identifier(store.schema)
            ),
            (10**19 - 1, project.id),
        )
    assert (await store.app.get_project(project.id)).metadata_revision == 10**19 - 1


async def test_waiting_lock_reads_new_head_and_uses_post_wait_clock(store: Store) -> None:
    project = await create_project(store)
    async with store.uow.transaction() as first:
        locked = await first.get_project(project.id, for_update=True)
        assert locked == project
        waiter = asyncio.create_task(
            store.app.edit_project(
                project_id=project.id,
                expected_revision=2,
                changes=ProjectChanges(name="等待后的编辑"),
            )
        )
        try:
            # Observe a real database wait rather than inferring overlap from sleep/gather.
            async with await psycopg.AsyncConnection.connect(
                store.owner_dsn, autocommit=True
            ) as owner:
                deadline = time.monotonic() + 2
                while True:
                    row = await (
                        await owner.execute(
                            "SELECT EXISTS (SELECT 1 FROM pg_stat_activity WHERE "
                            "application_name = 'speechnexus-workbench-versions' "
                            "AND cardinality(pg_blocking_pids(pid)) > 0)"
                        )
                    ).fetchone()
                    if row == (True,):
                        break
                    assert time.monotonic() < deadline, "no actual lock wait was observed"
                    await asyncio.sleep(0.005)
            changed = locked.edit(
                expected_revision=1, changes=ProjectChanges(name="持锁编辑"), now=await first.now()
            )
            await first.save_project(changed)
        except BaseException:
            waiter.cancel()
            await asyncio.gather(waiter, return_exceptions=True)
            raise
    result = await asyncio.wait_for(waiter, 5)
    assert result.metadata_revision == 3
    assert result.updated_at >= changed.updated_at


async def test_lock_timeout_is_bounded_known_abort(store: Store) -> None:
    project = await create_project(store)
    bounded = PostgresVersionUnitOfWork(
        make_conninfo(store.owner_dsn, user=store.role), schema=store.schema, lock_timeout_ms=50
    )
    async with store.uow.transaction() as blocker:
        await blocker.get_project(project.id, for_update=True)
        with pytest.raises(VersionStoreError) as error:
            await asyncio.wait_for(
                WorkbenchVersions(bounded).edit_project(
                    project_id=project.id, expected_revision=1, changes=ProjectChanges(name="超时")
                ),
                2,
            )
        assert error.value.sqlstate == "55P03"
        assert not error.value.commit_unknown
    assert await store.app.get_project(project.id) == project


async def test_transaction_deadline_rolls_back_and_connection_can_be_reused(store: Store) -> None:
    bounded = PostgresVersionUnitOfWork(
        make_conninfo(store.owner_dsn, user=store.role),
        schema=store.schema,
        max_connections=1,
        transaction_timeout=0.1,
    )
    with pytest.raises(VersionStoreError) as error:
        async with bounded.transaction() as session:
            await session.save_project(
                Project.create(
                    project_id="timed-out-project",
                    workspace_id="workspace-synthetic",
                    name="合成",
                    now=await session.now(),
                )
            )
            await asyncio.Event().wait()
    assert not error.value.commit_unknown
    async with bounded.transaction() as session:
        assert await session.get_project("timed-out-project") is None


async def test_exception_after_commit_can_reconcile_original_key(store: Store) -> None:
    # Deliberately discard the already committed application reply. This proves replay
    # after that fault, not actual packet loss during COMMIT or database failover.
    class DiscardedReplyError(Exception):
        pass

    with pytest.raises(DiscardedReplyError):
        await create_project(store)
        raise DiscardedReplyError
    replayed = await create_project(store)
    assert replayed.metadata_revision == 1
    assert await store.app.get_project(replayed.id) == replayed


@pytest.mark.parametrize("failure", ["transport", "shutdown", "statement_unknown"])
async def test_lost_commit_confirmation_is_unknown_and_does_not_auto_retry(
    store: Store,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    original = psycopg.AsyncConnection.transaction
    commits = 0

    @asynccontextmanager
    async def lose_reply(
        connection: psycopg.AsyncConnection[tuple[object, ...]],
        savepoint_name: str | None = None,
        force_rollback: bool = False,
    ) -> AsyncIterator[psycopg.AsyncTransaction]:
        nonlocal commits
        async with original(connection, savepoint_name, force_rollback) as tx:
            yield tx
        commits += 1
        if failure == "shutdown":
            raise psycopg.errors.AdminShutdown("synthetic shutdown after committed transaction")
        if failure == "statement_unknown":
            raise psycopg.errors.StatementCompletionUnknown("synthetic completion unknown")
        raise psycopg.OperationalError("synthetic private driver message must not escape")

    # The real server commits. Only the adapter-facing transaction exit is faulted.
    with monkeypatch.context() as patch:
        patch.setattr(psycopg.AsyncConnection, "transaction", lose_reply)
        with pytest.raises(VersionStoreError) as error:
            await create_project(store)
        assert error.value.commit_unknown
        assert (
            error.value.sqlstate
            == {"transport": None, "shutdown": "57P01", "statement_unknown": "40003"}[failure]
        )
        assert "private" not in str(error.value)
        assert commits == 1
    replayed = await create_project(store)
    assert await store.app.get_project(replayed.id) == replayed


@pytest.mark.parametrize(
    "schema",
    ["public", "pg_catalog", "information_schema", "X", "x" * 64, "wb; DROP SCHEMA public", ""],
)
def test_schema_names_cannot_escape_module_namespace(schema: str) -> None:
    with pytest.raises(ValueError):
        migration_sql(schema)


@pytest.mark.parametrize(
    "options",
    [
        {"max_connections": 0},
        {"max_connections": 65},
        {"transaction_timeout": 0},
        {"transaction_timeout": 61},
        {"lock_timeout_ms": 0},
        {"lock_timeout_ms": 5001},
        {"statement_timeout_ms": 60001},
        {"cleanup_timeout": 0},
        {"cleanup_timeout": 6},
    ],
)
def test_connection_time_and_capacity_bounds_are_validated(options: dict[str, int]) -> None:
    with pytest.raises(ValueError):
        PostgresVersionUnitOfWork("not-used", schema="workbench_test", **options)
