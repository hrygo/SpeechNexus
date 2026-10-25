"""Real TCP slow-header/body counterexamples for complete-before-expose input."""

import asyncio
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


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), Handler)
        self.slow_stage = "HEAD.headers"
        self.started = threading.Event()
        self.disconnected = threading.Event()
        self.calls: list[tuple[str, str, str]] = []
        ref = reference()
        self.slow_key = "/" + ref.storage_bucket + "/" + ref.object_key


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
                self.server.started.set()
                # Activity every 30ms defeats an inactivity-only socket timeout.
                for index in range(40):
                    self.wfile.write(f"X-Trickle-{index}: synthetic\r\n".encode())
                    self.wfile.flush()
                    time.sleep(0.03)
            self.wfile.write(
                (f"x-amz-version-id: v1\r\nContent-Length: {len(BODY)}\r\n\r\n").encode()
            )
            if self.command == "GET":
                if slow and self.server.slow_stage == "GET.body":
                    self.server.started.set()
                    for byte in BODY[:40]:
                        self.wfile.write(bytes([byte]))
                        self.wfile.flush()
                        time.sleep(0.03)
                    self.wfile.write(BODY[40:])
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


@pytest.mark.parametrize("stage", ["HEAD.headers", "GET.headers", "GET.body"])
async def test_trickling_input_cannot_extend_total_deadline_and_releases_socket(
    server: Server, tmp_path: Path, stage: str
) -> None:
    server.slow_stage = stage
    value = adapter(server, tmp_path)
    started = time.monotonic()
    with pytest.raises(ExecutionInputError, match="execution_input_deadline_exceeded"):
        await read(value, duration_seconds=0.08)
    assert time.monotonic() - started < 0.5
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
