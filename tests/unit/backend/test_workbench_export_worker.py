"""The export worker: fail closed, report progress, publish, and settle honestly."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from io import BytesIO
from typing import Any

from speechnexus_backend.workbench.application.export_worker import (
    ExportCancelledError,
    ExportWorker,
    ProducedExport,
)
from speechnexus_backend.workbench.domain.exports import (
    ExportArtifact,
    ExportError,
    ExportJob,
    ExportStateConflictError,
)

NOW = datetime(2026, 10, 24, 12, 0, tzinfo=UTC)
CONTENT = b"synthetic export bundle\n" * 4


def job() -> ExportJob:
    return ExportJob.create(
        export_id="export_demo",
        book_id="book_demo",
        export_format="zip",
        manifest_revision=1,
        progress_total=2,
        now=NOW,
    ).start(now=NOW)


class _Store:
    def __init__(self, queued: list[ExportJob] | None = None) -> None:
        self.queued = queued or []
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.progress: list[int] = []
        self.cancels_at: int | None = None
        self.settle_conflict = False

    async def claim_queued_exports(self, *, limit: int) -> list[ExportJob]:
        self.calls.append(("claim", {"limit": limit}))
        return list(self.queued)

    async def record_export_progress(self, *, export_id: str, completed: int) -> ExportJob:
        self.calls.append(("progress", {"completed": completed}))
        self.progress.append(completed)
        if self.cancels_at is not None and completed >= self.cancels_at:
            raise ExportStateConflictError
        return job()

    async def complete_export(self, *, export_id: str, artifact: ExportArtifact) -> ExportJob:
        self.calls.append(("complete", {"artifact": artifact}))
        return job()

    async def fail_export(self, *, export_id: str, error: ExportError) -> ExportJob:
        self.calls.append(("fail", {"error": error}))
        return job()

    async def settle_cancel_export(self, *, export_id: str) -> ExportJob:
        self.calls.append(("settle_cancel", {"export_id": export_id}))
        if self.settle_conflict:
            raise ExportStateConflictError
        return job()


class _Zone:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    async def put(self, *, key: str, body: BytesIO, length: int) -> None:
        self.objects[key] = body.read(length)


class _Producer:
    def __init__(self, *, items: int = 2, error: Exception | None = None) -> None:
        self.items = items
        self.error = error
        self.reported: list[int] = []

    async def produce_export(self, job: ExportJob, *, report: Any) -> ProducedExport:
        for item in range(1, self.items + 1):
            self.reported.append(item)
            await report(item)
        if self.error is not None:
            raise self.error
        return ProducedExport(content=CONTENT, media_type="application/zip")


def worker(store: _Store, zone: _Zone | None, producer: _Producer | None) -> ExportWorker:
    return ExportWorker(store, zone, producer)


async def test_a_worker_without_a_producer_never_touches_the_store() -> None:
    store = _Store(queued=[job()])
    for missing in ((None, None), (_Zone(), None), (None, _Producer())):
        instance = worker(store, missing[0], missing[1])
        assert instance.enabled is False
        assert await instance.run_once() == 0
    assert store.calls == []


async def test_a_claimed_export_is_produced_published_and_completed() -> None:
    store = _Store(queued=[job()])
    zone = _Zone()
    producer = _Producer()
    assert await worker(store, zone, producer).run_once(limit=3) == 1

    assert store.calls[0] == ("claim", {"limit": 3})
    assert producer.reported == [1, 2]
    assert store.progress == [1, 2]
    assert [name for name, _ in store.calls] == ["claim", "progress", "progress", "complete"]

    artifact = store.calls[-1][1]["artifact"]
    assert artifact.export_id == "export_demo"
    assert artifact.manifest_revision == 1
    assert artifact.media_type == "application/zip"
    assert artifact.bytes == len(CONTENT)
    assert artifact.sha256 == "sha256:" + hashlib.sha256(CONTENT).hexdigest()
    # The published object really holds the produced bytes.
    assert list(zone.objects) == [f"book_demo/export_demo/{artifact.id}"]
    assert zone.objects[f"book_demo/export_demo/{artifact.id}"] == CONTENT


async def test_cancellation_during_production_settles_instead_of_completing() -> None:
    store = _Store(queued=[job()])
    store.cancels_at = 2
    producer = _Producer()
    assert await worker(store, _Zone(), producer).run_once() == 1

    assert [name for name, _ in store.calls] == ["claim", "progress", "progress", "settle_cancel"]
    assert producer.reported == [1, 2]


async def test_a_producer_failure_records_a_stable_provider_free_error() -> None:
    store = _Store(queued=[job()])
    producer = _Producer(error=RuntimeError("provider said: secret endpoint http://internal"))
    assert await worker(store, _Zone(), producer).run_once() == 1

    assert [name for name, _ in store.calls] == ["claim", "progress", "progress", "fail"]
    error = store.calls[-1][1]["error"]
    assert (error.code, error.retryable) == ("unavailable", True)
    assert "secret endpoint" not in error.message
    assert "internal" not in error.message


async def test_a_producer_stops_for_a_cancellation_that_really_exists() -> None:
    store = _Store(queued=[job()])
    producer = _Producer(error=ExportCancelledError())
    assert await worker(store, _Zone(), producer).run_once() == 1
    assert [name for name, _ in store.calls][-1] == "settle_cancel"


async def test_an_abort_without_a_cancellation_request_is_recorded_as_failed() -> None:
    store = _Store(queued=[job()])
    store.settle_conflict = True
    producer = _Producer(error=ExportCancelledError())
    assert await worker(store, _Zone(), producer).run_once() == 1
    assert [name for name, _ in store.calls][-2:] == ["settle_cancel", "fail"]
    error = store.calls[-1][1]["error"]
    assert (error.code, error.message) == ("aborted", "export production was aborted")


async def test_an_empty_queue_claims_nothing() -> None:
    store = _Store()
    assert await worker(store, _Zone(), _Producer()).run_once() == 0
    assert [name for name, _ in store.calls] == ["claim"]
