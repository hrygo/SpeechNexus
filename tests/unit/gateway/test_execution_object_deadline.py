"""Real TCP slow-header/body counterexamples for complete-before-expose input."""

import asyncio
import math
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
import pytest

from speechnexus_gateway.domain.execution_inputs import ExecutionInputError, ExecutionInputLimits
from speechnexus_gateway.infrastructure.execution_objects_minio import MinioExecutionInputs
from tests.unit.gateway.test_execution_objects import BODY, reference

# One trickle frame every 30 ms: far faster than any socket inactivity timeout,
# so peer activity alone cannot extend the read, and far slower than the budgets
# below, which is what keeps the peer writing when the total deadline fires.
TRICKLE_INTERVAL_SECONDS = 0.03
# The peer's trickle total outlives the budget by this factor, so a still-clear
# `trickled` event proves the read was cut rather than drained at any budget.
TRICKLE_BUDGET_MULTIPLE = 20
# The budget races the fast path (dial + HEAD + slow-stage entry) and nothing
# else. A coverage-traced cold first read spends 25-50 ms on one-time costs, and
# a stalled runner adds hundreds of ms on top; 0.5 s keeps the deadline away
# from that tail, which is where the merge gate used to end the read before the
# slow stage was entered (#938).
MINIMUM_BUDGET_SECONDS = 0.5
# A machine whose fast path is genuinely slower gets a proportionally larger
# budget: the slowest of a few calibration reads times this. The calibration
# reads also pay the one-time costs, so they stay outside the budgeted read.
FAST_PATH_MULTIPLE = 10
CALIBRATION_READS = 3
# A single stalled calibration read must not blow the test's runtime up: past
# this ceiling the fast path is a runner problem the failure message shows.
MAXIMUM_BUDGET_SECONDS = 2.0
# The deadline, not the peer's end of stream, must end the read, so the read
# ends at the budget. The upper bound is still an order of magnitude below the
# trickle total, so a drained read could never reach it, while a scheduler stall
# of up to one budget in the detection/cleanup path stays green.
DEADLINE_LOWER_RATIO = 0.9
DEADLINE_UPPER_RATIO = 2.0


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), Handler)
        self.slow_stage = "HEAD.headers"
        self.trickle_frames = 40
        self.started = threading.Event()
        self.started_at: float | None = None
        self.disconnected = threading.Event()
        # Set only when the whole trickle was written; proving it stayed clear
        # is how the test shows the client stopped the read itself.
        self.trickled = threading.Event()
        self.calls: list[tuple[str, str, str]] = []
        ref = reference()
        self.slow_key = "/" + ref.storage_bucket + "/" + ref.object_key

    def mark_started(self) -> None:
        self.started_at = time.monotonic()
        self.started.set()


class Handler(BaseHTTPRequestHandler):
    server: Server
    protocol_version = "HTTP/1.1"

    def log_message(self, message: str, *args: Any) -> None:
        pass

    def do_HEAD(self) -> None:
        self._respond()

    def do_GET(self) -> None:
        self._respond()

    def _respond(self) -> None:
        url = urlsplit(self.path)
        self.server.calls.append((self.command, url.path, url.query))
        slow = url.path == self.server.slow_key
        try:
            self.wfile.write(b"HTTP/1.1 200 OK\r\n")
            if slow and self.server.slow_stage == self.command + ".headers":
                self.server.mark_started()
                # Activity every 30ms defeats an inactivity-only socket timeout.
                for index in range(self.server.trickle_frames):
                    self.wfile.write(f"X-Trickle-{index}: synthetic\r\n".encode())
                    self.wfile.flush()
                    time.sleep(TRICKLE_INTERVAL_SECONDS)
                self.server.trickled.set()
            self.wfile.write(
                (f"x-amz-version-id: v1\r\nContent-Length: {len(BODY)}\r\n\r\n").encode()
            )
            if self.command == "GET":
                if slow and self.server.slow_stage == "GET.body":
                    self.server.mark_started()
                    for index in range(min(len(BODY), self.server.trickle_frames)):
                        self.wfile.write(bytes([BODY[index]]))
                        self.wfile.flush()
                        time.sleep(TRICKLE_INTERVAL_SECONDS)
                    self.server.trickled.set()
                    self.wfile.write(BODY[self.server.trickle_frames :])
                else:
                    self.wfile.write(BODY)
            self.wfile.flush()
        except BrokenPipeError, ConnectionResetError:
            self.server.disconnected.set()
            self.close_connection = True


@pytest.fixture
def server() -> Iterator[Server]:
    value = Server()
    thread = threading.Thread(target=value.serve_forever, kwargs={"poll_interval": 0.01})
    thread.start()
    try:
        yield value
    finally:
        value.shutdown()
        value.server_close()
        thread.join(2)
        assert not thread.is_alive()


def adapter(server: Server, tmp_path: Path) -> MinioExecutionInputs:
    tmp_path.chmod(0o700)
    return MinioExecutionInputs(
        endpoint=f"http://127.0.0.1:{server.server_port}",
        bucket="sn-input-test",
        access_key="synthetic-key",
        secret_key="synthetic-secret",
        spool_directory=tmp_path,
    )


async def read(
    value: MinioExecutionInputs,
    *,
    duration_seconds: float,
    cancelled: Callable[[], bool] = lambda: False,
) -> None:
    async with value.open_verified(
        reference(),
        limits=ExecutionInputLimits(len(BODY), 8, duration_seconds),
        cancelled=cancelled,
    ):
        pytest.fail("partial or cancelled object reached consumer")


async def fast_read(value: MinioExecutionInputs) -> float:
    """Read the whole object once with no slow stage and return its wall clock.

    A first read in a fresh worker pays one-time costs -- httpcore/anyio
    imports, the first connection, the first `ThreadingHTTPServer` handler
    thread -- of tens of milliseconds. Paying them here keeps them out of the
    budgeted read, where they used to be an unaccounted part of the fast path.
    """

    started = time.monotonic()
    async with value.open_verified(
        reference(),
        limits=ExecutionInputLimits(len(BODY), 8, 30),
        cancelled=lambda: False,
    ) as verified:
        assert verified.stream.read() == BODY
    return time.monotonic() - started


async def calibrated_budget(value: MinioExecutionInputs) -> float:
    """Return a budget an order of magnitude above this machine's fast path."""

    samples = [await fast_read(value) for _ in range(CALIBRATION_READS)]
    calibrated = FAST_PATH_MULTIPLE * max(samples)
    return min(MAXIMUM_BUDGET_SECONDS, max(MINIMUM_BUDGET_SECONDS, calibrated))


@pytest.mark.parametrize("stage", ["HEAD.headers", "GET.headers", "GET.body"])
async def test_trickling_input_cannot_extend_total_deadline_and_releases_socket(
    server: Server, tmp_path: Path, stage: str
) -> None:
    # Calibrate with the slow stage disabled: the budget below has to outlive
    # the fast path, and measuring that path on this machine is the only way to
    # state that outliving as a bound instead of a guess.
    server.slow_stage = ""
    value = adapter(server, tmp_path)
    budget = await calibrated_budget(value)
    server.slow_stage = stage
    # The peer keeps trickling long past the budget, so only a total deadline
    # can end this read, and it has to do so while the peer still writes.
    server.trickle_frames = math.ceil(TRICKLE_BUDGET_MULTIPLE * budget / TRICKLE_INTERVAL_SECONDS)
    server.calls.clear()
    started = time.monotonic()
    with pytest.raises(ExecutionInputError, match="execution_input_deadline_exceeded"):
        await read(value, duration_seconds=budget)
    elapsed = time.monotonic() - started
    fast_path = None if server.started_at is None else server.started_at - started
    observed = (
        f"budget={budget:.3f}s elapsed={elapsed:.3f}s fast_path={fast_path} "
        f"trickled={server.trickled.is_set()} "
        f"calls={[verb for verb, _, _ in server.calls]}"
    )
    # The slow stage must have been entered: otherwise the deadline ended the
    # read before the behaviour under test was reached. The order-of-magnitude
    # margin, not a rerun, is what keeps that from happening at random.
    assert server.started.is_set(), observed
    assert fast_path is not None and fast_path < budget, observed
    # The peer never finished trickling: the read was cut, not drained.
    assert not server.trickled.is_set(), observed
    # Ended at the total deadline: near the budget, an order of magnitude from
    # the trickle total a drained read would have lasted.
    assert budget * DEADLINE_LOWER_RATIO <= elapsed <= budget * DEADLINE_UPPER_RATIO, observed
    assert [verb for verb, _, _ in server.calls] == (
        ["HEAD"] if stage == "HEAD.headers" else ["HEAD", "GET"]
    )
    assert all(query == "versionId=v1" for verb, _, query in server.calls if verb == "GET")
    assert await asyncio.to_thread(server.disconnected.wait, 1)
    assert not await asyncio.to_thread(lambda: list(tmp_path.iterdir()))


@pytest.mark.parametrize("stage", ["HEAD.headers", "GET.headers", "GET.body"])
@pytest.mark.parametrize("cancel_kind", ["task", "flag"])
async def test_cancel_interrupts_input_and_joins_own_cleanup(
    server: Server, tmp_path: Path, stage: str, cancel_kind: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    server.slow_stage = stage
    value = adapter(server, tmp_path)
    factory = value._new_client
    clients: list[httpx.AsyncClient] = []

    def client() -> httpx.AsyncClient:
        result = factory()
        clients.append(result)
        return result

    monkeypatch.setattr(value, "_new_client", client)
    cancelled = False
    task = asyncio.create_task(read(value, duration_seconds=5, cancelled=lambda: cancelled))
    try:
        assert await asyncio.to_thread(server.started.wait, 2)
        started = time.monotonic()
        if cancel_kind == "task":
            task.cancel()
            await asyncio.sleep(0)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            cancelled = True
            with pytest.raises(ExecutionInputError, match="execution_input_cancelled"):
                await task
        assert time.monotonic() - started < 0.5
        assert clients and all(client.is_closed for client in clients)
        assert await asyncio.to_thread(server.disconnected.wait, 1)
        assert not await asyncio.to_thread(lambda: list(tmp_path.iterdir()))
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_slow_input_keeps_event_loop_live_and_cancelling_it_preserves_other_input(
    server: Server, tmp_path: Path
) -> None:
    value = adapter(server, tmp_path)
    slow = asyncio.create_task(read(value, duration_seconds=5))
    try:
        assert await asyncio.to_thread(server.started.wait, 2)
        fast_reference = replace(reference(), object_key="private/assets/asset-input/another")
        started = time.monotonic()
        async with value.open_verified(
            fast_reference,
            limits=ExecutionInputLimits(len(BODY), 8, 2),
            cancelled=lambda: False,
        ) as verified:
            # A second complete request advances while the first awaits headers.
            assert verified.stream.read() == BODY
            slow.cancel()
            with pytest.raises(asyncio.CancelledError):
                await slow
            assert verified.stream.seek(0) == 0 and verified.stream.read() == BODY
        assert time.monotonic() - started < 0.5
        assert await asyncio.to_thread(server.disconnected.wait, 1)
    finally:
        slow.cancel()
        await asyncio.gather(slow, return_exceptions=True)
