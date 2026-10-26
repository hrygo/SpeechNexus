"""Settlement applies only terminal, readable Job outcomes and never invents a result."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast

import pytest

from speechnexus_backend.workbench.application.render_reconcile import (
    RenderJobOutcome,
    RenderJobReadError,
    RenderJobState,
    RenderReconciler,
)
from speechnexus_backend.workbench.domain.render_units import (
    RenderAttemptConflictError,
    RenderDependencies,
    RenderSource,
    RenderUnit,
    RevisionPin,
)
from speechnexus_backend.workbench.domain.versions import InvalidVersionChangeError

NOW = datetime(2026, 10, 24, tzinfo=UTC)
WORKSPACE = "workspace-demo"


class ObjectZoneError(Exception):
    """Stand-in for a storage adapter that could not accept the copy."""


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
    created = RenderUnit.create(
        unit_id="unit", book_id="book", ordinal=0, dependencies=dependencies, now=NOW
    )
    return created.start(expected_revision=1, generation_id="gen:1", job_id="job_1", now=NOW)


class Units:
    def __init__(self, current: RenderUnit, *, conflict: bool = False) -> None:
        self.current = current
        self.conflict = conflict
        self.completed: list[dict[str, Any]] = []
        self.failed: list[dict[str, Any]] = []
        self.unsaved: tuple[RenderUnit, ...] = ()
        self.unsaved_limit: int | None = None

    async def generating_units(self, *, workspace_id: str, limit: int) -> tuple[RenderUnit, ...]:
        return (self.current,)

    async def unsaved_units(self, *, workspace_id: str, limit: int) -> tuple[RenderUnit, ...]:
        self.unsaved_limit = limit
        return self.unsaved

    async def complete_unit(self, **arguments: Any) -> RenderUnit:
        if self.conflict:
            raise RenderAttemptConflictError
        self.completed.append(arguments)
        return self.current

    async def fail_unit(self, **arguments: Any) -> RenderUnit:
        if self.conflict:
            raise RenderAttemptConflictError
        self.failed.append(arguments)
        return self.current


class Jobs:
    def __init__(self, outcome: RenderJobOutcome | Exception) -> None:
        self.outcome = outcome
        self.read: list[str] = []

    async def read_job(self, *, job_id: str) -> RenderJobOutcome:
        self.read.append(job_id)
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


async def test_succeeded_job_completes_the_attempt_with_its_receipt() -> None:
    units, jobs = Units(unit()), Jobs(RenderJobOutcome("succeeded", "artifact_1", "receipt_1"))
    settled = await RenderReconciler(units, jobs).settle(workspace_id=WORKSPACE)
    assert [(item.unit_id, item.state) for item in settled] == [("unit", "succeeded")]
    assert jobs.read == ["job_1"]
    (completed,) = units.completed
    assert completed["unit_id"] == "unit"
    assert completed["workspace_id"] == WORKSPACE
    assert completed["generation_id"] == "gen:1"
    source = completed["source"]
    assert (source.unit_id, source.job_id) == ("unit", "job_1")
    assert (source.artifact_id, source.receipt_id) == ("artifact_1", "receipt_1")
    assert source.dependencies_sha256 == units.current.dependencies.sha256
    assert units.failed == []


@pytest.mark.parametrize("state", ["failed", "expired", "cancelled"])
async def test_terminal_failures_fail_the_attempt(state: str) -> None:
    units, jobs = Units(unit()), Jobs(RenderJobOutcome(cast(RenderJobState, state)))
    settled = await RenderReconciler(units, jobs).settle(workspace_id=WORKSPACE)
    assert [item.state for item in settled] == [state]
    assert units.completed == []
    assert units.failed == [
        {"unit_id": "unit", "workspace_id": WORKSPACE, "generation_id": "gen:1"}
    ]


async def test_pending_job_and_unreadable_receipt_leave_the_unit_generating() -> None:
    unreadable = (RenderJobOutcome("pending"), RenderJobReadError(), RenderJobOutcome("succeeded"))
    for outcome in unreadable:
        units, jobs = Units(unit()), Jobs(outcome)
        assert await RenderReconciler(units, jobs).settle(workspace_id=WORKSPACE) == ()
        assert units.completed == [] and units.failed == []


async def test_a_settled_attempt_is_not_settled_again() -> None:
    units, jobs = Units(unit(), conflict=True), Jobs(RenderJobOutcome("failed"))
    assert await RenderReconciler(units, jobs).settle(workspace_id=WORKSPACE) == ()


class Copies:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.saved: list[dict[str, Any]] = []
        self.attempts: list[dict[str, Any]] = []

    async def save_copy(self, **arguments: Any) -> str:
        self.attempts.append(arguments)
        if self.error is not None:
            raise self.error
        self.saved.append(arguments)
        return "asset_1"


async def test_succeeded_settlement_saves_the_receipt_pinned_copy() -> None:
    units = Units(unit())
    copies = Copies()
    reconciler = RenderReconciler(
        units, Jobs(RenderJobOutcome("succeeded", "artifact_1", "receipt_1")), copies
    )
    assert [item.state for item in await reconciler.settle(workspace_id=WORKSPACE)] == ["succeeded"]
    (saved,) = copies.saved
    assert saved["workspace_id"] == WORKSPACE
    assert saved["unit"].id == "unit"
    assert (saved["source"].artifact_id, saved["source"].receipt_id) == (
        "artifact_1",
        "receipt_1",
    )


async def test_copy_failure_does_not_unsettle_the_ready_unit() -> None:
    units, copies = Units(unit()), Copies(ObjectZoneError())
    settled = await RenderReconciler(
        units, Jobs(RenderJobOutcome("succeeded", "artifact_1", "receipt_1")), copies
    ).settle(workspace_id=WORKSPACE)
    assert [item.state for item in settled] == ["succeeded"]
    assert units.completed != []


def ready_with_source() -> RenderUnit:
    started = unit()
    return started.complete(
        generation_id="gen:1",
        source=RenderSource(
            "unit", "job_1", "artifact_1", "receipt_1", started.dependencies.sha256
        ),
        now=NOW,
    )


async def test_restore_copies_retries_ready_units_that_lost_their_copy() -> None:
    ready = ready_with_source()
    units, copies = Units(ready), Copies()
    units.unsaved = (ready,)
    restored = await RenderReconciler(
        units, Jobs(RenderJobOutcome("pending")), copies
    ).restore_copies(workspace_id=WORKSPACE)
    assert restored == ("unit",)
    assert [saved["unit"].id for saved in copies.saved] == ["unit"]


async def test_restore_copies_keeps_failed_copies_for_the_next_pass() -> None:
    ready = ready_with_source()
    units, copies = Units(ready), Copies(ObjectZoneError())
    units.unsaved = (ready,)
    assert (
        await RenderReconciler(units, Jobs(RenderJobOutcome("pending")), copies).restore_copies(
            workspace_id=WORKSPACE
        )
        == ()
    )


async def test_restore_copies_without_a_port_is_a_no_op() -> None:
    ready = ready_with_source()
    units = Units(ready)
    units.unsaved = (ready,)
    assert (
        await RenderReconciler(units, Jobs(RenderJobOutcome("pending"))).restore_copies(
            workspace_id=WORKSPACE
        )
        == ()
    )


async def test_a_rejected_copy_is_not_retried_forever() -> None:
    ready = ready_with_source()
    units, copies = Units(ready), Copies(InvalidVersionChangeError("source is gone"))
    units.unsaved = (ready,)
    reconciler = RenderReconciler(units, Jobs(RenderJobOutcome("pending")), copies)
    assert await reconciler.restore_copies(workspace_id=WORKSPACE) == ()
    assert await reconciler.restore_copies(workspace_id=WORKSPACE) == ()
    assert len(copies.attempts) == 1


async def test_a_transient_copy_failure_is_retried_on_the_next_pass() -> None:
    ready = ready_with_source()
    units, copies = Units(ready), Copies(ObjectZoneError())
    units.unsaved = (ready,)
    reconciler = RenderReconciler(units, Jobs(RenderJobOutcome("pending")), copies)
    await reconciler.restore_copies(workspace_id=WORKSPACE)
    await reconciler.restore_copies(workspace_id=WORKSPACE)
    assert len(copies.attempts) == 2


async def test_restore_copies_forwards_its_bounded_limit() -> None:
    ready = ready_with_source()
    units, copies = Units(ready), Copies()
    units.unsaved = (ready,)
    await RenderReconciler(units, Jobs(RenderJobOutcome("pending")), copies).restore_copies(
        workspace_id=WORKSPACE, limit=3
    )
    assert units.unsaved_limit == 3
