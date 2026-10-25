"""Real loopback HTTP artifact copies, the disposable PostgreSQL store and MinIO."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass, field

import httpx
import psycopg
import pytest
from minio import Minio
from psycopg import sql
from psycopg.conninfo import make_conninfo
from tests.integration.test_workbench_versions_postgres import Store

from speechnexus_backend.workbench.application.saved_assets import WorkbenchSavedAssets
from speechnexus_backend.workbench.application.speech_sources import SpeechSourceError
from speechnexus_backend.workbench.application.versions import (
    IdempotencyConflictError,
    ResourceNotFoundError,
    WorkbenchVersions,
)
from speechnexus_backend.workbench.domain.saved_assets import (
    project_object_prefix,
    saved_object_key,
)
from speechnexus_backend.workbench.infrastructure.deletion_schema import (
    deletion_migration_sql,
    grant_deletion_runtime_sql,
)
from speechnexus_backend.workbench.infrastructure.minio_object_zone import MinioObjectZone
from speechnexus_backend.workbench.infrastructure.postgres_saved_assets import PostgresSavedAssets
from speechnexus_backend.workbench.infrastructure.postgres_versions import PostgresVersionUnitOfWork
from speechnexus_backend.workbench.infrastructure.public_artifact_source import (
    PublicArtifactSource,
)
from speechnexus_backend.workbench.infrastructure.saved_assets_schema import (
    grant_saved_assets_runtime_sql,
    saved_assets_migration_sql,
)
from speechnexus_backend.workbench.infrastructure.version_schema import (
    grant_runtime_sql,
    migration_sql,
)

pytestmark = pytest.mark.integration
CONTENT = b"synthetic audio payload" * 8
DIGEST = "sha256:" + hashlib.sha256(CONTENT).hexdigest()
ARTIFACT = "artifact-saved-one"
RECEIPT = "receipt-saved-one"
WORKSPACE = "workspace-saved"
KEY = "saved-asset-key-0001"
BUCKET = "speechnexus-workbench-saved"


@pytest.fixture
async def saved_store() -> AsyncIterator[Store]:
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
        schema = "wb_saved_" + os.urandom(6).hex()
        role = "wb_saved_runtime_" + os.urandom(6).hex()
        await owner.execute(sql.SQL("CREATE ROLE {} LOGIN").format(sql.Identifier(role)))
        await owner.execute(migration_sql(schema))
        await owner.execute(deletion_migration_sql(schema))
        await owner.execute(saved_assets_migration_sql(schema))
        await owner.execute(grant_runtime_sql(schema, role))
        await owner.execute(grant_deletion_runtime_sql(schema, role))
        await owner.execute(grant_saved_assets_runtime_sql(schema, role))
        try:
            yield Store(
                dsn,
                schema,
                role,
                PostgresVersionUnitOfWork(make_conninfo(dsn, user=role), schema=schema),
            )
        finally:
            await owner.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))
            await owner.execute(sql.SQL("DROP OWNED BY {}").format(sql.Identifier(role)))
            await owner.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(role)))


@dataclass
class Objects:
    zone: MinioObjectZone
    client: Minio

    def names(self, project_id: str) -> list[str]:
        prefix = f"saved/{project_object_prefix(project_id)}"
        return sorted(item.object_name for item in self.client.list_objects(BUCKET, prefix=prefix))

    def read(self, project_id: str, sha256: str) -> bytes:
        response = self.client.get_object(BUCKET, f"saved/{saved_object_key(project_id, sha256)}")
        try:
            return response.read()
        finally:
            response.close()
            response.release_conn()


@pytest.fixture(scope="module")
def objects() -> Iterator[Objects]:
    endpoint = os.environ.get("SPEECHNEXUS_TEST_MINIO_ENDPOINT")
    access = os.environ.get("SPEECHNEXUS_TEST_MINIO_ACCESS_KEY")
    secret = os.environ.get("SPEECHNEXUS_TEST_MINIO_SECRET_KEY")
    assert endpoint and access and secret, "isolated MinIO service is required for object evidence"
    zone = MinioObjectZone(endpoint, BUCKET, access, secret, prefix="saved")
    client = Minio(
        endpoint.replace("http://", "").replace("https://", ""),
        access_key=access,
        secret_key=secret,
        secure=endpoint.startswith("https://"),
    )
    if not client.bucket_exists(BUCKET):
        client.make_bucket(BUCKET)
    try:
        yield Objects(zone, client)
    finally:
        zone.close()


@dataclass
class PublicService:
    status: int = 200
    receipt: str = RECEIPT
    expires_at: str | None = None
    media_type: str = "audio/wav"
    digest: str = DIGEST
    content: bytes = CONTENT
    reads: int = 0
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
            if path == f"/v1/nexus/artifacts/{ARTIFACT}".encode():
                body = json.dumps(
                    {
                        "id": ARTIFACT,
                        "object": "nexus.artifact",
                        "workspace_id": WORKSPACE,
                        "kind": "audio",
                        "status": "ready",
                        "media_type": self.media_type,
                        "bytes": len(self.content),
                        "sha256": self.digest,
                        "receipt_id": self.receipt,
                        "created_at": "2026-10-24T00:00:00Z",
                        **({} if self.expires_at is None else {"expires_at": self.expires_at}),
                    }
                ).encode()
                self._write(writer, b"application/json", body)
            else:
                assert path == f"/v1/nexus/artifacts/{ARTIFACT}/content".encode()
                self._write(writer, b"application/octet-stream", self.content)
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
                pass
            finally:
                self.tasks.discard(task)

    def _write(self, writer: asyncio.StreamWriter, media: bytes, body: bytes) -> None:
        writer.write(
            b"HTTP/1.1 "
            + str(self.status).encode()
            + b" OK\r\nContent-Type: "
            + media
            + b"\r\nConnection: close\r\nContent-Length: "
            + str(len(body)).encode()
            + b"\r\n\r\n"
            + body
        )


@pytest.fixture
async def public_http() -> AsyncIterator[tuple[PublicService, PublicArtifactSource]]:
    state = PublicService()
    server = await asyncio.start_server(state.connection, "127.0.0.1", 0)
    host, port = server.sockets[0].getsockname()[:2]
    try:
        async with httpx.AsyncClient(
            base_url=f"http://{host}:{port}", trust_env=False, follow_redirects=False
        ) as client:
            yield state, PublicArtifactSource(client)
    finally:
        server.close()
        await server.wait_closed()
        if state.tasks:
            await asyncio.wait_for(asyncio.gather(*state.tasks), 2)
        assert not state.errors


def service(store: Store, source: PublicArtifactSource, objects: Objects) -> WorkbenchSavedAssets:
    return WorkbenchSavedAssets(PostgresSavedAssets(store.uow), source, objects.zone)


async def project(store: Store) -> str:
    created = await WorkbenchVersions(store.uow).create_project(
        workspace_id=WORKSPACE, name="合成项目", idempotency_key="project-key"
    )
    return created.id


async def test_verified_copy_is_pinned_replayed_without_refetch_and_isolated_per_workspace(
    saved_store: Store,
    objects: Objects,
    public_http: tuple[PublicService, PublicArtifactSource],
) -> None:
    state, source = public_http
    app = service(saved_store, source, objects)
    parent = await project(saved_store)
    asset = await app.save(
        project_id=parent,
        workspace_id=WORKSPACE,
        source_artifact_id=ARTIFACT,
        source_receipt_id=RECEIPT,
        display_name="合成第一章",
        key=KEY,
    )
    assert state.reads == 2
    assert asset.copy.sha256 == DIGEST and asset.copy.bytes == len(CONTENT)
    assert objects.names(parent) == [f"saved/{saved_object_key(parent, DIGEST)}"]
    assert objects.read(parent, DIGEST) == CONTENT

    # Replay returns the committed copy without touching the source or the object zone.
    replay = await app.save(
        project_id=parent,
        workspace_id=WORKSPACE,
        source_artifact_id=ARTIFACT,
        source_receipt_id=RECEIPT,
        display_name="合成第一章",
        key=KEY,
    )
    assert replay == asset and state.reads == 2

    with pytest.raises(IdempotencyConflictError):
        await app.save(
            project_id=parent,
            workspace_id=WORKSPACE,
            source_artifact_id=ARTIFACT,
            source_receipt_id=RECEIPT,
            display_name="另一个名字",
            key=KEY,
        )
    assert await app.get(asset_id=asset.id, workspace_id=WORKSPACE) == asset
    with pytest.raises(ResourceNotFoundError):
        await app.get(asset_id=asset.id, workspace_id="workspace-other")
    with pytest.raises(ResourceNotFoundError):
        await app.save(
            project_id="project-absent",
            workspace_id=WORKSPACE,
            source_artifact_id=ARTIFACT,
            source_receipt_id=RECEIPT,
        )
    async with await psycopg.AsyncConnection.connect(saved_store.owner_dsn) as owner:
        rows = await (
            await owner.execute(
                sql.SQL("SELECT id, project_id, bytes, sha256 FROM {}.saved_assets").format(
                    sql.Identifier(saved_store.schema)
                )
            )
        ).fetchall()
        receipts = await (
            await owner.execute(
                sql.SQL("SELECT count(*) FROM {}.saved_asset_receipts").format(
                    sql.Identifier(saved_store.schema)
                )
            )
        ).fetchone()
    assert rows == [(asset.id, parent, len(CONTENT), DIGEST)]
    assert receipts == (1,)


async def test_source_identity_receipt_and_expiry_failures_pin_nothing(
    saved_store: Store,
    objects: Objects,
    public_http: tuple[PublicService, PublicArtifactSource],
) -> None:
    state, source = public_http
    app = service(saved_store, source, objects)
    parent = await project(saved_store)
    state.receipt = "receipt-other"
    with pytest.raises(SpeechSourceError) as mismatch:
        await app.save(
            project_id=parent,
            workspace_id=WORKSPACE,
            source_artifact_id=ARTIFACT,
            source_receipt_id=RECEIPT,
        )
    assert mismatch.value.code == "source_invalid"
    state.receipt = RECEIPT
    state.digest = "sha256:" + "c" * 64
    with pytest.raises(SpeechSourceError) as digest:
        await app.save(
            project_id=parent,
            workspace_id=WORKSPACE,
            source_artifact_id=ARTIFACT,
            source_receipt_id=RECEIPT,
        )
    assert digest.value.code == "source_invalid"
    state.digest = DIGEST
    state.expires_at = "2020-01-01T00:00:00Z"
    with pytest.raises(SpeechSourceError) as expired:
        await app.save(
            project_id=parent,
            workspace_id=WORKSPACE,
            source_artifact_id=ARTIFACT,
            source_receipt_id=RECEIPT,
        )
    assert expired.value.code == "source_expired"
    assert objects.names(parent) == []
    async with await psycopg.AsyncConnection.connect(saved_store.owner_dsn) as owner:
        count = await (
            await owner.execute(
                sql.SQL("SELECT count(*) FROM {}.saved_assets").format(
                    sql.Identifier(saved_store.schema)
                )
            )
        ).fetchone()
    assert count == (0,)


async def test_project_erasure_removes_rows_and_its_object_prefix(
    saved_store: Store,
    objects: Objects,
    public_http: tuple[PublicService, PublicArtifactSource],
) -> None:
    _, source = public_http
    app = service(saved_store, source, objects)
    parent = await project(saved_store)
    asset = await app.save(
        project_id=parent,
        workspace_id=WORKSPACE,
        source_artifact_id=ARTIFACT,
        source_receipt_id=RECEIPT,
        key=KEY,
    )
    assert objects.names(parent) == [f"saved/{saved_object_key(parent, DIGEST)}"]

    # The owner row deletion is the cascade the cleanup worker commits.
    async with await psycopg.AsyncConnection.connect(saved_store.owner_dsn) as owner:
        for statement in (
            "DELETE FROM {}.mutation_receipts WHERE project_id = %s",
            "DELETE FROM {}.project_versions WHERE project_id = %s",
            "DELETE FROM {}.projects WHERE id = %s",
        ):
            await owner.execute(
                sql.SQL(statement).format(sql.Identifier(saved_store.schema)), (parent,)
            )
        for table in ("saved_assets", "saved_asset_receipts"):
            count = await (
                await owner.execute(
                    sql.SQL("SELECT count(*) FROM {}.{}").format(
                        sql.Identifier(saved_store.schema), sql.Identifier(table)
                    )
                )
            ).fetchone()
            assert count == (0,)
    with pytest.raises(ResourceNotFoundError):
        await app.get(asset_id=asset.id, workspace_id=WORKSPACE)

    # The worker erases objects after the row commit; a retry is idempotent.
    assert await objects.zone.erase_prefix(prefix=project_object_prefix(parent)) == 1
    assert objects.names(parent) == []
    assert await objects.zone.erase_prefix(prefix=project_object_prefix(parent)) == 0


async def test_read_back_returns_the_committed_record_and_hides_unknown_or_foreign_ids(
    saved_store: Store,
    objects: Objects,
    public_http: tuple[PublicService, PublicArtifactSource],
) -> None:
    """The workbench's own copy is readable by id, without refetching from the base."""
    state, source = public_http
    app = service(saved_store, source, objects)
    parent = await project(saved_store)
    saved = await app.save(
        project_id=parent,
        workspace_id=WORKSPACE,
        source_artifact_id=ARTIFACT,
        source_receipt_id=RECEIPT,
        display_name="合成第一章",
        key=KEY,
    )
    reads_after_save = state.reads

    read = await app.get(asset_id=saved.id, workspace_id=WORKSPACE)
    assert read == saved
    # A read-back is a local read: the base is not asked again for the artifact.
    assert state.reads == reads_after_save
    # An unknown id and another workspace's id are both not-found, never an empty record.
    with pytest.raises(ResourceNotFoundError):
        await app.get(asset_id="asset_saved_unknown", workspace_id=WORKSPACE)
    with pytest.raises(ResourceNotFoundError):
        await app.get(asset_id=saved.id, workspace_id="workspace-other")
