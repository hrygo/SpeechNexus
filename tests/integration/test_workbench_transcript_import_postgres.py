"""Actual loopback HTTP plus the disposable runtime-role PostgreSQL store."""

import asyncio
import hashlib
import json
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field

import httpx
import psycopg
import pytest
from tests.integration.test_workbench_deletion_cleanup import Cleanup
from tests.integration.test_workbench_deletion_cleanup import cleanup as cleanup
from tests.integration.test_workbench_deletions_postgres import target
from tests.integration.test_workbench_listings_postgres import delete
from tests.integration.test_workbench_versions_postgres import Store
from tests.integration.test_workbench_versions_postgres import store as store

from speechnexus_backend.workbench.application.transcript_sources import TranscriptSourceError
from speechnexus_backend.workbench.application.version_ports import VersionSession
from speechnexus_backend.workbench.application.versions import (
    IdempotencyConflictError,
    ResourceNotFoundError,
    WorkbenchVersions,
)
from speechnexus_backend.workbench.domain.deletion import DeletionTarget
from speechnexus_backend.workbench.domain.versions import Project, Transcript
from speechnexus_backend.workbench.infrastructure.public_transcript_source import (
    PublicTranscriptSource,
)

pytestmark = pytest.mark.integration
TEXT = "  合成网络原文\n"
CONTENT = TEXT.encode()


@dataclass
class PublicService:
    content: bytes = CONTENT
    reads: int = 0
    hold: bool = False
    entered: asyncio.Event = field(default_factory=asyncio.Event)
    release: asyncio.Event = field(default_factory=asyncio.Event)
    tasks: set[asyncio.Task[None]] = field(default_factory=set)
    errors: list[str] = field(default_factory=list)

    async def connection(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        assert task is not None
        self.tasks.add(task)
        try:
            headers = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 2)
            method, path, _ = headers.split(b"\r\n", 1)[0].split(b" ", 2)
            assert method == b"GET"
            self.reads += 1
            if path == b"/v1/nexus/artifacts/artifact-source":
                body = json.dumps(
                    {
                        "id": "artifact-source",
                        "object": "nexus.artifact",
                        "workspace_id": "workspace-one",
                        "kind": "transcript",
                        "status": "ready",
                        "media_type": "text/plain; charset=utf-8",
                        "bytes": len(self.content),
                        "sha256": "sha256:" + hashlib.sha256(self.content).hexdigest(),
                        "receipt_id": "receipt-source",
                        "created_at": "2026-10-23T00:00:00Z",
                    }
                ).encode()
                writer.write(
                    b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                    b"Connection: close\r\nContent-Length: "
                    + str(len(body)).encode()
                    + b"\r\n\r\n"
                    + body
                )
            else:
                assert path == b"/v1/nexus/artifacts/artifact-source/content"
                self.entered.set()
                if self.hold:
                    await self.release.wait()
                writer.write(
                    b"HTTP/1.1 200 OK\r\nContent-Type: application/octet-stream\r\n"
                    b"Transfer-Encoding: chunked\r\nConnection: close\r\n\r\n"
                )
                for chunk in (self.content[:3], self.content[3:]):
                    if not chunk:
                        continue
                    writer.write(f"{len(chunk):x}\r\n".encode() + chunk + b"\r\n")
                writer.write(b"0\r\n\r\n")
            await writer.drain()
        except ConnectionError, asyncio.CancelledError:
            pass
        except Exception as error:
            self.errors.append(type(error).__name__)
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except ConnectionError:
                pass  # A cancelled client may already have reset its synthetic source connection.
            finally:
                self.tasks.discard(task)


@pytest.fixture
async def public_http() -> AsyncIterator[tuple[PublicService, PublicTranscriptSource]]:
    state = PublicService()
    server = await asyncio.start_server(state.connection, "127.0.0.1", 0)
    host, port = server.sockets[0].getsockname()[:2]
    try:
        async with httpx.AsyncClient(base_url=f"http://{host}:{port}", trust_env=False) as client:
            yield state, PublicTranscriptSource(client)
    finally:
        state.release.set()
        server.close()
        await server.wait_closed()
        if state.tasks:
            await asyncio.wait_for(asyncio.gather(*state.tasks), 2)
        assert not state.errors


async def create_parent(store: Store) -> Project:
    return await store.app.create_project(workspace_id="workspace-one", name="合成项目")


async def import_one(app: WorkbenchVersions, parent_id: str) -> Transcript:
    return await app.import_speech_transcript(
        project_id=parent_id,
        display_name="合成原稿",
        artifact_id="artifact-source",
        receipt_id="receipt-source",
        idempotency_key="import-source",
    )


async def test_real_http_import_sealed_then_corrected_history_and_replay_without_source(
    store: Store, public_http: tuple[PublicService, PublicTranscriptSource]
) -> None:
    state, reader = public_http
    app = WorkbenchVersions(store.uow, source_reader=reader)
    parent = await create_parent(store)
    original = await import_one(app, parent.id)
    assert state.reads == 2
    assert original.head.status == "sealed" and original.head.text == TEXT
    assert original.head.source is not None
    assert original.head.source.receipt_id == "receipt-source"
    corrected = await app.revise_transcript(
        transcript_id=original.id, expected_revision=1, text="合成校对"
    )
    assert corrected.head.status == "draft" and corrected.head.source_revision == 1
    assert await app.get_revision(original.id, 1) == original.head
    # Even a disabled source reader must allow historical reconciliation.
    assert await import_one(WorkbenchVersions(store.uow), parent.id) == original
    assert state.reads == 2
    with pytest.raises(IdempotencyConflictError):
        await app.create_manual_transcript(
            project_id=parent.id,
            display_name="合成原稿",
            text=TEXT,
            idempotency_key="import-source",
        )
    page = await app.list_transcripts(project_id=parent.id)
    assert len(page.items) == 1 and page.items[0].head_revision == 2
    assert page.items[0].source == original.head.source


async def test_download_holds_no_database_connection_and_parent_deletion_wins(
    store: Store, public_http: tuple[PublicService, PublicTranscriptSource]
) -> None:
    state, reader = public_http
    state.hold = True
    parent = await create_parent(store)
    task = asyncio.create_task(
        import_one(WorkbenchVersions(store.uow, source_reader=reader), parent.id)
    )
    try:
        await asyncio.wait_for(state.entered.wait(), 2)
        async with await psycopg.AsyncConnection.connect(store.owner_dsn) as owner:
            row = await (
                await owner.execute(
                    "SELECT count(*) FROM pg_stat_activity WHERE usename = %s AND "
                    "application_name = 'speechnexus-workbench-versions'",
                    (store.role,),
                )
            ).fetchone()
            assert row == (0,), "network read retained a database connection"
        await delete(store, parent, target(parent))
        state.release.set()
        with pytest.raises(ResourceNotFoundError):
            await asyncio.wait_for(task, 2)
        assert (await store.app.list_projects(workspace_id="workspace-one")).items == ()
    finally:
        state.release.set()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def test_concurrent_real_http_imports_commit_one_identity(
    store: Store, public_http: tuple[PublicService, PublicTranscriptSource]
) -> None:
    state, reader = public_http
    parent = await create_parent(store)
    app = WorkbenchVersions(store.uow, source_reader=reader)
    imported = await asyncio.gather(*(import_one(app, parent.id) for _ in range(6)))
    assert len({item.id for item in imported}) == 1
    assert 2 <= state.reads <= 12
    assert len((await app.list_transcripts(project_id=parent.id)).items) == 1


async def test_cancelled_download_does_not_commit_key_and_can_retry(
    store: Store, public_http: tuple[PublicService, PublicTranscriptSource]
) -> None:
    state, reader = public_http
    state.hold = True
    parent = await create_parent(store)
    app = WorkbenchVersions(store.uow, source_reader=reader)
    task = asyncio.create_task(import_one(app, parent.id))
    await asyncio.wait_for(state.entered.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert (await app.list_transcripts(project_id=parent.id)).items == ()
    state.release.set()
    state.hold = False
    assert (await import_one(app, parent.id)).head.status == "sealed"


async def test_deleted_import_receipt_never_redownloads_or_resurrects(
    store: Store, cleanup: Cleanup, public_http: tuple[PublicService, PublicTranscriptSource]
) -> None:
    state, reader = public_http
    parent = await create_parent(store)
    app = WorkbenchVersions(store.uow, source_reader=reader)
    original = await import_one(app, parent.id)
    record = await delete(store, parent, DeletionTarget("workbench", "transcript", original.id, 1))
    for cleaned in (False, True):
        if cleaned:
            assert await cleanup.worker.run(record.id) is not None
        with pytest.raises(ResourceNotFoundError):
            await import_one(app, parent.id)
        assert state.reads == 2


async def test_nul_source_fails_before_final_transaction_and_same_key_remains_available(
    store: Store, public_http: tuple[PublicService, PublicTranscriptSource]
) -> None:
    state, reader = public_http
    state.content = b"a\x00b"
    parent = await create_parent(store)

    class CountTransactions:
        calls = 0

        def transaction(self) -> AbstractAsyncContextManager[VersionSession]:
            self.calls += 1
            return store.uow.transaction()

    unit = CountTransactions()
    app = WorkbenchVersions(unit, source_reader=reader)
    with pytest.raises(TranscriptSourceError, match=r"^source_invalid$"):
        await import_one(app, parent.id)
    assert unit.calls == 1, "invalid source opened a final transaction"
    assert (await store.app.list_transcripts(project_id=parent.id)).items == ()
    state.content = CONTENT
    assert (await import_one(app, parent.id)).head.text == TEXT
