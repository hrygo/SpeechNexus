"""Export dispatch reports bounded diagnostics without provider or content details."""

import asyncio
from typing import cast

import pytest

from speechnexus_backend.workbench.application.export_worker import ExportWorker
from speechnexus_backend.workbench.runtime import _dispatch_exports


async def test_a_failed_export_pass_logs_no_exception_content_and_the_loop_can_stop(
    caplog: pytest.LogCaptureFixture,
) -> None:
    private_detail = "synthetic transcript and private object endpoint"

    class FailingPass:
        def __init__(self) -> None:
            self.limits: list[int] = []

        async def run_once(self, *, limit: int) -> int:
            self.limits.append(limit)
            if len(self.limits) == 1:
                raise RuntimeError(private_detail)
            raise asyncio.CancelledError

    worker = FailingPass()
    with pytest.raises(asyncio.CancelledError):
        await _dispatch_exports(cast(ExportWorker, worker), 0)

    assert worker.limits == [4, 4]
    assert len(caplog.records) == 1
    assert "export pass failed" in caplog.text
    assert private_detail not in caplog.text
    assert caplog.records[0].exc_info is None
