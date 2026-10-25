"""Real loopback warmup failures never grant readiness or repeat a POST by address."""

import socket
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager, nullcontext
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Event, Thread
from types import SimpleNamespace
from typing import Any

import pytest
from tests.fake.controller import build_request, build_template
from tests.unit.controller.test_docker_runtime import (
    _engine,
    _runtime,
    _warmup_body,
    _warmup_rule,
)

from speechnexus_node_controller.domain.intents import ObservedInstanceState
from speechnexus_node_controller.infrastructure import docker_runtime


@contextmanager
def responding(
    mode: str, *, address: str = "127.0.0.1", port: int = 0
) -> Iterator[tuple[ThreadingHTTPServer, list[str], Event]]:
    requests: list[str] = []
    completed, stopping = Event(), Event()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            self.rfile.read(int(self.headers["Content-Length"]))
            requests.append(self.path)
            if mode == "disconnect":
                self.close_connection = True
                return
            try:
                if mode == "headers":
                    self.wfile.write(b"HTTP/1.1 200 OK\r\n")
                    for index in range(24):
                        if stopping.wait(0.05):
                            return
                        self.wfile.write(f"X-Progress-{index}: yes\r\n".encode())
                    completed.set()
                    self.wfile.write(b"Content-Length: 0\r\nConnection: close\r\n\r\n")
                    return
                if mode == "combined" and stopping.wait(0.25):
                    return
                status = 503 if mode == "rejected" else 200
                payload = b"x" * (1025 if mode == "oversized" else 1)
                self.send_response(status)
                self.send_header("Content-Length", "24" if mode == "body" else str(len(payload)))
                if mode == "encoded":
                    self.send_header("Content-Encoding", "gzip")
                self.end_headers()
                if mode == "body":
                    for index in range(24):
                        if stopping.wait(0.05):
                            return
                        if index == 23:
                            completed.set()
                        self.wfile.write(b"x")
                else:
                    if mode == "combined" and stopping.wait(0.25):
                        return
                    completed.set()
                    self.wfile.write(payload)
            except OSError:
                # A correct client closes while this intentionally slow peer is active.
                pass

        def log_message(self, _format: str, *args: object) -> None:
            pass

    server = ThreadingHTTPServer((address, port), Handler)
    thread = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    try:
        yield server, requests, completed
    finally:
        stopping.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        assert not thread.is_alive()


@pytest.mark.parametrize("phase", ["headers", "body", "combined"])
def test_partial_progress_cannot_extend_the_total_warmup_deadline(
    tmp_path: Path, phase: str
) -> None:
    with responding(phase) as (server, requests, completed):
        body, digest = _warmup_body(tmp_path, b"synthetic-warmup")
        rule = _warmup_rule(server, body, digest, attempts=1)
        assert rule.warmup is not None
        rule = replace(rule, warmup=replace(rule.warmup, timeout_seconds=0.4))
        runtime = _runtime(_engine(addresses=("127.0.0.1",)))
        observation = runtime.prepare(build_request(), build_template(readiness=rule))
        assert observation.observed_instance_state is ObservedInstanceState.WARMING_UP
        assert len(requests) == 1
        # Rejecting only after the entire slow response arrives is also a failure.
        assert not completed.is_set()
        assert not runtime._warmup_attempts
        assert observation.observed_peak_memory is not None
        assert observation.stop_evidence is None


@pytest.mark.parametrize("failure", ["rejected", "disconnect", "oversized", "encoded"])
def test_one_prepare_does_not_repeat_a_failed_post_at_the_next_container_address(
    tmp_path: Path, failure: str
) -> None:
    with (
        responding(failure) as (first, first_requests, _),
        responding("ok", address="127.0.0.2", port=first.server_port) as (
            _,
            second_requests,
            _,
        ),
    ):
        body, digest = _warmup_body(tmp_path, b"synthetic-warmup")
        rule = _warmup_rule(first, body, digest, attempts=1)
        assert rule.warmup is not None
        rule = replace(rule, warmup=replace(rule.warmup, max_response_bytes=1024))
        engine = _engine(addresses=("127.0.0.1", "127.0.0.2"))
        runtime = _runtime(engine)
        observation = runtime.prepare(build_request(), build_template(readiness=rule))
        assert observation.observed_instance_state is ObservedInstanceState.WARMING_UP
        assert len(first_requests) == 1 and not second_requests
        assert not runtime._warmup_attempts
        assert len(engine.created) == 1
        assert not engine.stopped and not engine.removed
        assert observation.stop_evidence is None


def test_choose_a_reachable_address_before_sending_the_only_warmup_post(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with responding("ok") as (server, requests, _):
        body, digest = _warmup_body(tmp_path, b"synthetic-warmup")
        rule = _warmup_rule(server, body, digest, attempts=1)
        runtime = _runtime(_engine(addresses=("127.0.0.2", "127.0.0.1")))
        send = runtime._run_warmup
        attempted: list[str] = []

        def observed(address: str, *args: Any, **kwargs: Any) -> bool:
            attempted.append(address)
            return send(address, *args, **kwargs)

        monkeypatch.setattr(runtime, "_run_warmup", observed)
        observation = runtime.prepare(build_request(), build_template(readiness=rule))
        assert observation.observed_instance_state is ObservedInstanceState.READY
        assert attempted == ["127.0.0.1"] and len(requests) == 1


@pytest.mark.parametrize("connects_after_deadline", [False, True])
def test_exhausted_tcp_probe_budget_prevents_warmup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, connects_after_deadline: bool
) -> None:
    elapsed = 0.0
    attempted: list[str] = []

    def probe(address: tuple[str, int], *, timeout: float) -> AbstractContextManager[None]:
        nonlocal elapsed
        attempted.append(address[0])
        if connects_after_deadline:
            elapsed += timeout + 0.01
            return nullcontext()
        elapsed += min(0.25, timeout)
        raise OSError("synthetic unreachable container")

    with responding("ok") as (server, requests, _):
        body, digest = _warmup_body(tmp_path, b"synthetic-warmup")
        rule = _warmup_rule(server, body, digest, attempts=1)
        runtime = _runtime(
            _engine(addresses=("127.0.0.1", "127.0.0.2", "127.0.0.3")),
            readiness_timeout_seconds=0.4,
        )
        monkeypatch.setattr(docker_runtime, "time", SimpleNamespace(monotonic=lambda: elapsed))
        monkeypatch.setattr(socket, "create_connection", probe)
        observation = runtime.prepare(build_request(), build_template(readiness=rule))
        assert observation.observed_instance_state is ObservedInstanceState.LOADING
        assert elapsed == pytest.approx(0.41 if connects_after_deadline else 0.4)
        assert attempted == (
            ["127.0.0.1"] if connects_after_deadline else ["127.0.0.1", "127.0.0.2"]
        )
        assert not requests and not runtime._warmup_attempts
        assert observation.stop_evidence is None
