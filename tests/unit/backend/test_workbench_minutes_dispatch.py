"""The serving loop that dispatches queued Minutes work to the configured model."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from typing import cast

from speechnexus_backend.workbench.application.minutes_tasks import MinutesTasks
from speechnexus_backend.workbench.runtime import _dispatch_minutes, _dispatch_minutes_once

INTERVAL = 0.01


class Queue:
    """Minimal queue double: bounded reads, per-task outcomes, recorded calls."""

    def __init__(
        self,
        pages: list[tuple[str, ...]],
        *,
        failed_reads: int = 0,
        failing: frozenset[str] = frozenset(),
    ) -> None:
        self._pages = pages
        self._failed_reads = failed_reads
        self._failing = failing
        self.reads: list[tuple[str, int]] = []
        self.executed: list[str] = []
        self.read = asyncio.Event()
        self.dispatched = asyncio.Event()

    async def queued_ids(self, workspace_id: str, *, limit: int) -> tuple[str, ...]:
        self.reads.append((workspace_id, limit))
        self.read.set()
        if self._failed_reads > 0:
            self._failed_reads -= 1
            raise RuntimeError("synthetic queue outage")
        index = min(len(self.reads) - 1, len(self._pages) - 1)
        return self._pages[index]

    async def execute(self, task_id: str) -> None:
        if task_id in self._failing:
            raise RuntimeError("synthetic task outage")
        self.executed.append(task_id)
        self.dispatched.set()


def service(queue: Queue) -> MinutesTasks:
    return cast(MinutesTasks, queue)


async def test_one_pass_dispatches_each_queued_task_in_a_bounded_page() -> None:
    queue = Queue([("task-a", "task-b")])
    await _dispatch_minutes_once(service(queue), "workspace-synthetic")
    assert queue.executed == ["task-a", "task-b"]
    assert queue.reads == [("workspace-synthetic", 8)]


async def test_one_failing_task_does_not_stop_the_page() -> None:
    queue = Queue([("task-a", "task-b")], failing=frozenset({"task-a"}))
    await _dispatch_minutes_once(service(queue), "workspace-synthetic")
    assert queue.executed == ["task-b"]


async def test_a_failed_read_dispatches_nothing_and_the_next_pass_retries() -> None:
    queue = Queue([("task-a",)], failed_reads=1)
    await _dispatch_minutes_once(service(queue), "workspace-synthetic")
    assert queue.executed == []
    await _dispatch_minutes_once(service(queue), "workspace-synthetic")
    assert queue.executed == ["task-a"]


async def test_the_loop_runs_a_pass_after_the_interval_and_stops_on_cancellation() -> None:
    queue = Queue([("task-a",)])
    loop = asyncio.create_task(_dispatch_minutes(service(queue), "workspace-synthetic", INTERVAL))
    try:
        async with asyncio.timeout(2):
            await queue.dispatched.wait()
    finally:
        loop.cancel()
        with suppress(asyncio.CancelledError):
            await loop
    assert loop.cancelled() and queue.executed == ["task-a"]
