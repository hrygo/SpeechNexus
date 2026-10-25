"""Real loopback slow-header counterexamples; no models, credentials or external network."""

import asyncio
import threading
import time
from collections.abc import Iterator
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import pytest

from speechnexus_gateway.domain.asr_results import AsrResultError
from speechnexus_gateway.infrastructure.asr_results_minio import MinioAsrResults
from tests.unit.gateway.test_asr_results_minio import RESULT


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), Handler)
        self.slow_method = "PUT"
        self.started = threading.Event()
        self.calls: list[tuple[str, str]] = []
        self.bodies: dict[str, bytes] = {}
        self.slow_key = (
            "/"
            + RESULT.reference("sn-output-test").storage_bucket
            + "/"
            + (RESULT.reference("sn-output-test").object_key)
        )


class Handler(BaseHTTPRequestHandler):
    server: Server
    protocol_version = "HTTP/1.1"

    def log_message(self, message: str, *args: Any) -> None:
        pass

    def do_PUT(self) -> None:
        self._respond()

    def do_HEAD(self) -> None:
        self._respond()

    def do_GET(self) -> None:
        self._respond()

    def _respond(self) -> None:
        path = urlsplit(self.path).path
        self.server.calls.append((self.command, path))
        if self.command == "PUT":
            self.server.bodies[path] = self.rfile.read(int(self.headers["Content-Length"]))
        body = self.server.bodies.get(path, b"")
        try:
            self.wfile.write(b"HTTP/1.1 200 OK\r\n")
            if self.command == self.server.slow_method and path == self.server.slow_key:
                self.server.started.set()
                # Every packet arrives well within a socket inactivity timeout.
                for index in range(40):
                    self.wfile.write(f"X-Trickle-{index}: synthetic\r\n".encode())
                    self.wfile.flush()
                    time.sleep(0.03)
            length = len(body) if self.command != "PUT" else 0
            self.wfile.write(
                (
                    "x-amz-version-id: v1\r\n"
                    "Content-Type: application/json\r\n"
                    f"Content-Length: {length}\r\n\r\n"
                ).encode()
            )
            if self.command == "GET":
                self.wfile.write(body)
            self.wfile.flush()
        except BrokenPipeError, ConnectionResetError:
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


def adapter(server: Server, tmp_path: Path, timeout: float) -> MinioAsrResults:
    tmp_path.chmod(0o700)
    return MinioAsrResults(
        endpoint=f"http://127.0.0.1:{server.server_port}",
        bucket="sn-output-test",
        access_key="synthetic-key",
        secret_key="synthetic-secret",
        timeout_seconds=timeout,
    )


@pytest.mark.parametrize("method", ["PUT", "HEAD", "GET"])
async def test_trickle_headers_cannot_extend_total_deadline(
    server: Server, tmp_path: Path, method: str
) -> None:
    server.slow_method = method
    value = adapter(server, tmp_path, 0.08)
    started = time.monotonic()
    try:
        with pytest.raises(AsrResultError, match="asr_result_deadline_exceeded"):
            await value.stage(RESULT, cancelled=lambda: False)
        assert time.monotonic() - started < 0.5
        assert [verb for verb, _ in server.calls] == {
            "PUT": ["PUT"],
            "HEAD": ["PUT", "HEAD"],
            "GET": ["PUT", "HEAD", "GET"],
        }[method]
        # The server accepted the bytes before its stalled response: a local
        # deadline must never be interpreted as rollback or permission to retry.
        assert server.bodies[server.slow_key] == RESULT.body
        assert not any(method == "DELETE" for method, _ in server.calls)
    finally:
        value.close()


@pytest.mark.parametrize("method", ["PUT", "HEAD", "GET"])
async def test_cancel_interrupts_own_trickle_headers_before_cleanup_returns(
    server: Server, tmp_path: Path, method: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    server.slow_method = method
    value = adapter(server, tmp_path, 5)
    factory = value._new_client
    clients = []

    def client() -> Any:
        result = factory()
        clients.append(result)
        return result

    monkeypatch.setattr(value, "_new_client", client)
    task = asyncio.create_task(value.stage(RESULT, cancelled=lambda: False))
    try:
        assert await asyncio.to_thread(server.started.wait, 2)
        started = time.monotonic()
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert time.monotonic() - started < 0.5
        assert clients and all(client.is_closed for client in clients)
        assert sum(method == "PUT" for method, _ in server.calls) == 1
        assert server.bodies[server.slow_key] == RESULT.body
    finally:
        value.close()


@pytest.mark.parametrize("method", ["PUT", "HEAD", "GET"])
async def test_cancel_flag_interrupts_trickle_headers_without_waiting_for_a_packet(
    server: Server, tmp_path: Path, method: str
) -> None:
    server.slow_method = method
    value = adapter(server, tmp_path, 5)
    cancelled = False
    task = asyncio.create_task(value.stage(RESULT, cancelled=lambda: cancelled))
    try:
        assert await asyncio.to_thread(server.started.wait, 2)
        started = time.monotonic()
        cancelled = True
        with pytest.raises(AsrResultError, match="asr_result_cancelled"):
            await task
        assert time.monotonic() - started < 0.5
        assert sum(method == "PUT" for method, _ in server.calls) == 1
        assert server.bodies[server.slow_key] == RESULT.body
    finally:
        value.close()


async def test_cancel_one_write_does_not_close_another_operations_client(
    server: Server, tmp_path: Path
) -> None:
    value = adapter(server, tmp_path, 5)
    slow = asyncio.create_task(value.stage(RESULT, cancelled=lambda: False))
    try:
        assert await asyncio.to_thread(server.started.wait, 2)
        fast_result = replace(RESULT, request_digest="sha256:" + "b" * 64)
        fast = asyncio.create_task(value.stage(fast_result, cancelled=lambda: False))
        slow.cancel()
        started = time.monotonic()
        with pytest.raises(asyncio.CancelledError):
            await slow
        assert time.monotonic() - started < 0.5
        receipt = await fast
        assert receipt.request_digest == fast_result.request_digest
        assert receipt.reference == fast_result.reference("sn-output-test")
    finally:
        value.close()
