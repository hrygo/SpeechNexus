"""Cancellation must join startup before closing the execution authority."""

import asyncio
import threading
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from speechnexus_gateway.app import _close_control, create_app
from speechnexus_gateway.application.barrier_control import GatewayBarrierService
from speechnexus_gateway.application.instance_drain import InstanceDrainService
from speechnexus_gateway.application.query_execution import ExecutionQueryService
from speechnexus_gateway.domain.file_execution import FileExecutionLimits
from speechnexus_gateway.infrastructure.file_execution_worker import FileExecutionWorker
from tests.fake.execution import InMemorySubmissionLedger, fake_ingress
from tests.unit.gateway.test_barrier_http import settings_for
from tests.unit.gateway.test_file_execution_worker import Pipeline, Results


def runtime(**options: Any) -> FileExecutionWorker[Results]:
    limits = FileExecutionLimits(
        1024,
        16384,
        options.pop("max_pending", 1),
        options.pop("shutdown_seconds", 10),
    )
    return FileExecutionWorker(
        ingress=fake_ingress(),
        ledger=InMemorySubmissionLedger(),
        execution=Pipeline(),
        results=Results(),
        limits=limits,
        engine_request_prefix="file-asr",
        **options,
    )


@pytest.mark.parametrize("phase", ["control", "factory", "open"])
@pytest.mark.parametrize("open_fails", [False, True])
async def test_cancelled_startup_joins_before_releasing_authority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, phase: str, open_fails: bool
) -> None:
    entered, release, open_finished = threading.Event(), threading.Event(), threading.Event()
    closed: list[str] = []
    ingress, ledger = fake_ingress(), InMemorySubmissionLedger()

    def gate(name: str) -> None:
        if phase == name:
            entered.set()
            assert release.wait(3), "the test must release its startup operation"

    class GatedWorker(FileExecutionWorker[Results]):
        def open(self) -> None:
            try:
                gate("open")
                if open_fails:
                    raise RuntimeError("synthetic startup failed")
                super().open()
            finally:
                open_finished.set()

        def close(self) -> None:
            super().close()
            closed.append("file")

    worker = GatedWorker(
        ingress=ingress,
        ledger=ledger,
        execution=Pipeline(),
        results=Results(),
        limits=FileExecutionLimits(1024, 16384, 1, 10),
        engine_request_prefix="file-asr",
    )

    def build(_: Any) -> tuple[Any, ...]:
        gate("control")
        return (
            GatewayBarrierService(ingress=ingress, ledger=ledger),
            ExecutionQueryService(ingress=ingress, ledger=ledger),
            InstanceDrainService(ingress=ingress, ledger=ledger),
            SimpleNamespace(close=lambda: closed.append("ownership")),
            SimpleNamespace(close=lambda: closed.append("pool")),
            None,
            ingress,
            ledger,
        )

    def factory(*_: Any) -> FileExecutionWorker[Results]:
        gate("factory")
        return worker

    monkeypatch.setattr("speechnexus_gateway.app._build_control", build)
    app = create_app(settings=settings_for(tmp_path), file_execution_factory=factory)
    lifespan = app.router.lifespan_context(app)
    startup = asyncio.create_task(lifespan.__aenter__())
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        startup.cancel()
        await asyncio.sleep(0.01)
        startup.cancel()
        await asyncio.sleep(0.01)
        assert not startup.done() and not closed
        release.set()
        expected: type[BaseException] = (
            RuntimeError if open_fails and phase == "open" else asyncio.CancelledError
        )
        with pytest.raises(expected):
            await asyncio.wait_for(startup, 2)
        assert closed == (
            ["pool", "ownership"] if phase == "control" else ["file", "pool", "ownership"]
        )
        assert not worker.running
        assert app.state.file_execution_runtime is app.state.execution_query is None
    finally:
        release.set()
        await asyncio.gather(startup, return_exceptions=True)
        # Also clean the old-code RED case where startup continued after cancellation.
        if phase == "open":
            assert await asyncio.to_thread(open_finished.wait, 2)
        worker.close()


def test_failed_thread_start_cannot_report_successful_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    release = threading.Event()
    worker = runtime(shutdown_seconds=0.02)
    original: Callable[[], None] = worker._serve

    def late_start() -> None:
        assert release.wait(3)
        original()

    monkeypatch.setattr(worker, "_serve", late_start)
    try:
        with pytest.raises(RuntimeError, match="did not start"):
            worker.open()
        assert worker._thread is not None and worker._thread.is_alive()
        with pytest.raises(RuntimeError, match=r"cleanup|owned resources"):
            worker.close()
    finally:
        release.set()
        if worker._thread is not None:
            # After a failed startup, the late thread must see the close request.
            worker._thread.join(0.1)
            if worker._thread.is_alive():  # Clean up the pre-fix RED case too.
                assert worker._ready.wait(2)
                worker.close()
        worker.close()
    assert not worker.running
    assert worker._thread is not None and not worker._thread.is_alive()


def test_closed_runtime_cannot_later_start_and_reuse_released_authority() -> None:
    worker = runtime()
    worker.close()
    try:
        with pytest.raises(RuntimeError, match=r"closed|reused"):
            worker.open()
    finally:
        worker.close()


@pytest.mark.parametrize("phase", ["inventory", "thread"])
def test_failed_startup_without_a_live_thread_releases_unused_control(
    monkeypatch: pytest.MonkeyPatch, phase: str
) -> None:
    worker = runtime()
    closed: list[str] = []

    def reject_inventory(**_: Any) -> list[Any]:
        raise RuntimeError("synthetic startup failure")

    start = threading.Thread.start

    def reject_file_thread(thread: threading.Thread) -> None:
        if thread.name == "file-execution":
            raise RuntimeError("synthetic startup failure")
        start(thread)

    if phase == "inventory":
        monkeypatch.setattr(worker._ledger, "list_unfinished", reject_inventory)
    else:
        monkeypatch.setattr(threading.Thread, "start", reject_file_thread)
    with pytest.raises(RuntimeError, match="synthetic startup failure"):
        worker.open()
    assert worker._thread is None or not worker._thread.is_alive()
    pool: Any = SimpleNamespace(close=lambda: closed.append("pool"))
    ownership: Any = SimpleNamespace(close=lambda: closed.append("ownership"))
    _close_control(None, pool, ownership, worker)
    assert closed == ["pool", "ownership"]
    assert not worker.running
    with pytest.raises(RuntimeError, match=r"closed|reused"):
        worker.open()
