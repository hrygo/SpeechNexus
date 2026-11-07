"""Real loopback warmup failures never grant readiness or repeat a POST by address."""

import socket
import time
from collections.abc import Iterator
from contextlib import AbstractContextManager, ExitStack, contextmanager, nullcontext
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Event, Thread
from types import SimpleNamespace
from typing import Any

import pytest
from tests.fake.controller import (
    FIXED_NOW,
    SAMPLE_INSTANCE_ID,
    SAMPLE_NODE_ID,
    InMemoryIntentStore,
    build_request,
    build_template,
)
from tests.unit.controller.test_docker_runtime import (
    _engine,
    _runtime,
    _warmup_body,
    _warmup_rule,
)

from speechnexus_node_controller.application.runtime_control import NodeRuntimeControlService
from speechnexus_node_controller.domain.intents import (
    MemoryHold,
    ObservedInstanceState,
    RuntimeActionKind,
    RuntimeIntentErrorCode,
    RuntimeIntentState,
)
from speechnexus_node_controller.domain.templates import (
    ApprovedTemplateRegistry,
    WarmupRequest,
)
from speechnexus_node_controller.infrastructure import docker_runtime


def _only_progress(runtime: Any) -> Any:
    assert len(runtime._warmup_progress) == 1
    return next(iter(runtime._warmup_progress.values()))


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
                if mode == "stall":
                    stopping.wait(5)
                    return
                if mode == "redirect":
                    self.send_response(302)
                    self.send_header("Location", "http://127.0.0.2/")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    completed.set()
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


@contextmanager
def _replied(
    status: int, *, location: str | None = None
) -> Iterator[tuple[ThreadingHTTPServer, list[tuple[str, str]]]]:
    """A loopback endpoint that records every method it actually receives."""
    requests: list[tuple[str, str]] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            self.rfile.read(int(self.headers.get("Content-Length", "0")))
            requests.append(("POST", self.path))
            self._reply()

        def do_GET(self) -> None:
            requests.append(("GET", self.path))
            self._reply()

        def _reply(self) -> None:
            self.send_response(status if self.command == "POST" else 200)
            if location is not None:
                self.send_header("Location", location)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, _format: str, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    try:
        yield server, requests
    finally:
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
        assert _only_progress(runtime).completed == 0
        assert observation.observed_peak_memory is not None
        assert observation.stop_evidence is None


@pytest.mark.parametrize("failure", ["rejected", "redirect", "disconnect", "oversized", "encoded"])
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
        assert _only_progress(runtime).completed == 0
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
        assert not requests and _only_progress(runtime).completed == 0
        assert observation.stop_evidence is None


def test_attempts_share_one_deadline_and_exhaustion_sends_nothing_more(tmp_path: Path) -> None:
    with responding("stall") as (server, requests, _):
        body, digest = _warmup_body(tmp_path, b"synthetic-warmup")
        rule = _warmup_rule(server, body, digest, attempts=4)
        assert rule.warmup is not None
        rule = replace(rule, warmup=replace(rule.warmup, timeout_seconds=0.35))
        template = build_template(readiness=rule)
        runtime = _runtime(_engine(addresses=("127.0.0.1",)))
        started = time.monotonic()

        first = runtime.prepare(build_request(), template)

        elapsed = time.monotonic() - started
        assert first.observed_instance_state is ObservedInstanceState.WARMING_UP
        assert len(requests) == 1
        # Four attempts must not multiply the 0.35 s wall-clock action budget.
        assert elapsed < 0.35 * 2

        for _ in range(3):
            repeated = runtime.prepare(build_request(), template)
            assert repeated.observed_instance_state is ObservedInstanceState.WARMING_UP
        assert len(requests) == 1
        assert time.monotonic() - started < 0.35 * 2
        assert first.observed_peak_memory is not None
        assert first.stop_evidence is None


def test_a_stalled_response_is_non_ready_and_never_retried_at_another_address(
    tmp_path: Path,
) -> None:
    with (
        responding("stall") as (first, first_requests, _),
        responding("ok", address="127.0.0.2", port=first.server_port) as (
            _,
            second_requests,
            _,
        ),
    ):
        body, digest = _warmup_body(tmp_path, b"synthetic-warmup")
        rule = _warmup_rule(first, body, digest, attempts=1)
        assert rule.warmup is not None
        rule = replace(rule, warmup=replace(rule.warmup, timeout_seconds=0.3))
        runtime = _runtime(_engine(addresses=("127.0.0.1", "127.0.0.2")))

        observation = runtime.prepare(build_request(), build_template(readiness=rule))

        assert observation.observed_instance_state is ObservedInstanceState.WARMING_UP
        assert len(first_requests) == 1 and not second_requests


def test_exhausted_action_budget_stops_address_probing_and_warmup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    elapsed = 0.0
    attempted: list[str] = []

    def probe(address: tuple[str, int], *, timeout: float) -> AbstractContextManager[None]:
        nonlocal elapsed
        attempted.append(address[0])
        elapsed += min(timeout, 0.2)
        raise OSError("synthetic unreachable container")

    with responding("ok") as (server, requests, _):
        body, digest = _warmup_body(tmp_path, b"synthetic-warmup")
        rule = _warmup_rule(server, body, digest, attempts=1)
        assert rule.warmup is not None
        rule = replace(rule, warmup=replace(rule.warmup, timeout_seconds=0.4))
        template = build_template(readiness=rule)
        runtime = _runtime(
            _engine(addresses=("127.0.0.1", "127.0.0.2", "127.0.0.3")),
            readiness_timeout_seconds=0.5,
        )
        monkeypatch.setattr(docker_runtime, "time", SimpleNamespace(monotonic=lambda: elapsed))
        monkeypatch.setattr(socket, "create_connection", probe)

        observation = runtime.prepare(build_request(), template)

        assert observation.observed_instance_state is ObservedInstanceState.WARMING_UP
        assert elapsed == pytest.approx(0.4)
        assert attempted == ["127.0.0.1", "127.0.0.2"]
        assert not requests and _only_progress(runtime).completed == 0

        repeated = runtime.prepare(build_request(), template)
        assert repeated.observed_instance_state is ObservedInstanceState.WARMING_UP
        assert attempted == ["127.0.0.1", "127.0.0.2"]
        assert not requests


def test_container_start_spends_the_one_warmup_budget_anchored_before_daemon_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    elapsed = 0.0
    attempted: list[float] = []

    def probe(address: tuple[str, int], *, timeout: float) -> AbstractContextManager[None]:
        attempted.append(timeout)
        raise OSError("synthetic unreachable container")

    with responding("ok") as (server, requests, _):
        body, digest = _warmup_body(tmp_path, b"synthetic-warmup")
        rule = _warmup_rule(server, body, digest, attempts=1)
        assert rule.warmup is not None
        rule = replace(rule, warmup=replace(rule.warmup, timeout_seconds=0.4))
        template = build_template(readiness=rule)
        engine = _engine(addresses=("127.0.0.1",))
        start = engine.start_container

        def slow_start(container_id: str) -> None:
            nonlocal elapsed
            elapsed += 0.3
            start(container_id)

        monkeypatch.setattr(engine, "start_container", slow_start)
        monkeypatch.setattr(docker_runtime, "time", SimpleNamespace(monotonic=lambda: elapsed))
        monkeypatch.setattr(socket, "create_connection", probe)
        runtime = _runtime(engine, readiness_timeout_seconds=5)

        first = runtime.prepare(build_request(), template)

        # The deadline started before start_container, so only 0.1 s remains.
        assert first.observed_instance_state is ObservedInstanceState.LOADING
        assert attempted == [pytest.approx(0.1)]
        assert not requests and _only_progress(runtime).completed == 0

        repeated = runtime.prepare(build_request(), template)
        assert repeated.observed_instance_state is ObservedInstanceState.LOADING
        # A repeated prepare reuses the anchor instead of granting a fresh 0.4 s.
        assert attempted == [pytest.approx(0.1), pytest.approx(0.1)]
        assert not requests


@pytest.mark.parametrize(
    "slow_phase", ["inspect_image", "create_container", "start_container", "inspect_container"]
)
def test_budget_spent_during_daemon_work_sends_no_probe_and_no_post(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, slow_phase: str
) -> None:
    elapsed = 0.0
    attempted: list[float] = []

    def probe(address: tuple[str, int], *, timeout: float) -> AbstractContextManager[None]:
        attempted.append(timeout)
        return nullcontext()

    with responding("ok") as (server, requests, _):
        body, digest = _warmup_body(tmp_path, b"synthetic-warmup")
        rule = _warmup_rule(server, body, digest, attempts=1)
        assert rule.warmup is not None
        rule = replace(rule, warmup=replace(rule.warmup, timeout_seconds=0.4))
        template = build_template(readiness=rule)
        engine = _engine(addresses=("127.0.0.1",))
        original = getattr(engine, slow_phase)

        def exhausted(*args: Any, **kwargs: Any) -> Any:
            nonlocal elapsed
            elapsed += 0.5
            return original(*args, **kwargs)

        monkeypatch.setattr(engine, slow_phase, exhausted)
        monkeypatch.setattr(docker_runtime, "time", SimpleNamespace(monotonic=lambda: elapsed))
        monkeypatch.setattr(socket, "create_connection", probe)
        runtime = _runtime(engine)

        first = runtime.prepare(build_request(), template)

        assert first.observed_instance_state is ObservedInstanceState.WARMING_UP
        assert not attempted and not requests
        assert _only_progress(runtime).completed == 0
        assert first.observed_peak_memory is not None
        assert first.stop_evidence is None

        repeated = runtime.prepare(build_request(), template)
        assert repeated.observed_instance_state is ObservedInstanceState.WARMING_UP
        assert not attempted and not requests
        assert len(engine.created) == 1


def test_replacement_container_inherits_the_original_action_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    elapsed = 0.0
    attempted: list[float] = []

    def probe(address: tuple[str, int], *, timeout: float) -> AbstractContextManager[None]:
        attempted.append(timeout)
        raise OSError("synthetic unreachable container")

    with responding("ok") as (server, requests, _):
        body, digest = _warmup_body(tmp_path, b"synthetic-warmup")
        rule = _warmup_rule(server, body, digest, attempts=4)
        assert rule.warmup is not None
        rule = replace(rule, warmup=replace(rule.warmup, timeout_seconds=0.4))
        template = build_template(readiness=rule)
        engine = _engine(addresses=("127.0.0.1",))
        runtime = _runtime(engine, readiness_timeout_seconds=0.2)
        monkeypatch.setattr(docker_runtime, "time", SimpleNamespace(monotonic=lambda: elapsed))
        monkeypatch.setattr(socket, "create_connection", probe)

        first = runtime.prepare(build_request(), template)

        assert first.observed_instance_state is ObservedInstanceState.LOADING
        assert attempted == [pytest.approx(0.2)]
        original = engine.created[0].container_id
        assert runtime._warmup_deadlines[(SAMPLE_INSTANCE_ID, 1)] == pytest.approx(0.4)

        # The original container vanishes after the one action deadline has passed.
        elapsed = 0.5
        del engine.containers[original]

        repeated = runtime.prepare(build_request(), template)

        # A replacement container may exist, but it inherits the spent deadline.
        assert repeated.observed_instance_state is ObservedInstanceState.WARMING_UP
        assert len(engine.created) == 2
        assert attempted == [pytest.approx(0.2)]
        assert not requests
        assert _only_progress(runtime).completed == 0
        assert _only_progress(runtime).deadline == pytest.approx(0.4)
        assert runtime._warmup_deadlines[(SAMPLE_INSTANCE_ID, 1)] == pytest.approx(0.4)


def test_confirmed_stop_lets_the_same_generation_start_a_new_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    elapsed = 0.0
    attempted: list[float] = []
    healthy = False

    def probe(address: tuple[str, int], *, timeout: float) -> AbstractContextManager[None]:
        attempted.append(timeout)
        if healthy:
            return nullcontext()
        raise OSError("synthetic unreachable container")

    with responding("ok") as (server, requests, _):
        body, digest = _warmup_body(tmp_path, b"synthetic-warmup")
        rule = _warmup_rule(server, body, digest, attempts=1)
        assert rule.warmup is not None
        rule = replace(rule, warmup=replace(rule.warmup, timeout_seconds=0.4))
        template = build_template(readiness=rule)
        engine = _engine(addresses=("127.0.0.1",))
        runtime = _runtime(engine, readiness_timeout_seconds=0.2)
        monkeypatch.setattr(docker_runtime, "time", SimpleNamespace(monotonic=lambda: elapsed))
        monkeypatch.setattr(socket, "create_connection", probe)

        first = runtime.prepare(build_request(), template)

        assert first.observed_instance_state is ObservedInstanceState.LOADING
        assert runtime._warmup_deadlines[(SAMPLE_INSTANCE_ID, 1)] == pytest.approx(0.4)

        stopped = runtime.stop(build_request(action=RuntimeActionKind.STOP), template)

        assert stopped.observed_instance_state is ObservedInstanceState.STOPPED
        assert stopped.stop_evidence is not None
        assert (SAMPLE_INSTANCE_ID, 1) not in runtime._warmup_deadlines
        assert engine.containers == {}

        # After a confirmed release the next same-generation action is new.
        healthy = True
        elapsed = 5.0
        repeated = runtime.prepare(build_request(), template)

        assert repeated.observed_instance_state is ObservedInstanceState.READY
        assert len(engine.created) == 2
        assert attempted == [pytest.approx(0.2), pytest.approx(0.2)]
        assert len(requests) == 1


def test_generation_change_starts_a_new_action_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    elapsed = 0.0
    attempted: list[float] = []
    healthy = False

    def probe(address: tuple[str, int], *, timeout: float) -> AbstractContextManager[None]:
        attempted.append(timeout)
        if healthy:
            return nullcontext()
        raise OSError("synthetic unreachable container")

    with responding("ok") as (server, requests, _):
        body, digest = _warmup_body(tmp_path, b"synthetic-warmup")
        rule = _warmup_rule(server, body, digest, attempts=1)
        assert rule.warmup is not None
        rule = replace(rule, warmup=replace(rule.warmup, timeout_seconds=0.4))
        template = build_template(readiness=rule)
        engine = _engine(addresses=("127.0.0.1",))
        runtime = _runtime(engine, readiness_timeout_seconds=0.2)
        monkeypatch.setattr(docker_runtime, "time", SimpleNamespace(monotonic=lambda: elapsed))
        monkeypatch.setattr(socket, "create_connection", probe)

        first = runtime.prepare(build_request(), template)

        assert first.observed_instance_state is ObservedInstanceState.LOADING
        assert runtime._warmup_deadlines[(SAMPLE_INSTANCE_ID, 1)] == pytest.approx(0.4)

        healthy = True
        elapsed = 5.0
        repeated = runtime.prepare(build_request(instance_generation=2), template)

        assert repeated.observed_instance_state is ObservedInstanceState.READY
        assert len(engine.created) == 2
        assert attempted == [pytest.approx(0.2), pytest.approx(0.2)]
        assert len(requests) == 1
        assert (SAMPLE_INSTANCE_ID, 1) not in runtime._warmup_deadlines
        # Readiness ends the action, so its anchor is dropped as well.
        assert (SAMPLE_INSTANCE_ID, 2) not in runtime._warmup_deadlines


def test_a_restarted_adapter_reports_warming_up_without_probing_or_posting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    probed: list[str] = []

    def probe(address: tuple[str, int], *, timeout: float) -> AbstractContextManager[None]:
        probed.append(address[0])
        return nullcontext()

    with responding("ok") as (server, requests, _):
        body, digest = _warmup_body(tmp_path, b"synthetic-warmup")
        rule = _warmup_rule(server, body, digest, attempts=2)
        template = build_template(readiness=rule)
        engine = _engine(addresses=("127.0.0.1",))
        first = _runtime(engine)
        elapsed = 0.0
        monkeypatch.setattr(docker_runtime, "time", SimpleNamespace(monotonic=lambda: elapsed))
        monkeypatch.setattr(socket, "create_connection", probe)

        prepared = first.prepare(build_request(), template)
        assert prepared.observed_instance_state is ObservedInstanceState.WARMING_UP
        assert len(requests) == 1 and probed

        # Restart: a new adapter has no in-memory anchor for the persisted intent.
        probed.clear()
        restarted = _runtime(engine)
        observed = restarted.inspect(build_request(), template, resumed=True)

        assert observed is not None
        assert observed.observed_instance_state is ObservedInstanceState.WARMING_UP
        assert observed.observed_peak_memory is not None
        assert not probed and len(requests) == 1
        assert restarted._warmup_progress == {} and restarted._warmup_deadlines == {}

        # Repeated observations of the same resumed intent stay closed as well.
        again = restarted.inspect(build_request(), template, resumed=True)
        assert again is not None
        assert again.observed_instance_state is ObservedInstanceState.WARMING_UP
        assert not probed and len(requests) == 1
        assert restarted._warmup_progress == {} and restarted._warmup_deadlines == {}


def test_an_intent_resumed_in_the_same_process_spends_the_remaining_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    elapsed = 0.0
    deadlines: list[float] = []

    with responding("ok") as (server, requests, _):
        body, digest = _warmup_body(tmp_path, b"synthetic-warmup")
        rule = _warmup_rule(server, body, digest, attempts=2)
        assert rule.warmup is not None
        rule = replace(rule, warmup=replace(rule.warmup, timeout_seconds=0.4))
        template = build_template(readiness=rule)
        runtime = _runtime(_engine(addresses=("127.0.0.1",)))
        monkeypatch.setattr(docker_runtime, "time", SimpleNamespace(monotonic=lambda: elapsed))
        send = runtime._run_warmup

        def observed(address: str, *args: Any, **kwargs: Any) -> bool:
            deadlines.append(kwargs["deadline"])
            return send(address, *args, **kwargs)

        monkeypatch.setattr(runtime, "_run_warmup", observed)

        prepared = runtime.prepare(build_request(), template)
        assert prepared.observed_instance_state is ObservedInstanceState.WARMING_UP
        assert deadlines == [pytest.approx(0.4)]

        elapsed = 0.3
        resumed = runtime.inspect(build_request(), template, resumed=True)

        assert resumed is not None
        assert resumed.observed_instance_state is ObservedInstanceState.READY
        assert len(requests) == 2
        # The in-memory anchor keeps the original 0.4 s deadline instead of
        # restarting a fresh window when the later version is observed.
        assert deadlines == [pytest.approx(0.4), pytest.approx(0.4)]


def test_a_new_intent_after_a_restart_anchors_its_own_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    elapsed = 0.0

    with responding("ok") as (server, requests, _):
        body, digest = _warmup_body(tmp_path, b"synthetic-warmup")
        rule = _warmup_rule(server, body, digest, attempts=2)
        assert rule.warmup is not None
        rule = replace(rule, warmup=replace(rule.warmup, timeout_seconds=0.4))
        template = build_template(readiness=rule)
        runtime = _runtime(_engine(addresses=("127.0.0.1",)))
        monkeypatch.setattr(docker_runtime, "time", SimpleNamespace(monotonic=lambda: elapsed))

        first = runtime.prepare(build_request(), template)

        assert first.observed_instance_state is ObservedInstanceState.WARMING_UP
        assert runtime._warmup_deadlines[(SAMPLE_INSTANCE_ID, 1)] == pytest.approx(0.4)
        assert len(requests) == 1

        # A version-1 intent is new work, so the restarted adapter grants it a
        # budget and it can still reach readiness.
        ready = runtime.inspect(build_request(), template)

        assert ready is not None
        assert ready.observed_instance_state is ObservedInstanceState.READY
        assert len(requests) == 2
        assert (SAMPLE_INSTANCE_ID, 1) not in runtime._warmup_deadlines


def test_a_passed_intent_deadline_is_non_ready_with_zero_outbound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    probed: list[str] = []

    def probe(address: tuple[str, int], *, timeout: float) -> AbstractContextManager[None]:
        probed.append(address[0])
        return nullcontext()

    with responding("ok") as (server, requests, _):
        body, digest = _warmup_body(tmp_path, b"synthetic-warmup")
        rule = _warmup_rule(server, body, digest, attempts=1)
        template = build_template(readiness=rule)
        engine = _engine(addresses=("127.0.0.1",))
        runtime = _runtime(engine)
        monkeypatch.setattr(socket, "create_connection", probe)
        expired = build_request(deadline_at=datetime.now(UTC) - timedelta(seconds=1))

        prepared = runtime.prepare(expired, template)

        assert prepared.observed_instance_state is ObservedInstanceState.WARMING_UP
        assert not probed and not requests

        # A new (version-1) intent after a restart still spends no outbound work.
        fresh = _runtime(engine)
        new_intent = fresh.inspect(expired, template)

        assert new_intent is not None
        assert new_intent.observed_instance_state is ObservedInstanceState.WARMING_UP
        assert not probed and not requests

        # Restarting a resumed intent does not launder the expired deadline either.
        restarted = _runtime(engine)
        resumed = restarted.inspect(expired, template, resumed=True)

        assert resumed is not None
        assert resumed.observed_instance_state is ObservedInstanceState.WARMING_UP
        assert not probed and not requests


def test_warmup_budget_is_capped_by_the_intent_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(docker_runtime, "time", SimpleNamespace(monotonic=lambda: 100.0))
    request = build_request(deadline_at=datetime.now(UTC) + timedelta(seconds=1))
    warmup = WarmupRequest(
        path="/synthetic-warmup",
        content_type="application/octet-stream",
        body_path="/synthetic/body",
        body_sha256="sha256:" + "0" * 64,
        attempts=1,
        timeout_seconds=60,
        max_response_bytes=1024,
    )

    deadline = _runtime(_engine())._warmup_budget_deadline(request, warmup)

    assert 100.0 < deadline <= 101.5


def _control_service(
    template: Any, store: InMemoryIntentStore, runtime: Any
) -> NodeRuntimeControlService:
    return NodeRuntimeControlService(
        node_id=SAMPLE_NODE_ID,
        templates=ApprovedTemplateRegistry((template,)),
        store=store,
        runtime=runtime,
        now=lambda: FIXED_NOW,
    )


def test_a_restarted_controller_keeps_an_unresolved_warmup_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    probed: list[str] = []

    def probe(address: tuple[str, int], *, timeout: float) -> AbstractContextManager[None]:
        probed.append(address[0])
        return nullcontext()

    with responding("ok") as (server, requests, _):
        body, digest = _warmup_body(tmp_path, b"synthetic-warmup")
        rule = _warmup_rule(server, body, digest, attempts=2)
        template = build_template(readiness=rule)
        engine = _engine(addresses=("127.0.0.1",))
        monkeypatch.setattr(socket, "create_connection", probe)
        store = InMemoryIntentStore()
        request = build_request()

        first = _control_service(template, store, _runtime(engine))
        submitted = first.submit_intent(request).intent
        assert submitted.observed_instance_state is ObservedInstanceState.WARMING_UP
        assert submitted.version == 2
        assert len(requests) == 1 and probed

        # Restart: the durable intent survives, the adapter's anchors do not.
        probed.clear()
        restarted_runtime = _runtime(engine)
        restarted = _control_service(template, store, restarted_runtime)
        outcomes = restarted.reconcile_instance(SAMPLE_INSTANCE_ID, 1)

        assert len(outcomes) == 1
        folded = outcomes[0].intent
        assert folded.observed_instance_state is ObservedInstanceState.WARMING_UP
        assert folded.memory_hold is MemoryHold.PEAK
        assert folded.version == 3
        assert not probed and len(requests) == 1
        assert restarted_runtime._warmup_progress == {}
        assert restarted_runtime._warmup_deadlines == {}

        restarted.reconcile_instance(SAMPLE_INSTANCE_ID, 1)
        assert not probed and len(requests) == 1
        assert restarted_runtime._warmup_progress == {}
        assert restarted_runtime._warmup_deadlines == {}


def test_a_redirect_is_not_followed_to_another_reachable_endpoint(tmp_path: Path) -> None:
    """A 3xx is non-ready even when its Location would answer 200 if followed."""
    with ExitStack() as stack:
        target, target_requests = stack.enter_context(_replied(200))
        primary, primary_requests = stack.enter_context(
            _replied(302, location=f"http://127.0.0.1:{target.server_port}/")
        )
        body, digest = _warmup_body(tmp_path, b"synthetic-warmup")
        rule = _warmup_rule(primary, body, digest, attempts=1)
        runtime = _runtime(_engine(addresses=("127.0.0.1",)))

        observation = runtime.prepare(build_request(), build_template(readiness=rule))

        assert observation.observed_instance_state is ObservedInstanceState.WARMING_UP
        assert observation.error_code is None
        assert primary_requests == [("POST", "/v1/audio/transcriptions")]
        # Following the redirect would reach a healthy endpoint and fake readiness.
        assert not target_requests
        assert _only_progress(runtime).completed == 0


def test_an_exhausted_warmup_reports_a_reason_and_keeps_the_peak_hold(tmp_path: Path) -> None:
    """The deadline is observable, but it is neither a failure nor a release."""
    with responding("stall") as (server, requests, _):
        body, digest = _warmup_body(tmp_path, b"synthetic-warmup")
        rule = _warmup_rule(server, body, digest, attempts=2)
        assert rule.warmup is not None
        rule = replace(rule, warmup=replace(rule.warmup, timeout_seconds=0.3))
        template = build_template(readiness=rule)
        store = InMemoryIntentStore()
        service = _control_service(template, store, _runtime(_engine(addresses=("127.0.0.1",))))

        submitted = service.submit_intent(build_request()).intent

        assert submitted.observed_instance_state is ObservedInstanceState.WARMING_UP
        assert submitted.intent_state is RuntimeIntentState.RECONCILING
        assert submitted.memory_hold is MemoryHold.PEAK
        assert submitted.error_code is RuntimeIntentErrorCode.WARMUP_DEADLINE_EXCEEDED
        assert len(requests) == 1

        replayed = service.submit_intent(build_request())

        assert replayed.idempotent_replay
        assert replayed.intent.error_code is RuntimeIntentErrorCode.WARMUP_DEADLINE_EXCEEDED
        assert len(requests) == 1

        folded = service.reconcile_instance(SAMPLE_INSTANCE_ID, 1)

        assert len(folded) == 1
        assert folded[0].intent.intent_state is RuntimeIntentState.RECONCILING
        assert folded[0].intent.memory_hold is MemoryHold.PEAK
        assert folded[0].intent.error_code is RuntimeIntentErrorCode.WARMUP_DEADLINE_EXCEEDED
        assert len(requests) == 1


def test_a_restart_keeps_the_persisted_deadline_reason(tmp_path: Path) -> None:
    """A restarted adapter has no anchor, but it must not erase the reason."""
    with responding("stall") as (server, requests, _):
        body, digest = _warmup_body(tmp_path, b"synthetic-warmup")
        rule = _warmup_rule(server, body, digest, attempts=2)
        assert rule.warmup is not None
        rule = replace(rule, warmup=replace(rule.warmup, timeout_seconds=0.3))
        template = build_template(readiness=rule)
        engine = _engine(addresses=("127.0.0.1",))
        store = InMemoryIntentStore()

        exhausted = _control_service(template, store, _runtime(engine))
        submitted = exhausted.submit_intent(build_request()).intent
        assert submitted.error_code is RuntimeIntentErrorCode.WARMUP_DEADLINE_EXCEEDED
        assert len(requests) == 1

        # The durable intent survives the restart; the in-memory anchor does not.
        restarted_runtime = _runtime(engine)
        restarted = _control_service(template, store, restarted_runtime)
        outcomes = restarted.reconcile_instance(SAMPLE_INSTANCE_ID, 1)

        assert len(outcomes) == 1
        folded = outcomes[0].intent
        assert folded.observed_instance_state is ObservedInstanceState.WARMING_UP
        assert folded.intent_state is RuntimeIntentState.RECONCILING
        assert folded.memory_hold is MemoryHold.PEAK
        assert folded.error_code is RuntimeIntentErrorCode.WARMUP_DEADLINE_EXCEEDED
        assert len(requests) == 1
        assert restarted_runtime._warmup_progress == {}
        assert restarted_runtime._warmup_deadlines == {}


def test_a_completed_warmup_reports_no_reason(tmp_path: Path) -> None:
    with responding("ok") as (server, requests, _):
        body, digest = _warmup_body(tmp_path, b"synthetic-warmup")
        rule = _warmup_rule(server, body, digest, attempts=1)
        runtime = _runtime(_engine(addresses=("127.0.0.1",)))

        observation = runtime.prepare(build_request(), build_template(readiness=rule))

        assert observation.observed_instance_state is ObservedInstanceState.READY
        assert observation.error_code is None
        assert len(requests) == 1
