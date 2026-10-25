"""Export jobs over the disposable PostgreSQL store.

Holding the store to stored state: which manifest an export pinned, what its
progress total counted, that a selection stays inside its own book, and that
erasing the project takes the job and its receipt with it.
"""

from __future__ import annotations

import hashlib
import io
import os
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import psycopg
import pytest
from minio import Minio
from psycopg import sql
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from tests.integration.test_workbench_versions_postgres import Store
from tests.integration.test_workbench_versions_postgres import store as store

from speechnexus_backend.workbench.application.books import WorkbenchBooks
from speechnexus_backend.workbench.application.exports import WorkbenchExports
from speechnexus_backend.workbench.application.render_units import WorkbenchRenderUnits
from speechnexus_backend.workbench.application.versions import (
    IdempotencyConflictError,
    ResourceNotFoundError,
    WorkbenchVersions,
)
from speechnexus_backend.workbench.domain.exports import (
    ExportArtifact,
    ExportError,
    ExportJob,
    ExportStateConflictError,
)
from speechnexus_backend.workbench.domain.versions import (
    InvalidVersionChangeError,
    RevisionConflictError,
)
from speechnexus_backend.workbench.infrastructure.books_schema import (
    books_migration_sql,
    grant_books_runtime_sql,
)
from speechnexus_backend.workbench.infrastructure.content_writes_schema import (
    content_writes_migration_sql,
    grant_content_writes_runtime_sql,
)
from speechnexus_backend.workbench.infrastructure.exports_schema import (
    exports_migration_sql,
    grant_exports_runtime_sql,
)
from speechnexus_backend.workbench.infrastructure.minio_object_zone import MinioObjectZone
from speechnexus_backend.workbench.infrastructure.postgres_books import PostgresBooks
from speechnexus_backend.workbench.infrastructure.postgres_exports import PostgresExports
from speechnexus_backend.workbench.infrastructure.postgres_render_units import (
    PostgresRenderUnits,
)
from speechnexus_backend.workbench.infrastructure.render_units_schema import (
    grant_render_units_runtime_sql,
    render_units_migration_sql,
)

pytestmark = pytest.mark.integration
WORKSPACE = "workspace-exports"
OTHER_WORKSPACE = "workspace-exports-other"
CREATE_KEY = "export-request-key-0001"
DIGEST = "sha256:" + "ab" * 32


@pytest.fixture
async def export_store(store: Store) -> AsyncIterator[Store]:
    """The shared partial fixture plus the book, render-unit and export fragments."""
    async with await psycopg.AsyncConnection.connect(store.owner_dsn, autocommit=True) as owner:
        await owner.execute(books_migration_sql(store.schema))
        await owner.execute(grant_books_runtime_sql(store.schema, store.role))
        await owner.execute(render_units_migration_sql(store.schema))
        await owner.execute(grant_render_units_runtime_sql(store.schema, store.role))
        await owner.execute(content_writes_migration_sql(store.schema))
        await owner.execute(grant_content_writes_runtime_sql(store.schema, store.role))
        await owner.execute(exports_migration_sql(store.schema))
        await owner.execute(grant_exports_runtime_sql(store.schema, store.role))
    yield store


def exports(store: Store) -> WorkbenchExports:
    return WorkbenchExports(PostgresExports(store.uow))


async def book(store: Store, *, suffix: str, units: int = 2) -> tuple[str, str, list[str]]:
    versions = WorkbenchVersions(store.uow)
    project = await versions.create_project(
        workspace_id=WORKSPACE,
        name="合成导出项目",
        idempotency_key=f"export-project-key-{suffix}",
    )
    books = WorkbenchBooks(PostgresBooks(store.uow))
    made = await books.create_book(
        project_id=project.id,
        workspace_id=WORKSPACE,
        title=f"合成有声书 {suffix}",
        key=f"export-book-key-{suffix}",
    )
    chapter = await books.create_chapter(
        book_id=made.id,
        workspace_id=WORKSPACE,
        title="第一章",
        ordinal=0,
        text="合成章节正文",
        key=f"export-chapter-key-{suffix}",
    )
    render = WorkbenchRenderUnits(PostgresRenderUnits(store.uow))
    unit_ids = [
        (
            await render.create_unit(
                workspace_id=WORKSPACE,
                chapter_id=chapter.id,
                ordinal=ordinal,
                text=f"合成旁白单元 {ordinal}",
                voice_id="voice_demo",
                voice_revision=1,
                model="tts-quality",
                model_binding_id="binding_demo",
                model_binding_revision=3,
                key=f"export-unit-key-{suffix}-{ordinal}",
            )
        ).id
        for ordinal in range(units)
    ]
    return project.id, made.id, unit_ids


async def create(store: Store, book_id: str, **overrides: Any) -> ExportJob:
    fields: dict[str, Any] = {
        "workspace_id": WORKSPACE,
        "book_id": book_id,
        "export_format": "zip",
        "expected_casting_revision": 1,
        "key": CREATE_KEY,
    }
    fields.update(overrides)
    return await exports(store).create_export(**fields)


async def stored(store: Store, export_id: str) -> dict[str, Any]:
    async with await psycopg.AsyncConnection.connect(
        store.owner_dsn, row_factory=dict_row
    ) as owner:
        row = await (
            await owner.execute(
                sql.SQL(
                    "SELECT status, export_format, manifest_revision, progress_completed,"
                    " progress_total, unit_ids, artifact, error FROM {}.export_jobs"
                    " WHERE id = %s"
                ).format(sql.Identifier(store.schema)),
                (export_id,),
            )
        ).fetchone()
    assert row is not None
    return row


async def rows(store: Store, table: str) -> int:
    async with await psycopg.AsyncConnection.connect(store.owner_dsn) as owner:
        row = await (
            await owner.execute(
                sql.SQL("SELECT count(*) FROM {}.{}").format(
                    sql.Identifier(store.schema), sql.Identifier(table)
                )
            )
        ).fetchone()
    assert row is not None
    return int(row[0])


async def test_create_pins_the_casting_revision_counts_every_unit_and_replays(
    export_store: Store,
) -> None:
    _, book_id, unit_ids = await book(export_store, suffix="one")
    assert len(unit_ids) == 2

    export = await create(export_store, book_id)
    assert (export.status, export.manifest_revision) == ("queued", 1)
    assert (export.progress_completed, export.progress_total) == (0, 2)
    assert (export.artifact, export.error) == (None, None)

    row = await stored(export_store, export.id)
    assert row["unit_ids"] is None
    assert (row["export_format"], row["status"]) == ("zip", "queued")
    assert int(row["progress_total"]) == 2

    replay = await create(export_store, book_id)
    assert replay == export
    with pytest.raises(IdempotencyConflictError):
        await create(export_store, book_id, export_format="mp3")
    assert await rows(export_store, "export_jobs") == 1
    assert await rows(export_store, "export_job_receipts") == 1


async def test_a_selection_stays_inside_its_own_book(export_store: Store) -> None:
    _, book_id, unit_ids = await book(export_store, suffix="one")
    _, other_book_id, other_units = await book(export_store, suffix="two")

    partial = await create(export_store, book_id, unit_ids=[unit_ids[0]], key="export-partial-key")
    assert partial.progress_total == 1
    assert (await stored(export_store, partial.id))["unit_ids"] == [unit_ids[0]]

    for foreign in ([unit_ids[0], other_units[0]], ["unit_absent"], [other_units[0]]):
        with pytest.raises(ResourceNotFoundError):
            await create(export_store, book_id, unit_ids=foreign, key=None)
    # Both books can still be exported whole.
    assert (await create(export_store, other_book_id, key="export-other-key")).progress_total == 2


async def test_a_stale_casting_revision_is_refused_and_the_pin_is_immutable(
    export_store: Store,
) -> None:
    _, book_id, _ = await book(export_store, suffix="one")
    export = await create(export_store, book_id)

    async with await psycopg.AsyncConnection.connect(export_store.owner_dsn) as owner:
        await owner.execute(
            sql.SQL("UPDATE {}.books SET casting_revision = 2 WHERE id = %s").format(
                sql.Identifier(export_store.schema)
            ),
            (book_id,),
        )
    with pytest.raises(RevisionConflictError) as caught:
        await create(export_store, book_id, key="export-next-key")
    assert (caught.value.expected, caught.value.actual) == (1, 2)

    # The accepted export keeps the manifest it was taken against.
    assert (
        await exports(export_store).get_export(export_id=export.id, workspace_id=WORKSPACE)
    ).manifest_revision == 1
    assert int((await stored(export_store, export.id))["manifest_revision"]) == 1


async def test_cancel_is_idempotent_and_a_finished_export_refuses_it(
    export_store: Store,
) -> None:
    _, book_id, _ = await book(export_store, suffix="one")
    export = await create(export_store, book_id)

    cancelling, changed = await exports(export_store).cancel_export(
        export_id=export.id, workspace_id=WORKSPACE
    )
    assert (cancelling.status, changed) == ("cancelling", True)
    assert (await stored(export_store, export.id))["status"] == "cancelling"
    again, changed_again = await exports(export_store).cancel_export(
        export_id=export.id, workspace_id=WORKSPACE
    )
    assert (again, changed_again) == (cancelling, False)

    # The worker path publishes the artifact; a finished export refuses cancellation.
    artifact = {
        "id": "artifact_demo",
        "export_id": export.id,
        "manifest_revision": 1,
        "media_type": "application/zip",
        "bytes": 2048,
        "sha256": DIGEST,
        "created_at": "2026-10-24T12:00:05+00:00",
    }
    async with await psycopg.AsyncConnection.connect(export_store.owner_dsn) as owner:
        await owner.execute(
            sql.SQL(
                "UPDATE {}.export_jobs SET status = 'succeeded', artifact = %s,"
                " progress_completed = progress_total, updated_at = now() WHERE id = %s"
            ).format(sql.Identifier(export_store.schema)),
            (Jsonb(artifact), export.id),
        )
    finished = await exports(export_store).get_export(export_id=export.id, workspace_id=WORKSPACE)
    assert finished.status == "succeeded"
    assert finished.artifact is not None
    assert (finished.artifact.bytes, finished.artifact.sha256) == (2048, DIGEST)
    assert finished.progress_completed == finished.progress_total
    with pytest.raises(ExportStateConflictError):
        await exports(export_store).cancel_export(export_id=export.id, workspace_id=WORKSPACE)
    with pytest.raises(ResourceNotFoundError):
        await exports(export_store).get_export(export_id=export.id, workspace_id=OTHER_WORKSPACE)


async def test_an_empty_book_cannot_be_exported_and_erasure_takes_the_rows(
    export_store: Store,
) -> None:
    _, empty_book_id, _ = await book(export_store, suffix="empty", units=0)
    with pytest.raises(InvalidVersionChangeError, match="empty workbench export"):
        await create(export_store, empty_book_id)

    project_id, book_id, _ = await book(export_store, suffix="one")
    assert (await create(export_store, book_id, key="export-erase-key")).progress_total == 2

    async with await psycopg.AsyncConnection.connect(export_store.owner_dsn) as owner:
        await owner.execute(
            sql.SQL("DELETE FROM {}.mutation_receipts WHERE project_id = %s").format(
                sql.Identifier(export_store.schema)
            ),
            (project_id,),
        )
        await owner.execute(
            sql.SQL("DELETE FROM {}.project_versions WHERE project_id = %s").format(
                sql.Identifier(export_store.schema)
            ),
            (project_id,),
        )
        await owner.execute(
            sql.SQL("DELETE FROM {}.projects WHERE id = %s").format(
                sql.Identifier(export_store.schema)
            ),
            (project_id,),
        )
    assert await rows(export_store, "export_jobs") == 0
    assert await rows(export_store, "export_job_receipts") == 0


async def test_the_worker_write_path_persists_every_transition(export_store: Store) -> None:
    _, book_id, unit_ids = await book(export_store, suffix="worker")
    export = await create(export_store, book_id, unit_ids=[unit_ids[0]], key="export-worker-key")
    service = exports(export_store)

    with pytest.raises(ExportStateConflictError):
        await service.record_export_progress(export_id=export.id, completed=1)

    running = await service.start_export(export_id=export.id)
    assert running.status == "running"
    with pytest.raises(ExportStateConflictError):
        await service.start_export(export_id=export.id)

    progressed = await service.record_export_progress(export_id=export.id, completed=1)
    assert (progressed.status, progressed.progress_completed) == ("running", 1)
    row = await stored(export_store, export.id)
    assert (row["status"], int(row["progress_completed"])) == ("running", 1)

    published = ExportArtifact(
        id="artifact_demo",
        export_id=export.id,
        manifest_revision=export.manifest_revision,
        media_type="application/zip",
        bytes=4096,
        sha256=DIGEST,
        created_at=datetime.now(UTC),
    )
    done = await service.complete_export(export_id=export.id, artifact=published)
    assert (done.status, done.artifact) == ("succeeded", published)
    assert done.progress_completed == done.progress_total
    stored_artifact = (await stored(export_store, export.id))["artifact"]
    assert (stored_artifact["sha256"], int(stored_artifact["bytes"])) == (DIGEST, 4096)
    with pytest.raises(ExportStateConflictError):
        await service.complete_export(export_id=export.id, artifact=published)

    expired = await service.expire_export(export_id=export.id)
    assert (expired.status, expired.artifact) == ("expired", None)
    assert (await stored(export_store, export.id))["artifact"] is None
    with pytest.raises(ExportStateConflictError):
        await service.expire_export(export_id=export.id)

    # A failure carries its own stable detail and cannot be expired as a success.
    other = await create(export_store, book_id, key="export-worker-fail-key")
    await service.start_export(export_id=other.id)
    failure = ExportError("unavailable", "合成导出失败", True)
    failed = await service.fail_export(export_id=other.id, error=failure)
    assert (failed.status, failed.error) == ("failed", failure)
    assert (await stored(export_store, other.id))["error"]["code"] == "unavailable"
    with pytest.raises(ExportStateConflictError):
        await service.expire_export(export_id=other.id)

    moves: list[Callable[[], Awaitable[ExportJob]]] = [
        lambda: service.start_export(export_id="export_absent"),
        lambda: service.record_export_progress(export_id="export_absent", completed=1),
        lambda: service.complete_export(export_id="export_absent", artifact=published),
        lambda: service.fail_export(export_id="export_absent", error=failure),
        lambda: service.expire_export(export_id="export_absent"),
    ]
    for move in moves:
        with pytest.raises(ResourceNotFoundError):
            await move()


async def test_a_published_bundle_becomes_the_recorded_artifact(export_store: Store) -> None:
    endpoint = os.environ.get("SPEECHNEXUS_TEST_MINIO_ENDPOINT")
    access = os.environ.get("SPEECHNEXUS_TEST_MINIO_ACCESS_KEY")
    secret = os.environ.get("SPEECHNEXUS_TEST_MINIO_SECRET_KEY")
    assert endpoint and access and secret, "isolated MinIO service is required for object evidence"
    bucket = f"workbench-export-{uuid4().hex}"
    zone = MinioObjectZone(endpoint, bucket, access, secret, prefix="exports")
    client = Minio(
        endpoint.replace("http://", "").replace("https://", ""),
        access_key=access,
        secret_key=secret,
        secure=endpoint.startswith("https://"),
    )
    # Each independently selected shard must provision its own object fixture.
    client.make_bucket(bucket)
    try:
        _, book_id, unit_ids = await book(export_store, suffix="publish")
        export = await create(export_store, book_id, unit_ids=unit_ids, key="export-publish-key")
        service = exports(export_store)
        await service.start_export(export_id=export.id)

        # Synthetic bundle bytes: real objects, no claim about real audio content.
        body = b"synthetic export bundle payload\n" * 8
        digest = "sha256:" + hashlib.sha256(body).hexdigest()
        key = f"{book_id}/{export.id}/artifact_demo"
        await zone.put(key=key, body=io.BytesIO(body), length=len(body))

        published = ExportArtifact(
            id="artifact_demo",
            export_id=export.id,
            manifest_revision=export.manifest_revision,
            media_type="application/zip",
            bytes=len(body),
            sha256=digest,
            created_at=datetime.now(UTC),
        )
        done = await service.complete_export(export_id=export.id, artifact=published)
        assert done.artifact == published
        assert (await stored(export_store, export.id))["artifact"]["sha256"] == digest

        # The recorded artifact must describe bytes the zone really holds.
        response = client.get_object(bucket, f"exports/{key}")
        try:
            assert response.read() == body
        finally:
            response.close()
            response.release_conn()
        await zone.erase_prefix(prefix=f"{book_id}/")
    finally:
        zone.close()
        for item in client.list_objects(bucket, recursive=True):
            client.remove_object(bucket, item.object_name)
        client.remove_bucket(bucket)
