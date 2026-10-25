"""Append-only sealed sources on the disposable PG cluster and restricted runtime role."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import timedelta

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo
from tests.integration.test_workbench_deletion_cleanup import Cleanup
from tests.integration.test_workbench_deletion_cleanup import cleanup as cleanup
from tests.integration.test_workbench_listings_postgres import delete
from tests.integration.test_workbench_transcript_import_postgres import (
    PublicService,
    create_parent,
    import_one,
)
from tests.integration.test_workbench_transcript_import_postgres import public_http as public_http
from tests.integration.test_workbench_versions_postgres import Store
from tests.integration.test_workbench_versions_postgres import store as store

from speechnexus_backend.workbench.application.transcript_exports import TranscriptTextExports
from speechnexus_backend.workbench.application.version_ports import MutationReceipt
from speechnexus_backend.workbench.application.versions import (
    IdempotencyConflictError,
    ResourceNotFoundError,
    WorkbenchVersions,
)
from speechnexus_backend.workbench.domain.deletion import DeletionTarget
from speechnexus_backend.workbench.domain.versions import (
    InvalidVersionChangeError,
    Project,
    RevisionConflictError,
    Transcript,
)
from speechnexus_backend.workbench.infrastructure.postgres_versions import VersionStoreError
from speechnexus_backend.workbench.infrastructure.public_transcript_source import (
    PublicTranscriptSource,
)
from speechnexus_backend.workbench.infrastructure.sealing_schema import sealing_migration_sql

pytestmark = pytest.mark.integration
BODY = "\ufeff合成会议\r\n🙂e\u0301 "


async def content(store: Store) -> tuple[Project, Transcript]:
    parent = await store.app.create_project(workspace_id="w", name="合成")
    original = await store.app.create_manual_transcript(
        project_id=parent.id, display_name="合成", text=BODY, idempotency_key="create"
    )
    return parent, original


async def upgrade(store: Store) -> None:
    async with await psycopg.AsyncConnection.connect(store.owner_dsn) as owner:
        await owner.execute(sealing_migration_sql(store.schema))


async def counts(store: Store) -> tuple[object, ...] | None:
    async with await psycopg.AsyncConnection.connect(store.owner_dsn) as owner:
        return await (
            await owner.execute(
                sql.SQL(
                    "SELECT (SELECT count(*) FROM {}.transcript_revisions), "
                    "(SELECT count(*) FROM {}.mutation_receipts)"
                ).format(sql.Identifier(store.schema), sql.Identifier(store.schema))
            )
        ).fetchone()


async def test_v1_refuses_sealing_without_an_owner_upgrade(store: Store) -> None:
    _, original = await content(store)
    with pytest.raises(VersionStoreError) as caught:
        await store.app.seal_transcript(
            transcript_id=original.id, expected_revision=1, idempotency_key="seal"
        )
    assert caught.value.sqlstate == "23514" and not caught.value.commit_unknown
    assert await store.app.get_transcript(original.id) == original
    assert await counts(store) == (1, 1)


async def test_upgrade_preserves_v1_history_receipts_and_runtime_permissions(store: Store) -> None:
    _, original = await content(store)
    corrected = await store.app.revise_transcript(
        transcript_id=original.id, expected_revision=1, text="校对", idempotency_key="edit"
    )
    await upgrade(store)
    assert (
        await store.app.create_manual_transcript(
            project_id=original.project_id, display_name="合成", text=BODY, idempotency_key="create"
        )
        == original
    )
    assert (
        await store.app.revise_transcript(
            transcript_id=original.id, expected_revision=1, text="校对", idempotency_key="edit"
        )
        == corrected
    )
    sealed = await store.app.seal_transcript(
        transcript_id=original.id, expected_revision=2, text="", idempotency_key="seal"
    )
    assert (
        sealed.head.number == 3
        and sealed.head.status == "sealed"
        and sealed.head.source_revision == 2
    )
    assert await store.app.get_sealed_revision(original.id, 3) == sealed.head
    assert (
        await TranscriptTextExports(store.app).export_revision(original.id, 1)
    ).content == BODY.encode()
    assert (
        await TranscriptTextExports(store.app).export_revision(original.id, 2)
    ).content == "校对".encode()
    assert (await TranscriptTextExports(store.app).export_revision(original.id, 3)).content == b""
    assert await counts(store) == (3, 3)
    # Explicit owner-only fragment and append-only content permissions stay enforced.
    async with await psycopg.AsyncConnection.connect(
        make_conninfo(store.owner_dsn, user=store.role), autocommit=True
    ) as runtime:
        for command in (
            sealing_migration_sql(store.schema),
            sql.SQL("UPDATE {}.transcript_revisions SET text = 'forged'").format(
                sql.Identifier(store.schema)
            ),
        ):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                await runtime.execute(command)
    assert await store.app.get_sealed_revision(original.id, 3) == sealed.head
    with pytest.raises(psycopg.errors.UndefinedObject):
        await upgrade(store)
    assert await counts(store) == (3, 3)


async def test_upgrade_missing_expected_v1_constraint_rolls_back_all_changes(store: Store) -> None:
    _, original = await content(store)
    async with await psycopg.AsyncConnection.connect(store.owner_dsn) as owner:
        await owner.execute(
            sql.SQL(
                "ALTER TABLE {}.mutation_receipts RENAME CONSTRAINT "
                "mutation_receipts_operation_check TO unexpected_name"
            ).format(sql.Identifier(store.schema))
        )
    with pytest.raises(psycopg.errors.UndefinedObject):
        await upgrade(store)
    # The preceding DROP on transcript_revisions must have rolled back too.
    async with await psycopg.AsyncConnection.connect(store.owner_dsn) as owner:
        row = await (
            await owner.execute(
                "SELECT count(*) FROM pg_constraint c "
                "JOIN pg_namespace n ON n.oid=c.connamespace "
                "WHERE n.nspname=%s AND c.conname='transcript_revisions_check2'",
                (store.schema,),
            )
        ).fetchone()
        assert row == (1,)
    assert await store.app.get_transcript(original.id) == original


async def test_same_key_concurrent_seals_replay_one_snapshot_then_keep_history(
    store: Store,
) -> None:
    _, original = await content(store)
    await upgrade(store)
    results = await asyncio.gather(
        *(
            store.app.seal_transcript(
                transcript_id=original.id, expected_revision=1, idempotency_key="same"
            )
            for _ in range(5)
        )
    )
    assert all(value == results[0] for value in results)
    sealed = results[0]
    later = await store.app.revise_transcript(
        transcript_id=original.id, expected_revision=2, text="后续", idempotency_key="same"
    )
    assert later.head.number == 3
    assert (
        await store.app.seal_transcript(
            transcript_id=original.id, expected_revision=1, idempotency_key="same"
        )
        == sealed
    )
    assert await store.app.get_sealed_revision(original.id, 2) == sealed.head
    with pytest.raises(IdempotencyConflictError):
        await store.app.seal_transcript(
            transcript_id=original.id, expected_revision=1, text=BODY, idempotency_key="same"
        )
    with pytest.raises(InvalidVersionChangeError):
        await store.app.get_sealed_revision(original.id, 3)
    assert await counts(store) == (3, 3)


async def test_seal_competes_with_edit_by_the_same_current_head(store: Store) -> None:
    _, original = await content(store)
    await upgrade(store)
    results = await asyncio.gather(
        store.app.seal_transcript(
            transcript_id=original.id, expected_revision=1, idempotency_key="seal"
        ),
        store.app.revise_transcript(
            transcript_id=original.id, expected_revision=1, text="竞争校对", idempotency_key="edit"
        ),
        return_exceptions=True,
    )
    assert sum(isinstance(value, Transcript) for value in results) == 1
    assert sum(isinstance(value, RevisionConflictError) for value in results) == 1
    assert await counts(store) == (2, 2)


async def test_real_committed_seal_with_lost_ack_recovers_only_by_original_key(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, original = await content(store)
    await upgrade(store)
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
            await store.app.seal_transcript(
                transcript_id=original.id, expected_revision=1, idempotency_key="original"
            )
        assert caught.value.commit_unknown and commits == 1
        assert "private" not in str(caught.value)
    recovered = await store.app.seal_transcript(
        transcript_id=original.id, expected_revision=1, idempotency_key="original"
    )
    assert recovered.head.number == 2 and recovered.head.text == BODY
    assert await counts(store) == (2, 2)


@pytest.mark.parametrize("kind", ["transcript", "project"])
async def test_delete_acceptance_and_cleanup_block_sealed_reads_and_replay(
    store: Store, cleanup: Cleanup, kind: str
) -> None:
    parent, original = await content(store)
    await upgrade(store)
    await store.app.seal_transcript(
        transcript_id=original.id, expected_revision=1, idempotency_key="seal"
    )
    target = (
        DeletionTarget("workbench", "project", parent.id, 1)
        if kind == "project"
        else DeletionTarget("workbench", "transcript", original.id, 2)
    )
    deletion = await delete(store, parent, target)
    for cleaned in (False, True):
        if cleaned:
            await cleanup.worker.run(deletion.id)
        with pytest.raises(ResourceNotFoundError):
            await store.app.get_sealed_revision(original.id, 2)
        with pytest.raises(ResourceNotFoundError):
            await store.app.seal_transcript(
                transcript_id=original.id, expected_revision=1, idempotency_key="seal"
            )
        with pytest.raises(ResourceNotFoundError):
            await store.app.seal_transcript(
                transcript_id=original.id, expected_revision=2, idempotency_key="new"
            )
        with pytest.raises(ResourceNotFoundError):
            await TranscriptTextExports(store.app).export_revision(original.id, 2)
    async with await psycopg.AsyncConnection.connect(store.owner_dsn) as owner:
        row = await (
            await owner.execute(
                sql.SQL("SELECT count(*) FROM {}.mutation_receipts WHERE transcript_id=%s").format(
                    sql.Identifier(store.schema)
                ),
                (original.id,),
            )
        ).fetchone()
        assert row == (0,)


async def test_adapter_rejects_forged_sealed_source_without_publishing(store: Store) -> None:
    _, original = await content(store)
    await upgrade(store)
    valid = original.seal(
        expected_revision=1,
        revision_id="candidate",
        now=original.head.created_at + timedelta(seconds=1),
    )
    for changed in (
        replace(valid, head=replace(valid.head, source_revision=None)),
        replace(valid, head=replace(valid.head, source_revision=99)),
    ):
        with pytest.raises(ValueError, match="snapshot transition"):
            async with store.uow.transaction() as session:
                assert await session.get_transcript(original.id, for_update=True) == original
                await session.save_transcript(changed)
    assert await store.app.get_transcript(original.id) == original
    assert await counts(store) == (1, 1)


async def test_public_speech_source_pair_survives_sealing_without_new_source_downloads(
    store: Store, public_http: tuple[PublicService, PublicTranscriptSource]
) -> None:
    state, source = public_http
    state.content = BODY.encode()
    app = WorkbenchVersions(store.uow, source_reader=source)
    parent = await create_parent(store)
    original = await import_one(app, parent.id)
    await upgrade(store)
    corrected = await app.revise_transcript(
        transcript_id=original.id, expected_revision=1, text="校对"
    )
    sealed = await app.seal_transcript(
        transcript_id=original.id, expected_revision=2, idempotency_key="seal"
    )
    assert sealed.head.source == corrected.head.source == original.head.source
    assert sealed.head.source_revision == 2 and sealed.head.status == "sealed"
    assert (await app.get_revision(original.id, 1)) == original.head
    assert await app.get_sealed_revision(original.id, 3) == sealed.head
    assert state.reads == 2


async def test_adapter_refuses_a_draft_replay_snapshot_for_a_seal_scope(store: Store) -> None:
    _, original = await content(store)
    await upgrade(store)
    with pytest.raises(ValueError, match="persisted scope"):
        async with store.uow.transaction() as session:
            scope = ("transcript.seal", original.id)
            assert await session.claim_receipt(scope, "forged") is None
            await session.save_receipt(scope, "forged", MutationReceipt("a" * 64, original))
    assert await counts(store) == (1, 1)
