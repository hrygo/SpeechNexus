"""The export worker over disposable PostgreSQL and the real object zone.

The producer here is a test-only adapter: it proves the worker's own behaviour
(claim, progress, publish, settle) without claiming any real audio was produced.
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Iterator
from io import BytesIO
from typing import Any

import pytest
from minio import Minio
from tests.integration.test_workbench_exports_postgres import book, create
from tests.integration.test_workbench_exports_postgres import export_store as export_store
from tests.integration.test_workbench_versions_postgres import Store
from tests.integration.test_workbench_versions_postgres import store as store

from speechnexus_backend.workbench.application.export_worker import (
    ExportCancelledError,
    ExportWorker,
    ProducedExport,
)
from speechnexus_backend.workbench.application.exports import WorkbenchExports
from speechnexus_backend.workbench.domain.exports import ExportJob
from speechnexus_backend.workbench.infrastructure.minio_object_zone import MinioObjectZone
from speechnexus_backend.workbench.infrastructure.postgres_exports import PostgresExports

pytestmark = pytest.mark.integration
WORKSPACE = "workspace-exports"
CONTENT = b"synthetic export bundle payload\n" * 4
DIGEST = "sha256:" + hashlib.sha256(CONTENT).hexdigest()
BUCKET = "speechnexus-workbench-saved"


class _Bundle:
    """Test-only producer: reports every requested item, then returns synthetic bytes."""

    def __init__(self, *, items: int = 2, error: Exception | None = None) -> None:
        self.items = items
        self.error = error

    async def produce_export(self, job: ExportJob, *, report: Any) -> ProducedExport:
        for item in range(1, self.items + 1):
            await report(item)
        if self.error is not None:
            raise self.error
        return ProducedExport(content=CONTENT, media_type="application/zip")


class _Cancelling(_Bundle):
    """Reports one item, asks for cancellation, then keeps reporting."""

    def __init__(self, service: WorkbenchExports, *, items: int = 2) -> None:
        super().__init__(items=items)
        self._service = service

    async def produce_export(self, job: ExportJob, *, report: Any) -> ProducedExport:
        await report(1)
        await self._service.cancel_export(export_id=job.id, workspace_id=WORKSPACE)
        await report(2)
        return ProducedExport(content=CONTENT, media_type="application/zip")


class _Zone:
    def __init__(self, zone: MinioObjectZone, client: Minio) -> None:
        self.zone = zone
        self.client = client

    def read(self, key: str) -> bytes:
        response = self.client.get_object(BUCKET, f"exports/{key}")
        try:
            return response.read()
        finally:
            response.close()
            response.release_conn()


@pytest.fixture
def zone() -> Iterator[_Zone]:
    endpoint = os.environ.get("SPEECHNEXUS_TEST_MINIO_ENDPOINT")
    access = os.environ.get("SPEECHNEXUS_TEST_MINIO_ACCESS_KEY")
    secret = os.environ.get("SPEECHNEXUS_TEST_MINIO_SECRET_KEY")
    assert endpoint and access and secret, "isolated MinIO service is required for object evidence"
    client = Minio(
        endpoint.replace("http://", "").replace("https://", ""),
        access_key=access,
        secret_key=secret,
        secure=endpoint.startswith("https://"),
    )
    if not client.bucket_exists(BUCKET):
        client.make_bucket(BUCKET)
    zone = MinioObjectZone(endpoint, BUCKET, access, secret, prefix="exports")
    try:
        yield _Zone(zone, client)
    finally:
        zone.close()


def worker(store: Store, zone: _Zone, producer: Any | None) -> ExportWorker:
    return ExportWorker(PostgresExports(store.uow), zone.zone, producer)


async def test_a_queued_export_is_claimed_produced_published_and_completed(
    export_store: Store, zone: _Zone
) -> None:
    project_id, book_id, _ = await book(export_store, suffix="worker")
    export = await create(export_store, book_id, key="export-worker-run-key")
    assert export.progress_total == 2

    assert await worker(export_store, zone, _Bundle()).run_once() == 1
    done = await WorkbenchExports(PostgresExports(export_store.uow)).get_export(
        export_id=export.id, workspace_id=WORKSPACE
    )
    assert done.status == "succeeded"
    assert done.progress_completed == done.progress_total == 2
    assert done.artifact is not None
    assert (done.artifact.bytes, done.artifact.sha256) == (len(CONTENT), DIGEST)
    assert done.artifact.manifest_revision == done.manifest_revision
    assert done.artifact.media_type == "application/zip"

    # The published object really holds the produced bytes.
    key = f"{project_id}/exports/{export.id}/{done.artifact.id}"
    assert zone.read(key) == CONTENT

    # The claim moved it out of the queue, so a second pass has nothing to do.
    assert await worker(export_store, zone, _Bundle()).run_once() == 0

    await zone.zone.erase_prefix(prefix=f"{project_id}/")


async def test_a_cancellation_during_production_settles_the_export(
    export_store: Store, zone: _Zone
) -> None:
    project_id, book_id, _ = await book(export_store, suffix="cancel")
    export = await create(export_store, book_id, key="export-worker-cancel-key")
    service = WorkbenchExports(PostgresExports(export_store.uow))

    assert await worker(export_store, zone, _Cancelling(service)).run_once() == 1
    settled = await service.get_export(export_id=export.id, workspace_id=WORKSPACE)
    # Cancellation is noticed at the next reported item, not instantly.
    assert (settled.status, settled.artifact) == ("cancelled", None)
    await zone.zone.erase_prefix(prefix=f"{project_id}/")


async def test_a_producer_failure_is_recorded_without_publishing_anything(
    export_store: Store, zone: _Zone
) -> None:
    _, book_id, _ = await book(export_store, suffix="fail")
    export = await create(export_store, book_id, key="export-worker-fail-key")

    failure = RuntimeError("provider said: secret endpoint http://internal")
    assert await worker(export_store, zone, _Bundle(error=failure)).run_once() == 1
    failed = await WorkbenchExports(PostgresExports(export_store.uow)).get_export(
        export_id=export.id, workspace_id=WORKSPACE
    )
    assert failed.status == "failed"
    assert failed.artifact is None
    assert failed.error is not None
    assert failed.error.code == "unavailable"
    assert "secret endpoint" not in failed.error.message


async def test_an_abort_without_a_real_cancellation_is_recorded_as_failed(
    export_store: Store, zone: _Zone
) -> None:
    _, book_id, _ = await book(export_store, suffix="selfcancel")
    export = await create(export_store, book_id, key="export-worker-self-key")
    assert await worker(export_store, zone, _Bundle(error=ExportCancelledError())).run_once() == 1
    recorded = await WorkbenchExports(PostgresExports(export_store.uow)).get_export(
        export_id=export.id, workspace_id=WORKSPACE
    )
    # No cancellation was ever requested, so the export must not claim to be cancelled.
    assert recorded.status == "failed"
    assert recorded.error is not None
    assert recorded.error.code == "aborted"


async def test_without_a_producer_the_queue_is_left_alone(export_store: Store, zone: _Zone) -> None:
    project_id, book_id, _ = await book(export_store, suffix="off")
    export = await create(export_store, book_id, key="export-worker-off-key")

    assert await worker(export_store, zone, None).run_once() == 0
    untouched = await WorkbenchExports(PostgresExports(export_store.uow)).get_export(
        export_id=export.id, workspace_id=WORKSPACE
    )
    # An unconfigured deployment must never invent an artifact.
    assert (untouched.status, untouched.artifact) == ("queued", None)
    assert await worker(export_store, zone, _Bundle()).run_once() == 1
    await zone.zone.erase_prefix(prefix=f"{project_id}/")


@pytest.mark.parametrize(("content", "media_type"), [(b"", "application/zip"), (CONTENT, "")])
async def test_invalid_output_is_durably_failed_without_an_object(
    export_store: Store, zone: _Zone, content: bytes, media_type: str
) -> None:
    class InvalidBundle(_Bundle):
        async def produce_export(self, job: ExportJob, *, report: Any) -> ProducedExport:
            await super().produce_export(job, report=report)
            return ProducedExport(content=content, media_type=media_type)

    project_id, book_id, _ = await book(export_store, suffix="invalid-output")
    accepted = await create(export_store, book_id, key="export-invalid-output-key")
    assert await worker(export_store, zone, InvalidBundle()).run_once() == 1

    failed = await WorkbenchExports(PostgresExports(export_store.uow)).get_export(
        export_id=accepted.id, workspace_id=WORKSPACE
    )
    assert failed.status == "failed" and failed.artifact is None
    assert failed.error is not None and failed.error.code == "unavailable"
    assert (
        list(zone.client.list_objects(BUCKET, prefix=f"exports/{project_id}/", recursive=True))
        == []
    )
    assert await worker(export_store, zone, _Bundle()).run_once() == 0


async def test_one_failed_object_write_does_not_strand_the_rest_of_the_claimed_batch(
    export_store: Store, zone: _Zone
) -> None:
    class FailFirstWrite:
        def __init__(self) -> None:
            self.attempts = 0

        async def put(self, *, key: str, body: BytesIO, length: int) -> None:
            self.attempts += 1
            if self.attempts == 1:
                raise OSError("synthetic private object outage")
            await zone.zone.put(key=key, body=body, length=length)

    project_id, book_id, _ = await book(export_store, suffix="write-failure")
    first = await create(export_store, book_id, key="export-write-failure-first")
    second = await create(export_store, book_id, key="export-write-failure-second")
    assert (
        await ExportWorker(
            PostgresExports(export_store.uow), FailFirstWrite(), _Bundle()
        ).run_once()
        == 2
    )

    service = WorkbenchExports(PostgresExports(export_store.uow))
    failed = await service.get_export(export_id=first.id, workspace_id=WORKSPACE)
    succeeded = await service.get_export(export_id=second.id, workspace_id=WORKSPACE)
    assert failed.status == "failed" and failed.artifact is None
    assert failed.error is not None and failed.error.message == "export production failed"
    assert succeeded.status == "succeeded" and succeeded.artifact is not None
    assert zone.read(f"{project_id}/exports/{second.id}/{succeeded.artifact.id}") == CONTENT
    assert await worker(export_store, zone, _Bundle()).run_once() == 0
    await zone.zone.erase_prefix(prefix=f"{project_id}/")


async def test_a_lost_completion_reply_preserves_the_committed_artifact(
    export_store: Store, zone: _Zone
) -> None:
    class LostCompletionReply(PostgresExports):
        async def complete_export(self, *, export_id: str, artifact: Any) -> ExportJob:
            await super().complete_export(export_id=export_id, artifact=artifact)
            raise OSError("synthetic lost completion reply")

    project_id, book_id, _ = await book(export_store, suffix="lost-completion")
    accepted = await create(export_store, book_id, key="export-lost-completion-key")
    with pytest.raises(OSError, match="synthetic lost completion reply"):
        await ExportWorker(LostCompletionReply(export_store.uow), zone.zone, _Bundle()).run_once()

    recovered = await WorkbenchExports(PostgresExports(export_store.uow)).get_export(
        export_id=accepted.id, workspace_id=WORKSPACE
    )
    assert recovered.status == "succeeded" and recovered.error is None
    assert recovered.artifact is not None
    assert zone.read(f"{project_id}/exports/{accepted.id}/{recovered.artifact.id}") == CONTENT
    assert await worker(export_store, zone, _Bundle()).run_once() == 0
    await zone.zone.erase_prefix(prefix=f"{project_id}/")


async def test_cancellation_while_put_is_in_flight_settles_after_confirmed_write(
    export_store: Store, zone: _Zone
) -> None:
    project_id, book_id, _ = await book(export_store, suffix="late-cancel")
    export = await create(export_store, book_id, key="late-cancel-original-key")
    service = WorkbenchExports(PostgresExports(export_store.uow))

    class CancelDuringPut:
        async def put(self, *, key: str, body: BytesIO, length: int) -> None:
            await service.cancel_export(export_id=export.id, workspace_id=WORKSPACE)
            await zone.zone.put(key=key, body=body, length=length)

    assert (
        await ExportWorker(
            PostgresExports(export_store.uow), CancelDuringPut(), _Bundle()
        ).run_once()
        == 1
    )
    done = await service.get_export(export_id=export.id, workspace_id=WORKSPACE)
    assert done.status == "cancelled" and done.artifact is None
    await zone.zone.erase_prefix(prefix=f"{project_id}/")
