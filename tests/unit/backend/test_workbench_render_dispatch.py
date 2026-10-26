"""Render dispatch submits once, records the attempt and replays the original key."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from speechnexus_backend.workbench.application.render_units import (
    RenderDispatchUnavailableError,
    RenderSubmissionUnknownError,
    WorkbenchRenderUnits,
)
from speechnexus_backend.workbench.domain.render_units import (
    RenderAttemptConflictError,
    RenderDependencies,
    RenderUnit,
    RevisionPin,
)
from speechnexus_backend.workbench.domain.versions import RevisionConflictError

NOW = datetime(2026, 10, 24, tzinfo=UTC)


def unit() -> RenderUnit:
    dependencies = RenderDependencies(
        original=RevisionPin("original", 1),
        working=RevisionPin("working", 2),
        chapter_id="chapter",
        role_id="narrator",
        text_plan=RevisionPin("plan", 1),
        voice=RevisionPin("voice", 3),
        model="tts-quality",
        model_binding=RevisionPin("binding", 4),
        text="合成旁白单元",
    )
    return RenderUnit.create(
        unit_id="unit", book_id="book", ordinal=0, dependencies=dependencies, now=NOW
    )


def running(reference: str, job_id: str) -> RenderUnit:
    return unit().start(expected_revision=1, job_id=job_id, generation_id=reference, now=NOW)


class _Store:
    def __init__(self, current: RenderUnit | None = None) -> None:
        self.current = current or unit()
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.conflict = False

    async def get_unit(self, *, unit_id: str, workspace_id: str) -> RenderUnit:
        self.calls.append(("get", {"unit_id": unit_id}))
        return self.current

    async def start_unit(self, **kw: Any) -> RenderUnit:
        self.calls.append(("start", dict(kw)))
        if self.conflict:
            raise RenderAttemptConflictError
        return self.current.start(
            expected_revision=kw["expected_revision"],
            job_id=kw["job_id"],
            generation_id=kw["generation_id"],
            now=NOW,
        )


class _Jobs:
    def __init__(self, job_id: str = "job_1") -> None:
        self.job_id = job_id
        self.calls: list[dict[str, Any]] = []
        self.error: Exception | None = None

    async def submit(self, *, unit: RenderUnit, key: str | None, reference: str) -> str:
        self.calls.append({"unit": unit.id, "key": key, "reference": reference})
        if self.error is not None:
            raise self.error
        return self.job_id


async def test_dispatch_submits_then_records_the_same_reference() -> None:
    store, jobs = _Store(), _Jobs()
    started, job_id = await WorkbenchRenderUnits(store, jobs).start_unit(
        unit_id="unit", workspace_id="workspace", expected_revision=1, key="key-12345678"
    )
    assert job_id == "job_1"
    assert [name for name, _ in store.calls] == ["get", "start"]
    assert jobs.calls[0]["key"] == "key-12345678"
    assert store.calls[1][1]["job_id"] == "job_1"
    assert store.calls[1][1]["generation_id"] == jobs.calls[0]["reference"]
    assert started.status == "generating"


async def test_the_same_key_always_names_the_same_generation_identity() -> None:
    first, second = _Jobs(), _Jobs()
    store = _Store()
    await WorkbenchRenderUnits(store, first).start_unit(
        unit_id="unit", workspace_id="workspace", expected_revision=1, key="key-12345678"
    )
    await WorkbenchRenderUnits(store, second).start_unit(
        unit_id="unit", workspace_id="workspace", expected_revision=1, key="key-12345678"
    )
    assert first.calls[0]["reference"] == second.calls[0]["reference"]


async def test_keyless_dispatch_uses_a_fresh_identity() -> None:
    jobs = _Jobs()
    await WorkbenchRenderUnits(_Store(), jobs).start_unit(
        unit_id="unit", workspace_id="workspace", expected_revision=1
    )
    assert jobs.calls[0]["key"] is None
    assert jobs.calls[0]["reference"].startswith("gen:")


async def test_a_changed_unit_revision_never_reaches_the_producer() -> None:
    jobs = _Jobs()
    with pytest.raises(RevisionConflictError):
        await WorkbenchRenderUnits(_Store(), jobs).start_unit(
            unit_id="unit", workspace_id="workspace", expected_revision=2, key="key-12345678"
        )
    assert jobs.calls == []


async def test_without_a_producer_the_attempt_never_starts() -> None:
    store = _Store()
    with pytest.raises(RenderDispatchUnavailableError):
        await WorkbenchRenderUnits(store).start_unit(
            unit_id="unit", workspace_id="workspace", expected_revision=1
        )
    assert store.calls == []


async def test_a_replayed_attempt_returns_the_recorded_generation() -> None:
    store, jobs = _Store(), _Jobs()
    service = WorkbenchRenderUnits(store, jobs)
    await service.start_unit(
        unit_id="unit", workspace_id="workspace", expected_revision=1, key="key-12345678"
    )
    reference = jobs.calls[0]["reference"]
    store.current = running(reference, "job_1")
    store.conflict = True
    replayed, job_id = await service.start_unit(
        unit_id="unit", workspace_id="workspace", expected_revision=1, key="key-12345678"
    )
    assert job_id == "job_1"
    assert replayed.generation is not None
    assert (replayed.generation.id, replayed.generation.job_id) == (reference, "job_1")


async def test_a_conflict_for_a_different_attempt_stays_a_conflict() -> None:
    store, jobs = _Store(), _Jobs()
    store.conflict = True
    with pytest.raises(RenderAttemptConflictError):
        await WorkbenchRenderUnits(store, jobs).start_unit(
            unit_id="unit", workspace_id="workspace", expected_revision=1, key="key-12345678"
        )


async def test_an_unknown_submission_is_not_recorded() -> None:
    store, jobs = _Store(), _Jobs()
    jobs.error = RenderSubmissionUnknownError()
    with pytest.raises(RenderSubmissionUnknownError):
        await WorkbenchRenderUnits(store, jobs).start_unit(
            unit_id="unit", workspace_id="workspace", expected_revision=1, key="key-12345678"
        )
    assert [name for name, _ in store.calls] == ["get"]
