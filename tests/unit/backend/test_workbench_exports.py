"""Export job state machine and request validation."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest

from speechnexus_backend.workbench.application.exports import WorkbenchExports
from speechnexus_backend.workbench.domain.exports import (
    ExportArtifact,
    ExportError,
    ExportJob,
    ExportStateConflictError,
)
from speechnexus_backend.workbench.domain.versions import InvalidVersionChangeError

NOW = datetime(2026, 10, 24, 12, 0, tzinfo=UTC)
DIGEST = "sha256:" + "ab" * 32
WORKSPACE = "workspace-exports"


def artifact(**overrides: Any) -> ExportArtifact:
    fields: dict[str, Any] = {
        "id": "artifact_demo",
        "export_id": "export_demo",
        "manifest_revision": 3,
        "media_type": "application/zip",
        "bytes": 1024,
        "sha256": DIGEST,
        "created_at": NOW + timedelta(seconds=5),
    }
    fields.update(overrides)
    return ExportArtifact(**fields)


def job(**overrides: Any) -> ExportJob:
    fields: dict[str, Any] = {
        "export_id": "export_demo",
        "book_id": "book_demo",
        "export_format": "zip",
        "manifest_revision": 3,
        "progress_total": 4,
        "now": NOW,
    }
    fields.update(overrides)
    return ExportJob.create(**fields)


def test_create_starts_queued_with_no_progress_or_result() -> None:
    created = job()
    assert (created.status, created.progress_completed, created.progress_total) == ("queued", 0, 4)
    assert (created.manifest_revision, created.artifact, created.error) == (3, None, None)
    assert created.in_flight and not created.finished


def test_progress_only_moves_forward_inside_a_running_export() -> None:
    running = job().start(now=NOW + timedelta(1))
    assert running.status == "running"

    advanced = running.record_progress(completed=2, now=NOW + timedelta(2))
    assert advanced.progress_completed == 2
    with pytest.raises(ValueError, match="must not move backwards"):
        advanced.record_progress(completed=1, now=NOW + timedelta(3))
    with pytest.raises(ValueError, match="past its total"):
        advanced.record_progress(completed=5, now=NOW + timedelta(3))
    with pytest.raises(ExportStateConflictError):
        job().record_progress(completed=1, now=NOW + timedelta(1))
    with pytest.raises(ExportStateConflictError):
        running.start(now=NOW + timedelta(2))


def test_completion_requires_an_artifact_pinned_to_this_export() -> None:
    running = job().start(now=NOW + timedelta(1))
    running = running.record_progress(completed=running.progress_total, now=running.updated_at)
    done = running.complete(artifact=artifact(), now=NOW + timedelta(6))
    # Producing every requested item is what completion means.
    assert (done.status, done.progress_completed, done.artifact) == ("succeeded", 4, artifact())
    with pytest.raises(ExportStateConflictError):
        job().complete(artifact=artifact(), now=NOW + timedelta(1))
    with pytest.raises(ValueError, match="belong to its own export"):
        running.complete(artifact=artifact(export_id="export_other"), now=NOW + timedelta(6))
    with pytest.raises(ValueError, match="pin its export manifest revision"):
        running.complete(artifact=artifact(manifest_revision=9), now=NOW + timedelta(6))


def test_failure_and_expiry_own_their_paired_fields() -> None:
    failure = ExportError("unavailable", "合成导出失败", True)
    failed = job().fail(error=failure, now=NOW + timedelta(1))
    assert (failed.status, failed.error, failed.artifact) == ("failed", failure, None)
    assert failed.finished and not failed.in_flight
    with pytest.raises(ExportStateConflictError):
        failed.fail(error=failure, now=NOW + timedelta(2))

    running = job().start(now=NOW + timedelta(1))
    done = running.record_progress(completed=4, now=running.updated_at).complete(
        artifact=artifact(), now=NOW + timedelta(2)
    )
    expired = done.expire(now=NOW + timedelta(3))
    # Retention drops the artifact reference; the manifest stays as evidence.
    assert (expired.status, expired.artifact, expired.manifest_revision) == ("expired", None, 3)
    with pytest.raises(ExportStateConflictError):
        failed.expire(now=NOW + timedelta(2))


def test_cancellation_runs_through_its_own_state() -> None:
    cancelling = job().start(now=NOW + timedelta(1)).request_cancel(now=NOW + timedelta(2))
    assert cancelling.status == "cancelling"
    settled = cancelling.settle_cancel(now=NOW + timedelta(3))
    assert (settled.status, settled.finished) == ("cancelled", True)

    assert job().request_cancel(now=NOW + timedelta(1)).status == "cancelling"
    with pytest.raises(ExportStateConflictError):
        settled.request_cancel(now=NOW + timedelta(4))
    with pytest.raises(ExportStateConflictError):
        job().settle_cancel(now=NOW + timedelta(1))


@pytest.mark.parametrize("completed", [0, 1, 3])
def test_completing_cannot_invent_unreported_progress(completed: int) -> None:
    running = job().start(now=NOW).record_progress(completed=completed, now=NOW)
    with pytest.raises(ValueError, match="progress"):
        running.complete(artifact=artifact(), now=NOW + timedelta(seconds=6))
    assert running.progress_completed == completed and running.artifact is None


def test_a_successful_snapshot_requires_complete_progress() -> None:
    from dataclasses import replace

    running = job().start(now=NOW)
    with pytest.raises(ValueError, match="progress"):
        replace(running, status="succeeded", artifact=artifact())


@pytest.mark.parametrize("media_type", ["audio/wav", "audio/mpeg", "application/octet-stream"])
def test_an_artifact_cannot_change_the_accepted_export_format(media_type: str) -> None:
    running = job().start(now=NOW).record_progress(completed=4, now=NOW)
    with pytest.raises(ValueError, match="media type"):
        running.complete(artifact=artifact(media_type=media_type), now=NOW + timedelta(seconds=6))


def test_invalid_field_values_are_refused_at_construction() -> None:
    with pytest.raises(ValueError, match="artifact digest"):
        artifact(sha256="sha256:short")
    with pytest.raises(ValueError, match="artifact bytes"):
        artifact(bytes=0)
    with pytest.raises(ValueError, match="artifact media type"):
        artifact(media_type="")
    with pytest.raises(ValueError, match="export error code"):
        ExportError("Bad", "合成失败", True)
    with pytest.raises(ValueError, match="invalid export format"):
        job(export_format="flac")
    with pytest.raises(ValueError, match="export progress total"):
        job(progress_total=0)
    with pytest.raises(ValueError, match="progress exceeds its total"):
        ExportJob(
            id="export_demo",
            book_id="book_demo",
            status="queued",
            format="zip",
            manifest_revision=3,
            progress_completed=5,
            progress_total=4,
            created_at=NOW,
            updated_at=NOW,
        )
    with pytest.raises(ValueError, match="updated before"):
        ExportJob(
            id="export_demo",
            book_id="book_demo",
            status="queued",
            format="zip",
            manifest_revision=3,
            progress_completed=0,
            progress_total=4,
            created_at=NOW,
            updated_at=NOW - timedelta(1),
        )
    with pytest.raises(ValueError, match="time moved backwards"):
        job().start(now=NOW - timedelta(1))


class _Store:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def create_export(self, **kwargs: Any) -> ExportJob:
        self.calls.append(("create", kwargs))
        return job(progress_total=len(kwargs["unit_ids"] or [0, 0]))

    async def get_export(self, **kwargs: Any) -> ExportJob:
        self.calls.append(("get", kwargs))
        return job()

    async def cancel_export(self, **kwargs: Any) -> tuple[ExportJob, bool]:
        self.calls.append(("cancel", kwargs))
        return job(), False

    async def start_export(self, **kwargs: Any) -> ExportJob:
        self.calls.append(("start", kwargs))
        return job()

    async def record_export_progress(self, **kwargs: Any) -> ExportJob:
        self.calls.append(("progress", kwargs))
        return job()

    async def complete_export(self, **kwargs: Any) -> ExportJob:
        self.calls.append(("complete", kwargs))
        return job()

    async def fail_export(self, **kwargs: Any) -> ExportJob:
        self.calls.append(("fail", kwargs))
        return job()

    async def expire_export(self, **kwargs: Any) -> ExportJob:
        self.calls.append(("expire", kwargs))
        return job()

    async def claim_queued_exports(self, **kwargs: Any) -> list[ExportJob]:
        self.calls.append(("claim", kwargs))
        return []

    async def settle_cancel_export(self, **kwargs: Any) -> ExportJob:
        self.calls.append(("settle_cancel", kwargs))
        return job()


async def test_requests_are_validated_before_the_store_is_touched() -> None:
    store = _Store()
    service = WorkbenchExports(store)
    for bad in (
        {"export_format": "flac", "expected_casting_revision": 1},
        {"export_format": "zip", "expected_casting_revision": 0},
        {"export_format": "zip", "expected_casting_revision": 1, "unit_ids": []},
        {"export_format": "zip", "expected_casting_revision": 1, "unit_ids": ["unit_a", "unit_a"]},
        {"export_format": "zip", "expected_casting_revision": 1, "unit_ids": ["BAD UNIT"]},
        {"export_format": "zip", "expected_casting_revision": 1, "key": "short"},
    ):
        with pytest.raises(InvalidVersionChangeError):
            await service.create_export(
                workspace_id=WORKSPACE, book_id="book_demo", **cast(Any, bad)
            )
    assert store.calls == []


async def test_valid_requests_reach_the_store_with_a_frozen_selection() -> None:
    store = _Store()
    service = WorkbenchExports(store)
    created = await service.create_export(
        workspace_id=WORKSPACE,
        book_id="book_demo",
        export_format="zip",
        expected_casting_revision=1,
        unit_ids=["unit_a"],
        key="export-request-key-0001",
    )
    assert created.progress_total == 1
    assert store.calls[0][1]["unit_ids"] == ["unit_a"]
    assert (await service.cancel_export(export_id="export_demo", workspace_id=WORKSPACE))[
        1
    ] is False
    assert await service.get_export(export_id="export_demo", workspace_id=WORKSPACE) == job()
    assert [name for name, _ in store.calls] == ["create", "cancel", "get"]


async def test_worker_transitions_are_validated_then_delegated() -> None:
    store = _Store()
    service = WorkbenchExports(store)
    with pytest.raises(InvalidVersionChangeError):
        await service.record_export_progress(export_id="export_demo", completed=-1)
    with pytest.raises(InvalidVersionChangeError):
        await service.complete_export(export_id="export_demo", artifact="not an artifact")  # type: ignore[arg-type]
    with pytest.raises(InvalidVersionChangeError):
        await service.fail_export(export_id="export_demo", error="not an error")  # type: ignore[arg-type]
    assert store.calls == []

    await service.start_export(export_id="export_demo")
    await service.record_export_progress(export_id="export_demo", completed=1)
    await service.complete_export(export_id="export_demo", artifact=artifact())
    await service.fail_export(
        export_id="export_demo", error=ExportError("unavailable", "合成导出失败", True)
    )
    await service.expire_export(export_id="export_demo")
    assert [name for name, _ in store.calls] == [
        "start",
        "progress",
        "complete",
        "fail",
        "expire",
    ]
    assert store.calls[1][1]["completed"] == 1
