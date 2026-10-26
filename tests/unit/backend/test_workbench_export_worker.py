"""The export worker: fail closed, report progress, publish, and settle honestly."""

from __future__ import annotations

import hashlib
from dataclasses import replace
from datetime import UTC, datetime
from io import BytesIO
from typing import Any

import pytest

from speechnexus_backend.workbench.application.export_worker import (
    ExportCancelledError,
    ExportValidator,
    ExportWorker,
    ProducedExport,
)
from speechnexus_backend.workbench.domain.exports import (
    ExportArtifact,
    ExportError,
    ExportJob,
    ExportStateConflictError,
)
from tests.fake.export_media import export_validator, synthetic_export_zip

NOW = datetime(2026, 10, 24, 12, 0, tzinfo=UTC)
CONTENT = synthetic_export_zip()


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
        from tests.fake.content_writes import FakeContentWrites

        self.content_writes = FakeContentWrites()
        self.queued = queued or []
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.progress: list[int] = []
        self.cancels_at: int | None = None
        self.settle_conflict = False
        self.rows = {item.id: item for item in self.queued}

    async def claim_queued_exports(self, *, limit: int) -> list[ExportJob]:
        self.calls.append(("claim", {"limit": limit}))
        return list(self.queued)

    async def record_export_progress(self, *, export_id: str, completed: int) -> ExportJob:
        self.calls.append(("progress", {"completed": completed}))
        self.progress.append(completed)
        if self.cancels_at is not None and completed >= self.cancels_at:
            raise ExportStateConflictError
        current = self.rows[export_id].record_progress(completed=completed, now=NOW)
        self.rows[export_id] = current
        return current

    async def complete_export(self, *, export_id: str, artifact: ExportArtifact) -> ExportJob:
        self.calls.append(("complete", {"artifact": artifact}))
        current = self.rows[export_id].complete(artifact=artifact, now=NOW)
        self.rows[export_id] = current
        return current

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
    def __init__(
        self,
        *,
        items: int = 2,
        error: Exception | None = None,
        content: bytes = CONTENT,
        media_type: str = "application/zip",
    ) -> None:
        self.items = items
        self.error = error
        self.content = content
        self.media_type = media_type
        self.reported: list[int] = []

    async def produce_export(self, job: ExportJob, *, report: Any) -> ProducedExport:
        for item in range(1, self.items + 1):
            self.reported.append(item)
            await report(item)
        if self.error is not None:
            raise self.error
        return ProducedExport(content=self.content, media_type=self.media_type)


def worker(
    store: _Store,
    zone: _Zone | None,
    producer: _Producer | None,
    validation: ExportValidator | None = None,
) -> ExportWorker:
    return ExportWorker(store, zone, producer, validation or export_validator())


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
    assert store.progress == [1, 2, 2]
    assert [name for name, _ in store.calls] == [
        "claim",
        "progress",
        "progress",
        "progress",
        "complete",
    ]

    artifact = store.calls[-1][1]["artifact"]
    assert artifact.export_id == "export_demo"
    assert artifact.manifest_revision == 1
    assert artifact.media_type == "application/zip"
    assert artifact.bytes == len(CONTENT)
    assert artifact.sha256 == "sha256:" + hashlib.sha256(CONTENT).hexdigest()
    # The published object really holds the produced bytes.
    assert list(zone.objects) == [f"project-synthetic/exports/export_demo/{artifact.id}"]
    assert zone.objects[f"project-synthetic/exports/export_demo/{artifact.id}"] == CONTENT


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


@pytest.mark.parametrize(
    ("content", "media_type"),
    [(b"", "application/zip"), (CONTENT, ""), (CONTENT, "x" * 129)],
    ids=["empty", "missing-type", "oversized-type"],
)
async def test_invalid_produced_metadata_fails_before_writing_any_object(
    content: bytes, media_type: str
) -> None:
    store = _Store(queued=[job()])
    zone = _Zone()
    producer = _Producer(content=content, media_type=media_type)

    assert await worker(store, zone, producer).run_once() == 1

    assert zone.objects == {}
    assert [name for name, _ in store.calls] == ["claim", "progress", "progress", "fail"]
    error = store.calls[-1][1]["error"]
    assert (error.code, error.retryable) == ("unavailable", True)


async def test_an_object_write_failure_fails_only_its_export_and_continues_the_claimed_batch() -> (
    None
):
    class FailFirstWrite(_Zone):
        def __init__(self) -> None:
            super().__init__()
            self.attempts = 0

        async def put(self, *, key: str, body: BytesIO, length: int) -> None:
            self.attempts += 1
            if self.attempts == 1:
                raise OSError("private object endpoint and synthetic content must not escape")
            await super().put(key=key, body=body, length=length)

    store = _Store(queued=[job(), replace(job(), id="export_second")])
    zone = FailFirstWrite()

    assert await worker(store, zone, _Producer()).run_once() == 2

    assert [name for name, _ in store.calls] == [
        "claim",
        "progress",
        "progress",
        "progress",
        "fail",
        "progress",
        "progress",
        "progress",
        "complete",
    ]
    error = next(value["error"] for name, value in store.calls if name == "fail")
    assert (error.code, error.retryable) == ("unavailable", True)
    assert "private" not in error.message and "synthetic content" not in error.message
    artifact = store.calls[-1][1]["artifact"]
    assert artifact.export_id == "export_second"
    assert list(zone.objects) == [f"project-synthetic/exports/export_second/{artifact.id}"]


async def test_an_unknown_completion_does_not_overwrite_the_original_outcome_with_failure() -> None:
    class UnknownCompletion(_Store):
        async def complete_export(self, *, export_id: str, artifact: ExportArtifact) -> ExportJob:
            await super().complete_export(export_id=export_id, artifact=artifact)
            # The database may have committed the completion before losing its reply.
            raise OSError("completion result unknown")

    store = UnknownCompletion(queued=[job()])
    zone = _Zone()
    with pytest.raises(OSError, match="completion result unknown"):
        await worker(store, zone, _Producer()).run_once()
    assert [name for name, _ in store.calls] == [
        "claim",
        "progress",
        "progress",
        "progress",
        "complete",
    ]
    assert len(zone.objects) == 1


async def test_cancellation_after_confirmed_put_is_settled_without_compensating_failure() -> None:
    class CancellingStore(_Store):
        async def complete_export(self, *, export_id: str, artifact: ExportArtifact) -> ExportJob:
            self.calls.append(("complete", {"artifact": artifact}))
            raise ExportStateConflictError

    store = CancellingStore(queued=[job()])
    zone = _Zone()
    assert await worker(store, zone, _Producer()).run_once() == 1
    assert len(zone.objects) == 1
    assert [name for name, _ in store.calls][-2:] == ["complete", "settle_cancel"]


@pytest.mark.parametrize("items", [0, 1])
async def test_incomplete_production_never_reserves_or_publishes_a_result(items: int) -> None:
    store = _Store(queued=[job()])
    zone = _Zone()
    assert await worker(store, zone, _Producer(items=items)).run_once() == 1
    assert zone.objects == {}
    assert not any(name == "complete" for name, _ in store.calls)
    assert store.content_writes.states == {}
    assert store.calls[-1][0] == "fail"


@pytest.mark.parametrize("field", ["id", "manifest_revision", "format", "progress_total"])
async def test_progress_for_another_export_cannot_authorize_publication(field: str) -> None:
    class WrongProgress(_Store):
        async def record_export_progress(self, *, export_id: str, completed: int) -> ExportJob:
            await super().record_export_progress(export_id=export_id, completed=completed)
            values: dict[str, Any] = {
                "id": "export_wrong",
                "manifest_revision": 2,
                "format": "wav",
                "progress_total": 3,
            }
            return replace(job(), progress_completed=completed, **{field: values[field]})

    store = WrongProgress(queued=[job()])
    zone = _Zone()
    assert await worker(store, zone, _Producer()).run_once() == 1
    assert zone.objects == {}
    assert store.content_writes.states == {}
    assert store.calls[-1][0] == "fail"


async def test_unacknowledged_progress_does_not_become_a_completed_export() -> None:
    class OldProgress(_Store):
        async def record_export_progress(self, *, export_id: str, completed: int) -> ExportJob:
            await super().record_export_progress(export_id=export_id, completed=completed)
            return job()

    store = OldProgress(queued=[job()])
    zone = _Zone()
    assert await worker(store, zone, _Producer()).run_once() == 1
    assert zone.objects == {}
    assert store.calls[-1][0] == "fail"


async def test_a_worker_without_validation_cannot_claim_jobs() -> None:
    store = _Store(queued=[job()])
    instance = ExportWorker(store, _Zone(), _Producer())
    assert not instance.enabled and await instance.run_once() == 0
    assert store.calls == []


async def test_validation_failure_leaves_no_content_write_intent_or_object() -> None:
    class InvalidMedia:
        async def validate_export(self, job: ExportJob, produced: ProducedExport) -> None:
            assert store.progress == [1, 2]
            assert store.content_writes.states == {} and zone.objects == {}
            raise ValueError("synthetic private media and endpoint must not escape")

    store = _Store(queued=[job()])
    zone = _Zone()
    assert await worker(store, zone, _Producer(), InvalidMedia()).run_once() == 1
    assert store.content_writes.states == {} and zone.objects == {}
    error = store.calls[-1][1]["error"]
    assert error.code == "unavailable" and "private" not in error.message


async def test_cancellation_during_validation_is_rechecked_before_reserving_a_write() -> None:
    class CancelDuringValidation:
        async def validate_export(self, job: ExportJob, produced: ProducedExport) -> None:
            store.cancels_at = 2

    store = _Store(queued=[job()])
    zone = _Zone()
    assert await worker(store, zone, _Producer(), CancelDuringValidation()).run_once() == 1
    assert store.content_writes.states == {} and zone.objects == {}
    assert store.calls[-1][0] == "settle_cancel"


async def test_a_late_producer_report_cannot_update_the_next_claimed_export() -> None:
    class LateProducer(_Producer):
        previous_report: Any = None
        rejected = False

        async def produce_export(self, job: ExportJob, *, report: Any) -> ProducedExport:
            if self.previous_report is not None:
                try:
                    await self.previous_report(2)
                except ValueError:
                    self.rejected = True
            self.previous_report = report
            return await super().produce_export(job, report=report)

    store = _Store(queued=[job(), replace(job(), id="export_second")])
    zone = _Zone()
    producer = LateProducer()
    assert await worker(store, zone, producer).run_once() == 2
    assert producer.rejected and len(zone.objects) == 2
    assert store.rows["export_demo"].status == store.rows["export_second"].status == "succeeded"


@pytest.mark.parametrize("error", [ValueError("invalid media"), TimeoutError("decoder deadline")])
async def test_a_validation_error_after_cancel_settles_and_continues_the_batch(
    error: Exception,
) -> None:
    class RealTransitions(_Store):
        async def fail_export(self, *, export_id: str, error: ExportError) -> ExportJob:
            self.calls.append(("fail", {"error": error}))
            current = self.rows[export_id].fail(error=error, now=NOW)
            self.rows[export_id] = current
            return current

        async def settle_cancel_export(self, *, export_id: str) -> ExportJob:
            self.calls.append(("settle_cancel", {"export_id": export_id}))
            current = self.rows[export_id].settle_cancel(now=NOW)
            self.rows[export_id] = current
            return current

    class CancelThenReject:
        async def validate_export(self, current: ExportJob, produced: ProducedExport) -> None:
            if current.id == "export_demo":
                store.rows[current.id] = store.rows[current.id].request_cancel(now=NOW)
                raise error
            await export_validator().validate_export(current, produced)

    store = RealTransitions(queued=[job(), replace(job(), id="export_second")])
    zone = _Zone()
    assert await worker(store, zone, _Producer(), CancelThenReject()).run_once() == 2
    assert store.rows["export_demo"].status == "cancelled"
    assert store.rows["export_demo"].artifact is None
    assert store.rows["export_second"].status == "succeeded"
    assert len(zone.objects) == 1
    assert all("/export_second/" in key for key in zone.objects)


async def test_an_unknown_failure_reply_does_not_trigger_cancellation_compensation() -> None:
    class UnknownFailure(_Store):
        async def fail_export(self, *, export_id: str, error: ExportError) -> ExportJob:
            self.rows[export_id] = self.rows[export_id].fail(error=error, now=NOW)
            raise OSError("failure committed but reply lost")

    store = UnknownFailure(queued=[job()])
    with pytest.raises(OSError, match="reply lost"):
        await worker(store, _Zone(), _Producer(items=0)).run_once()
    assert store.rows["export_demo"].status == "failed"
    assert not any(name == "settle_cancel" for name, _ in store.calls)
