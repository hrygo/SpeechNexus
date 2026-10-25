"""Project cleanup erases its own object prefix after the row commit, and on retry."""

from __future__ import annotations

import io
import os
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass, field
from typing import Any

import psycopg
import pytest
from minio import Minio
from psycopg import sql
from tests.integration.test_workbench_deletion_cleanup import Cleanup
from tests.integration.test_workbench_deletion_cleanup import cleanup as cleanup
from tests.integration.test_workbench_deletions_postgres import content, service, target
from tests.integration.test_workbench_versions_postgres import Store
from tests.integration.test_workbench_versions_postgres import store as store

from speechnexus_backend.workbench.domain.saved_assets import (
    project_object_prefix,
    saved_object_key,
)
from speechnexus_backend.workbench.infrastructure.deletion_codec import plan_digest
from speechnexus_backend.workbench.infrastructure.minio_object_zone import MinioObjectZone
from speechnexus_backend.workbench.infrastructure.postgres_deletion_cleanup import (
    PostgresDeletionCleanup,
)
from speechnexus_backend.workbench.infrastructure.postgres_versions import PostgresVersionUnitOfWork
from speechnexus_backend.workbench.infrastructure.runtime_schema import cleanup_grants_sql
from speechnexus_backend.workbench.infrastructure.saved_assets_schema import (
    saved_assets_migration_sql,
)

pytestmark = pytest.mark.integration
DIGEST = "sha256:" + "d" * 64
CONTENT = b"synthetic committed copy"
BUCKET = "speechnexus-workbench-saved"


@pytest.fixture
async def assets_store(store: Store, cleanup: Cleanup) -> AsyncIterator[Store]:
    """The shared partial fixture plus the module-owned saved-asset fragment."""
    async with await psycopg.AsyncConnection.connect(store.owner_dsn, autocommit=True) as owner:
        await owner.execute(saved_assets_migration_sql(store.schema))
        # The erasing worker gets the composed grant set, so a deployment that uses it
        # cannot miss the saved-asset read privilege this sweep needs.
        await owner.execute(cleanup_grants_sql(store.schema, store.role + "_cleanup"))
    yield store


@dataclass
class Zone:
    """Records each prefix and whether the deletion transaction was already committed."""

    deletion_id: str = ""
    dsn: str = field(repr=False, default="")
    schema: str = ""
    prefixes: list[str] = field(default_factory=list)
    committed: list[bool] = field(default_factory=list)

    async def put(self, **kw: Any) -> None:
        raise AssertionError("project cleanup never writes objects")

    async def erase_prefix(self, *, prefix: str) -> int:
        self.prefixes.append(prefix)
        if self.dsn:
            # A separate connection only sees the row once the erasing transaction has
            # committed, so object I/O inside that transaction would be observed here.
            async with await psycopg.AsyncConnection.connect(self.dsn, autocommit=True) as other:
                row = await (
                    await other.execute(
                        sql.SQL(
                            "SELECT completed_at FROM {}.deletion_outbox WHERE deletion_id = %s"
                        ).format(sql.Identifier(self.schema)),
                        (self.deletion_id,),
                    )
                ).fetchone()
            self.committed.append(row is not None and row[0] is not None)
        return 1


@dataclass
class Objects:
    zone: MinioObjectZone
    client: Minio

    def names(self, project_id: str) -> list[str]:
        prefix = f"saved/{project_object_prefix(project_id)}"
        return sorted(item.object_name for item in self.client.list_objects(BUCKET, prefix=prefix))


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


async def accept(store: Store, project: Any) -> Any:
    app = service(store)
    plan = await app.preview(
        workspace_id=project.workspace_id, target=target(project), mode="cascade", key="plan-key"
    )
    return await app.accept(
        workspace_id=project.workspace_id,
        plan_id=plan.id,
        expected_revision=1,
        digest=plan_digest(plan),
        targets=plan.confirmed_targets,
        key="delete-key",
    )


async def test_project_cleanup_calls_object_erasure_once_per_attempt(
    assets_store: Store, cleanup: Cleanup
) -> None:
    project, _ = await content(assets_store)
    record = await accept(assets_store, project)
    zone = Zone(deletion_id=record.id, dsn=cleanup.dsn, schema=assets_store.schema)
    worker = PostgresDeletionCleanup(
        PostgresVersionUnitOfWork(cleanup.dsn, schema=assets_store.schema), objects=zone
    )

    result = await worker.run(record.id)
    assert result is not None
    # The object half of the promise is recorded once the sweep finishes.
    assert zone.prefixes == [project_object_prefix(project.id)]
    # Row evidence commits before the object sweep: a peer connection already sees it.
    assert zone.committed == [True]
    # The sweep publishes its own evidence once it finishes, so a settled retry replays
    # the ledger without listing the prefix again: at-least-once is preserved by the
    # marker, not by repeating object I/O on every call.
    assert await worker.run(record.id) == result
    assert zone.prefixes == [project_object_prefix(project.id)]
    assert zone.committed == [True]
    # The object half carries its own evidence, so the owed set stays empty afterwards.
    async with await psycopg.AsyncConnection.connect(assets_store.owner_dsn) as owner:
        stamped = await (
            await owner.execute(
                sql.SQL(
                    "SELECT objects_erased_at IS NOT NULL FROM {}.deletion_outbox"
                    " WHERE deletion_id = %s"
                ).format(sql.Identifier(assets_store.schema)),
                (record.id,),
            )
        ).fetchone()
    assert stamped == (True,)


async def test_project_cleanup_removes_the_committed_object_bytes(
    assets_store: Store, cleanup: Cleanup, objects: Objects
) -> None:
    project, _ = await content(assets_store)
    key = saved_object_key(project.id, DIGEST)
    await objects.zone.put(key=key, body=io.BytesIO(CONTENT), length=len(CONTENT))
    assert objects.names(project.id) == [f"saved/{key}"]
    record = await accept(assets_store, project)

    worker = PostgresDeletionCleanup(
        PostgresVersionUnitOfWork(cleanup.dsn, schema=assets_store.schema), objects=objects.zone
    )
    result = await worker.run(record.id)
    assert result is not None
    assert objects.names(project.id) == []
    assert await worker.run(record.id) == result
    async with await psycopg.AsyncConnection.connect(assets_store.owner_dsn) as owner:
        count = await (
            await owner.execute(
                sql.SQL("SELECT count(*) FROM {}.saved_assets").format(
                    sql.Identifier(assets_store.schema)
                )
            )
        ).fetchone()
    assert count == (0,)
