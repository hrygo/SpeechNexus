"""Background failure diagnostics must not disclose adapter exception content."""

import asyncio
from typing import NoReturn, cast

import pytest

from speechnexus_backend.workbench.application.minutes_tasks import MinutesTasks
from speechnexus_backend.workbench.application.recovery import WorkbenchRecovery
from speechnexus_backend.workbench.runtime import _dispatch_minutes_once, _sweep_deletions

PRIVATE_DETAIL = "synthetic private transcript at http://private-service.invalid/content"
PRIVATE_CAUSE = "synthetic private database statement and object key"


def private_failure() -> NoReturn:
    try:
        raise ValueError(PRIVATE_CAUSE)
    except ValueError as cause:
        raise RuntimeError(PRIVATE_DETAIL) from cause


def assert_bounded_diagnostic(caplog: pytest.LogCaptureFixture, message: str) -> None:
    assert len(caplog.records) == 1
    assert caplog.records[0].levelname == "ERROR"
    assert message in caplog.text
    assert PRIVATE_DETAIL not in caplog.text
    assert PRIVATE_CAUSE not in caplog.text
    # Handlers may render exception or stack details later, outside caplog.text.
    assert caplog.records[0].exc_info is None
    assert caplog.records[0].exc_text is None
    assert caplog.records[0].stack_info is None


async def test_queue_failure_logs_no_private_content_and_next_pass_dispatches(
    caplog: pytest.LogCaptureFixture,
) -> None:
    class Queue:
        reads = 0

        def __init__(self) -> None:
            self.executed: list[str] = []

        async def queued_ids(self, workspace_id: str, *, limit: int) -> tuple[str, ...]:
            self.reads += 1
            if self.reads == 1:
                private_failure()
            return ("minutes-safe",)

        async def execute(self, task_id: str) -> None:
            self.executed.append(task_id)

    queue = Queue()
    await _dispatch_minutes_once(cast(MinutesTasks, queue), "workspace-safe")
    assert queue.executed == []
    await _dispatch_minutes_once(cast(MinutesTasks, queue), "workspace-safe")
    assert queue.executed == ["minutes-safe"]
    assert_bounded_diagnostic(caplog, "minutes queue could not be read")


async def test_task_failure_logs_no_private_content_and_remaining_tasks_dispatch(
    caplog: pytest.LogCaptureFixture,
) -> None:
    class Queue:
        def __init__(self) -> None:
            self.executed: list[str] = []

        async def queued_ids(self, workspace_id: str, *, limit: int) -> tuple[str, ...]:
            return ("minutes-failed", "minutes-safe")

        async def execute(self, task_id: str) -> None:
            if task_id == "minutes-failed":
                private_failure()
            self.executed.append(task_id)

    queue = Queue()
    await _dispatch_minutes_once(cast(MinutesTasks, queue), "workspace-safe")
    assert queue.executed == ["minutes-safe"]
    assert_bounded_diagnostic(caplog, "minutes task minutes-failed did not finish")


async def test_deletion_failure_logs_no_private_content_and_retries_until_cancelled(
    caplog: pytest.LogCaptureFixture,
) -> None:
    class Recovery:
        attempts = 0

        async def run(self) -> tuple[str, ...]:
            self.attempts += 1
            if self.attempts == 1:
                private_failure()
            raise asyncio.CancelledError

    recovery = Recovery()
    with pytest.raises(asyncio.CancelledError):
        async with asyncio.timeout(2):
            await _sweep_deletions(cast(WorkbenchRecovery, recovery), 0)
    assert recovery.attempts == 2
    assert_bounded_diagnostic(caplog, "deletion sweep failed")
