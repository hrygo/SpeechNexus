"""Real uvicorn and httpx evidence for the public Job event stream.

An ASGI transport buffers the complete response, so ordering across persisted
pages, idle heartbeats and a client that closes mid-stream are only observable
through a real socket. Each test serves the composed app with uvicorn on an
ephemeral loopback port and talks to it with an ordinary HTTP client.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import logging
import socket
import threading
import time
from collections.abc import AsyncIterator, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from typing import Any, cast

import httpx
import pytest
import uvicorn
from tests.integration.test_public_jobs_http import PREFIX, JobSandbox, _settings_for
from tests.integration.test_public_jobs_http import jobs_db as jobs_db

from speechnexus_backend.app import create_app
from speechnexus_backend.speech.application.public_jobs import PublicJobs
from speechnexus_backend.speech.domain.public_jobs import JobEventFact
from speechnexus_backend.speech.interfaces.http.jobs import _EVENT_BATCH

pytestmark = pytest.mark.integration
EVENTS = PREFIX + "/{job_id}/events"
# Short enough to observe in CI, long enough that scheduling noise is harmless.
HEARTBEAT_SECONDS = 0.2
SEND_TIMEOUT_SECONDS = 5.0
# One initial event plus this many persisted rows always crosses one page.
EXTRA_EVENTS = _EVENT_BATCH + 49
TOTAL_EVENTS = EXTRA_EVENTS + 1


def _app(
    db: JobSandbox,
    jobs: PublicJobs | None = None,
    *,
    heartbeat_seconds: float = HEARTBEAT_SECONDS,
    send_timeout_seconds: float = SEND_TIMEOUT_SECONDS,
) -> Any:
    settings = _settings_for(db).model_copy(
        update={
            "job_event_heartbeat_seconds": heartbeat_seconds,
            "job_event_send_timeout_seconds": send_timeout_seconds,
        }
    )
    return create_app(settings=settings, jobs=jobs if jobs is not None else db.jobs)


@contextmanager
def _serve(app: Any) -> Iterator[int]:
    """Serve one app on an ephemeral loopback port and stop it afterwards."""
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(16)
    port = int(sock.getsockname()[1])
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            log_level="error",
            access_log=False,
            proxy_headers=False,
            server_header=False,
            lifespan="off",
        )
    )
    with ThreadPoolExecutor(max_workers=1) as workers:
        serving = workers.submit(server.run, sockets=[sock])
        try:
            deadline = time.monotonic() + 10
            while not server.started and not serving.done() and time.monotonic() < deadline:
                time.sleep(0.01)
            assert server.started, "uvicorn did not start on the loopback socket"
            yield port
        finally:
            server.should_exit = True
            serving.result(timeout=20)
            sock.close()


def _client(port: int) -> httpx.AsyncClient:
    return httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", timeout=15, trust_env=False)


def _body() -> dict[str, Any]:
    return {
        "kind": "tts",
        "model": "tts-quality",
        "input": {"text": "synthetic socket stream"},
        "voice": {"id": "voice-vivian", "revision": "2"},
        "client_reference": "synthetic-socket-stream",
    }


async def _create(client: httpx.AsyncClient) -> dict[str, Any]:
    response = await client.post(PREFIX, json=_body())
    assert response.status_code == 202, response.text
    return cast(dict[str, Any], response.json())


def _frames(body: bytes) -> list[dict[str, Any]]:
    frames: list[dict[str, Any]] = []
    for block in body.split(b"\n\n"):
        if not block.strip() or block.lstrip().startswith(b":"):
            continue
        identifier: str | None = None
        data: list[bytes] = []
        for line in block.splitlines():
            if line.startswith(b"id: "):
                identifier = line[4:].decode()
            elif line.startswith(b"data: "):
                data.append(line[6:])
        assert identifier is not None and data, block
        event = json.loads(b"\n".join(data))
        assert event["id"] == identifier
        frames.append(event)
    return frames


async def _read_ids(lines: AsyncIterator[str], count: int) -> list[str]:
    """Collect the ids of the next count frames, ignoring comment heartbeats."""
    identifiers: list[str] = []
    frames = 0
    while frames < count:
        line = await anext(lines)
        if line.startswith("id: "):
            identifiers.append(line[4:])
        elif line.startswith("data: "):
            frames += 1
    return identifiers


def _seed_paged_history(db: JobSandbox, job_id: str, count: int = EXTRA_EVENTS) -> None:
    """Persist a long history so the reader must cross the page boundary.

    The rows stand in for a Job that advanced many times. The writer is not
    under test here: every assertion compares the stream with these rows.
    """
    db.admin.execute(
        "INSERT INTO speech_job_events (job_id, event_seq, state, occurred_at_us) "
        "SELECT j.job_id, seq, 'running', j.created_at_us + seq "
        "FROM generate_series(1, %s) AS seq, speech_jobs j WHERE j.job_id = %s",
        (count, job_id),
    )


async def test_paged_events_replay_in_order_over_a_real_socket(
    jobs_db: JobSandbox,
) -> None:
    app = _app(jobs_db)
    with _serve(app) as port:
        async with _client(port) as client:
            job = await _create(client)
            job_id = job["id"]
            _seed_paged_history(jobs_db, job_id)
            persisted = jobs_db.query(
                "SELECT count(*) AS n FROM speech_job_events WHERE job_id = %s", (job_id,)
            )[0]["n"]
            assert persisted == TOTAL_EVENTS

            async with client.stream("GET", EVENTS.format(job_id=job_id)) as response:
                assert response.status_code == 200, await response.aread()
                assert response.headers["content-type"].startswith("text/event-stream")
                async with asyncio.timeout(30):
                    identifiers = await _read_ids(response.aiter_lines(), TOTAL_EVENTS)
            # Every persisted row arrives once, in sequence order, across pages.
            assert identifiers == [str(number) for number in range(1, TOTAL_EVENTS + 1)]

            # A resume cursor inside the first page continues after it, once.
            async with client.stream(
                "GET",
                EVENTS.format(job_id=job_id),
                headers={"Last-Event-ID": str(_EVENT_BATCH)},
            ) as response:
                assert response.status_code == 200
                async with asyncio.timeout(30):
                    resumed = await _read_ids(response.aiter_lines(), TOTAL_EVENTS - _EVENT_BATCH)
            assert resumed == [str(number) for number in range(_EVENT_BATCH + 1, TOTAL_EVENTS + 1)]

            # A cursor in the middle of a page resumes after that exact row,
            # not after the page that contains it.
            for cursor in (42, _EVENT_BATCH + 37):
                async with client.stream(
                    "GET",
                    EVENTS.format(job_id=job_id),
                    headers={"Last-Event-ID": str(cursor)},
                ) as response:
                    assert response.status_code == 200
                    async with asyncio.timeout(30):
                        resumed = await _read_ids(response.aiter_lines(), TOTAL_EVENTS - cursor)
                assert resumed == [str(number) for number in range(cursor + 1, TOTAL_EVENTS + 1)]


async def test_terminal_stream_closes_and_resumes_without_repeating(
    jobs_db: JobSandbox,
) -> None:
    app = _app(jobs_db)
    with _serve(app) as port:
        async with _client(port) as client:
            job = await _create(client)
            job_id = job["id"]
            jobs_db.jobs.advance(job_id, "running")
            jobs_db.jobs.advance(job_id, "failed", error_code="engine_failure")

            async with client.stream("GET", EVENTS.format(job_id=job_id)) as response:
                assert response.status_code == 200
                body = await asyncio.wait_for(response.aread(), 30)
            events = _frames(body)
            assert [event["id"] for event in events] == ["1", "2", "3"]
            assert [event["type"] for event in events] == [
                "job.queued",
                "job.started",
                "job.failed",
            ]
            assert sum(event["type"] == "job.failed" for event in events) == 1

            async with client.stream(
                "GET", EVENTS.format(job_id=job_id), headers={"Last-Event-ID": "2"}
            ) as response:
                assert response.status_code == 200
                resumed = _frames(await asyncio.wait_for(response.aread(), 30))
            assert [event["id"] for event in resumed] == ["3"]

            # The consumed terminal fact is not repeated by a reconnect.
            async with client.stream(
                "GET", EVENTS.format(job_id=job_id), headers={"Last-Event-ID": "3"}
            ) as response:
                assert response.status_code == 200
                assert _frames(await asyncio.wait_for(response.aread(), 30)) == []


async def test_idle_stream_emits_keepalive_comments_over_a_real_socket(
    jobs_db: JobSandbox,
) -> None:
    app = _app(jobs_db)
    with _serve(app) as port:
        async with _client(port) as client:
            job = await _create(client)
            job_id = job["id"]
            async with client.stream("GET", EVENTS.format(job_id=job_id)) as response:
                assert response.status_code == 200
                lines = response.aiter_lines()
                async with asyncio.timeout(15):
                    assert await anext(lines) == "id: 1"
                    assert (await anext(lines)).startswith("data: ")
                    assert await anext(lines) == ""
                    heartbeats = 0
                    while heartbeats < 2:
                        line = await anext(lines)
                        if line == ": keepalive":
                            heartbeats += 1
                        else:
                            assert line == "", line
                    # A heartbeat never consumes a persisted sequence id.
                    jobs_db.jobs.advance(job_id, "running")
                    while True:
                        line = await anext(lines)
                        if line.startswith("id: "):
                            assert line == "id: 2", line
                            break
                        assert line in ("", ": keepalive"), line
                    assert (await anext(lines)).startswith("data: ")


async def test_heartbeat_cadence_does_not_drift_a_poll_interval(
    jobs_db: JobSandbox,
) -> None:
    """0.52 s beats stay near 0.52 s instead of waiting a whole 0.5 s poll again."""
    app = _app(jobs_db, heartbeat_seconds=0.52)
    beats: list[float] = []
    with _serve(app) as port:
        async with _client(port) as client:
            job = await _create(client)
            job_id = job["id"]
            async with client.stream("GET", EVENTS.format(job_id=job_id)) as response:
                lines = response.aiter_lines()
                async with asyncio.timeout(30):
                    while len(beats) < 4:
                        if await anext(lines) == ": keepalive":
                            beats.append(time.monotonic())
    gaps = [later - earlier for earlier, later in itertools.pairwise(beats)]
    # Sleeping a whole minimum interval overshot every beat to about 1.0 s; a
    # maximum gap keeps a single regression from hiding behind the median.
    assert max(gaps) < 0.85, gaps


async def test_heartbeat_then_disconnect_resumes_after_the_last_event(
    jobs_db: JobSandbox,
) -> None:
    app = _app(jobs_db)
    with _serve(app) as port:
        async with _client(port) as client:
            job = await _create(client)
            job_id = job["id"]
            async with client.stream("GET", EVENTS.format(job_id=job_id)) as response:
                lines = response.aiter_lines()
                async with asyncio.timeout(15):
                    assert await anext(lines) == "id: 1"
                    assert (await anext(lines)).startswith("data: ")
                    while True:
                        line = await anext(lines)
                        if line == ": keepalive":
                            break
                        assert line == "", line
            # An event committed after the disconnect is delivered on resume,
            # and the event already consumed is not repeated.
            jobs_db.jobs.advance(job_id, "running")
            async with client.stream(
                "GET", EVENTS.format(job_id=job_id), headers={"Last-Event-ID": "1"}
            ) as response:
                assert response.status_code == 200
                lines = response.aiter_lines()
                async with asyncio.timeout(15):
                    while True:
                        line = await anext(lines)
                        if line.startswith("id: "):
                            assert line == "id: 2", line
                            break
                        assert line in ("", ": keepalive"), line
                    assert (await anext(lines)).startswith("data: ")


class _ObservedJobs:
    """Delegate to the real service and count persisted event reads."""

    def __init__(self, inner: PublicJobs) -> None:
        self._inner = inner
        self.reads = 0
        self._lock = threading.Lock()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def events(self, job_id: str, *, after_seq: int, limit: int) -> tuple[JobEventFact, ...]:
        with self._lock:
            self.reads += 1
        return self._inner.events(job_id, after_seq=after_seq, limit=limit)


async def _polling_stops(observed: _ObservedJobs, *, limit: float) -> bool:
    """True once no further event read happened for two poll intervals."""
    deadline = time.monotonic() + limit
    last, idle_since = observed.reads, time.monotonic()
    while time.monotonic() < deadline:
        await asyncio.sleep(0.25)
        if observed.reads == last:
            if time.monotonic() - idle_since >= 1.5:
                return True
        else:
            last, idle_since = observed.reads, time.monotonic()
    return False


async def test_client_disconnect_stops_the_server_side_stream(
    jobs_db: JobSandbox,
) -> None:
    observed = _ObservedJobs(jobs_db.jobs)
    app = _app(jobs_db, jobs=cast(PublicJobs, observed))
    with _serve(app) as port:
        async with _client(port) as client:
            job = await _create(client)
            job_id = job["id"]
            async with client.stream("GET", EVENTS.format(job_id=job_id)) as response:
                assert response.status_code == 200
                lines = response.aiter_lines()
                async with asyncio.timeout(15):
                    assert await anext(lines) == "id: 1"
                # The stream is live while it is open, so the stop below is
                # caused by the disconnect and not by an already ended stream.
                before = observed.reads
                await asyncio.sleep(1.0)
                assert observed.reads > before
            # Closing the response must cancel the request task, not leave a
            # generator polling the database for a client that is gone.
            assert await _polling_stops(observed, limit=15), (
                "the event stream kept reading persisted events after the client closed"
            )
            assert observed.reads >= 1


class _TransportSpy:
    """Serve the app while exposing the transport of each uvicorn request."""

    def __init__(self, app: Any) -> None:
        self._app = app
        self.transports: list[Any] = []

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] == "http":
            self.transports.append(getattr(getattr(send, "__self__", None), "transport", None))
        await self._app(scope, receive, send)


def _raw_event_request(port: int, job_id: str, *, receive_buffer: int = 4096) -> socket.socket:
    """Open a real socket that sends the request and never reads the body."""
    sock = socket.socket()
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, receive_buffer)
    sock.connect(("127.0.0.1", port))
    sock.sendall(
        f"GET {EVENTS.format(job_id=job_id)} HTTP/1.1\r\n"
        "Host: 127.0.0.1\r\nAccept: text/event-stream\r\n\r\n".encode()
    )
    return sock


async def _spy_transport(spy: _TransportSpy, *, after: int, limit: float) -> Any:
    deadline = time.monotonic() + limit
    while time.monotonic() < deadline:
        if len(spy.transports) > after:
            return spy.transports[after]
        await asyncio.sleep(0.05)
    return None


class _RecordHandler(logging.Handler):
    """Collect the structured warnings the stream emits."""

    def __init__(self) -> None:
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


def _socket_fileno(sock: Any) -> Any:
    if sock is None:
        return None
    try:
        return sock.fileno()
    except Exception as error:  # diagnostics only
        return f"{type(error).__name__}: {error}"


def _socket_released(transport: Any) -> bool:
    if transport is None:
        return False
    sock = transport.get_extra_info("socket")
    return sock is None or _socket_fileno(sock) == -1


async def _released_within(transport: Any, *, limit: float) -> bool:
    deadline = time.monotonic() + limit
    while time.monotonic() < deadline:
        if _socket_released(transport):
            return True
        await asyncio.sleep(0.05)
    return _socket_released(transport)


def _transport_state(transport: Any) -> str:
    """Describe the transport well enough to tell abort, close and stall apart."""
    if transport is None:
        return "transport=None"
    sock = transport.get_extra_info("socket")
    sndbuf: Any = None
    if sock is not None:
        try:
            sndbuf = sock.getsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF)
        except Exception as error:  # diagnostics only
            sndbuf = f"{type(error).__name__}: {error}"
    return (
        f"transport={type(transport).__module__}.{type(transport).__name__} "
        f"closing={transport.is_closing()} sndbuf={sndbuf} socket={sock!r} "
        f"fileno={_socket_fileno(sock)}"
    )


async def _backpressure_record(
    handler: _RecordHandler, *, limit: float
) -> logging.LogRecord | None:
    deadline = time.monotonic() + limit
    while time.monotonic() < deadline:
        for record in handler.records:
            if record.getMessage() == "job_event_stream_backpressure":
                return record
        await asyncio.sleep(0.05)
    return None


async def test_a_client_that_never_reads_loses_its_socket(
    jobs_db: JobSandbox,
) -> None:
    observed = _ObservedJobs(jobs_db.jobs)
    app = _app(jobs_db, jobs=cast(PublicJobs, observed), send_timeout_seconds=0.5)
    spy = _TransportSpy(app)
    handler = _RecordHandler()
    logger = logging.getLogger("speechnexus_backend.speech.interfaces.http.jobs")
    logger.addHandler(handler)
    try:
        with _serve(spy) as port:
            async with _client(port) as client:
                job = await _create(client)
                job_id = job["id"]
            # Determinism comes from this history, not from socket options:
            # 120000 frames of about 190 B are about 22 MB, well past the 4 MB
            # default Linux send buffer (macOS autotunes to roughly 150 KB), so
            # the server blocks even on a host that ignores socket tuning.
            _seed_paged_history(jobs_db, job_id, count=120000)
            known = len(spy.transports)
            raw = _raw_event_request(port, job_id)
            try:
                transport = await _spy_transport(spy, after=known, limit=10)
                assert transport is not None, "uvicorn request transport was not reachable"
                warning = await _backpressure_record(handler, limit=20)
                assert warning is not None, (
                    "the write timeout never fired for a client that does not read; "
                    f"reads={observed.reads} " + _transport_state(transport)
                )
                assert getattr(warning, "connection_reset", False) is True, (
                    "the cutoff fell back to a graceful close instead of aborting; "
                    + _transport_state(transport)
                )
                started = time.monotonic()
                assert await _released_within(transport, limit=8), (
                    "a stalled event stream kept its socket open after the write timeout: "
                    f"elapsed={time.monotonic() - started:.2f}s reads={observed.reads} "
                    + _transport_state(transport)
                )
                assert await _polling_stops(observed, limit=15), (
                    "the stalled event stream kept reading persisted events"
                )
            finally:
                raw.close()
    finally:
        logger.removeHandler(handler)
