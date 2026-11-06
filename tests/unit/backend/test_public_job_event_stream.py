"""Public Job event projection, the live tail loop and the retry boundary.

httpx's ASGI transport buffers a whole response, so it cannot observe a stream
that is still growing. The in-process loop is therefore exercised directly
against a scripted service whose facts change between polls; the real socket
behaviour (heartbeat cadence, paging over one connection, mid-stream disconnect)
is covered by tests/integration/test_public_job_event_stream_tcp.py.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncGenerator, AsyncIterator
from dataclasses import replace
from typing import Any, cast

import pytest
from speechnexus_contracts import parse
from starlette.applications import Starlette
from starlette.types import Message

from speechnexus_backend.speech.application.public_jobs import (
    JobRetryNotAllowedError,
    JobRetryUnavailableError,
    PublicJobs,
)
from speechnexus_backend.speech.domain.public_jobs import (
    JobCreateResult,
    JobEventFact,
    JobIntent,
    JobProgressFact,
    JobState,
    JobSubmission,
    NewPublicJob,
    PublicJob,
    PublicJobUnavailableError,
    canonical_digest,
    retryable,
)
from speechnexus_backend.speech.interfaces.http import jobs as http
from speechnexus_backend.speech.interfaces.http.middleware import (
    REQUEST_ABORT_KEY,
    RequestIdMiddleware,
    require_request_abort_capture,
)
from tests.unit.backend.test_public_jobs import NOW, Facts, Store, _body

INTENT = {"kind": "tts", "model": "tts-quality"}
DIGEST = canonical_digest(INTENT)


def _job(
    state: JobState, *, error_code: str | None = None, result_json: str | None = None
) -> PublicJob:
    submission = JobSubmission(
        request_id="req-event-1",
        client_reference="synthetic-event",
        effective_configuration_json=json.dumps(
            {
                "models": {"tts": {"id": "tts-model", "revision": "1"}},
                "policy_revision": "1",
                "catalog_revision": "1",
            }
        ),
        execution_revision=1,
        progress_unit="text_codepoints",
        progress_total=21,
        submitted_at_us=NOW,
    )
    return PublicJob(
        "job-00000000000000000000000000000001",
        "workspace-default",
        "tts",
        "tts-quality",
        state,
        1,
        result_json,
        error_code,
        NOW,
        NOW,
        submission,
    )


class Scripted(Store):
    """Return one persisted page per poll; reads past the script repeat the last."""

    def __init__(self, pages: list[tuple[PublicJob, list[JobEventFact]]]) -> None:
        super().__init__()
        self.pages = pages
        self.polls = 0
        self.intent: JobIntent | None = JobIntent(
            request_digest=DIGEST, document=json.dumps(INTENT)
        )

    def _index(self) -> int:
        return min(max(self.polls - 1, 0), len(self.pages) - 1)

    def get(self, job_id: str) -> PublicJob | None:
        # The stream reads the page first, then the snapshot that projects it.
        return self.pages[self._index()][0]

    def intent_document(self, job_id: str) -> JobIntent | None:
        return self.intent

    def events(self, job_id: str, *, after_seq: int, limit: int) -> tuple[JobEventFact, ...]:
        index = min(self.polls, len(self.pages) - 1)
        self.polls += 1
        return tuple(event for event in self.pages[index][1] if event.event_seq > after_seq)


class Lagging(Store):
    """A committed page that the snapshot only catches up with on the re-read."""

    def __init__(self, *, lag: int | None) -> None:
        super().__init__()
        self.polls = 0
        self.gets = 0
        self.lag = lag
        self.intent: JobIntent | None = JobIntent(
            request_digest=DIGEST, document=json.dumps(INTENT)
        )

    def get(self, job_id: str) -> PublicJob | None:
        self.gets += 1
        stale = self.lag is None or self.gets <= self.lag
        return _job("running") if stale else _job("failed", error_code="engine_failure")

    def intent_document(self, job_id: str) -> JobIntent | None:
        return self.intent

    def events(self, job_id: str, *, after_seq: int, limit: int) -> tuple[JobEventFact, ...]:
        self.polls += 1
        if after_seq >= 2:
            return ()
        return (JobEventFact(2, "failed", NOW + 2),)


class RetryStore(Store):
    """The shared fake plus the two reads a retry needs."""

    def __init__(self) -> None:
        super().__init__()
        self.documents: dict[str, str] = {}

    def create(self, job: NewPublicJob) -> JobCreateResult:
        result = super().create(job)
        self.documents[job.job_id] = job.request_document
        if result.job is None:
            return result
        child = replace(result.job, retry_of_job_id=job.retry_of_job_id)
        self.jobs[job.job_id] = child
        if job.idempotency_key_digest is not None:
            self.keys[job.idempotency_key_digest] = (job.request_digest, job.job_id)
        return replace(result, job=child)

    def intent_document(self, job_id: str) -> JobIntent | None:
        document = self.documents.get(job_id)
        if document is None:
            return None
        return JobIntent(canonical_digest(json.loads(document)), document)

    def events(self, job_id: str, *, after_seq: int, limit: int) -> tuple[JobEventFact, ...]:
        return ()


async def _drain(service: Any, job_id: str, after_seq: int = -1) -> list[dict[str, Any]]:
    chunks = [
        chunk async for chunk in http._event_stream(service, job_id, after_seq, poll_seconds=0)
    ]
    body = b"".join(chunks).decode()
    events: list[dict[str, Any]] = []
    for block in body.split("\n\n"):
        if not block.strip() or block.lstrip().startswith(":"):
            continue
        identifier = None
        data: list[str] = []
        for line in block.splitlines():
            if line.startswith("id: "):
                identifier = line[4:]
            elif line.startswith("data: "):
                data.append(line[6:])
        event = json.loads("\n".join(data))
        assert identifier == event["id"]
        events.append(event)
    return events


async def test_stream_tails_persisted_pages_and_closes_on_the_terminal_fact() -> None:
    running = _job("running")
    failed = _job("failed", error_code="engine_failure")
    store = Scripted(
        [
            (
                running,
                [
                    JobEventFact(0, "pending", NOW),
                    JobEventFact(1, "running", NOW + 1),
                ],
            ),
            (
                failed,
                [
                    JobEventFact(
                        2,
                        "running",
                        NOW + 2,
                        kind="progress",
                        progress=JobProgressFact("audio_ms", 1_200, None),
                    ),
                    JobEventFact(3, "failed", NOW + 3),
                ],
            ),
            (failed, []),
        ]
    )
    service = PublicJobs(Facts(), store, now_us=lambda: NOW)
    events = await _drain(service, failed.job_id)
    assert [event["id"] for event in events] == ["1", "2", "3", "4"]
    assert [event["type"] for event in events] == [
        "job.queued",
        "job.started",
        "job.progress",
        "job.failed",
    ]
    assert [event["data"]["status"] for event in events] == [
        "queued",
        "running",
        "running",
        "failed",
    ]
    assert events[2]["data"]["progress"] == {
        "unit": "audio_ms",
        "completed": 1_200,
        "total": None,
    }
    # Resuming after the terminal fact re-emits nothing and closes immediately.
    assert await _drain(service, failed.job_id, 3) == []


def test_event_projection_never_invents_progress_or_duplicates_a_terminal_state() -> None:
    running = _job("running")
    cases: tuple[tuple[JobState, str], ...] = (
        ("pending", "job.queued"),
        ("running", "job.started"),
        ("cancelling", "job.cancelling"),
        ("cancelled", "job.cancelled"),
        ("expired", "job.expired"),
    )
    for state, expected in cases:
        job = replace(running, state=state)
        event = http.public_event(JobEventFact(3, state, NOW), job)
        assert event["type"] == expected
        assert event["id"] == "4"
        assert "progress" not in json.dumps(event)
    failed = http.public_event(
        JobEventFact(0, "failed", NOW), _job("failed", error_code="synthetic_end")
    )
    assert failed["data"]["error"]["code"] == "synthetic_end"
    # The same persisted row projects identically on every reconnect.
    assert failed == http.public_event(
        JobEventFact(0, "failed", NOW), _job("failed", error_code="synthetic_end")
    )
    with pytest.raises(PublicJobUnavailableError):
        http.public_event(
            JobEventFact(1, "succeeded", NOW),
            _job("succeeded", result_json=json.dumps({"kind": "tts"})),
        )


def test_job_projection_reports_the_committed_progress_fact() -> None:
    running = _job("running")
    submission = replace(running.submission, progress_unit="audio_ms", progress_total=None)
    progressed = replace(
        running,
        submission=submission,
        progress=JobProgressFact("audio_ms", 2_500, None),
    )
    assert http.public_job(progressed)["progress"] == {
        "unit": "audio_ms",
        "completed": 2_500,
        "total": None,
    }
    # No fact keeps the declared unit and a zero completed value.
    assert http.public_job(replace(running, submission=submission))["progress"] == {
        "unit": "audio_ms",
        "completed": 0,
        "total": None,
    }


def test_retry_reads_the_frozen_intent_and_links_the_replacement() -> None:
    facts, store = Facts(), RetryStore()
    service = PublicJobs(facts, store, now_us=lambda: NOW)
    parent = service.submit(
        parse("JobCreateRequest", _body()), request_id="req-parent", idempotency_key="parent-key-01"
    )
    service.advance(parent.job_id, "failed", error_code="engine_failure")
    child = service.retry(parent.job_id, request_id="req-retry-1", idempotency_key="retry-key-0001")
    assert child.job_id != parent.job_id
    assert child.retry_of_job_id == parent.job_id
    assert child.state == "pending"
    original = next(request for request in store.requests if request.job_id == parent.job_id)
    submitted = next(request for request in store.requests if request.job_id == child.job_id)
    assert submitted.retry_of_job_id == parent.job_id
    assert submitted.request_digest == original.request_digest
    assert submitted.request_document == original.request_document

    replay = service.retry(
        parent.job_id, request_id="req-retry-2", idempotency_key="retry-key-0001"
    )
    assert replay.job_id == child.job_id
    assert len(store.requests) == 2  # The parent and one replacement only.


def test_retry_requires_a_frozen_replaceable_state_and_a_readable_intent() -> None:
    facts, store = Facts(), RetryStore()
    service = PublicJobs(facts, store, now_us=lambda: NOW)
    parent = service.submit(
        parse("JobCreateRequest", _body()), request_id="req-pending", idempotency_key=None
    )
    with pytest.raises(JobRetryNotAllowedError):
        service.retry(parent.job_id, request_id="req-retry-3", idempotency_key=None)
    assert len(store.requests) == 1

    service.advance(parent.job_id, "cancelled")
    store.documents.pop(parent.job_id)
    with pytest.raises(JobRetryUnavailableError):
        service.retry(parent.job_id, request_id="req-retry-4", idempotency_key=None)
    assert len(store.requests) == 1

    assert retryable(_job("cancelled"))
    assert not retryable(_job("failed", error_code="cancellation_unconfirmed"))
    assert not retryable(_job("succeeded", result_json=json.dumps({"kind": "tts"})))


async def test_terminal_page_ahead_of_the_snapshot_waits_for_the_re_read() -> None:
    """A committed row must not abort a response that already started."""
    events = await _drain(PublicJobs(Facts(), Lagging(lag=1)), _job("running").job_id)
    assert [event["id"] for event in events] == ["3"]
    assert events[0]["type"] == "job.failed"
    assert events[0]["data"]["status"] == "failed"


async def test_permanently_lagging_snapshot_ends_the_stream_without_a_frame() -> None:
    assert await _drain(PublicJobs(Facts(), Lagging(lag=None)), _job("running").job_id) == []


class Gapped(Store):
    """Emit one page, stay idle for several polls, then append one event."""

    def __init__(self) -> None:
        super().__init__()
        self.job = _job("running")
        self.calls = 0

    def get(self, job_id: str) -> PublicJob | None:
        return self.job

    def events(self, job_id: str, *, after_seq: int, limit: int) -> tuple[JobEventFact, ...]:
        self.calls += 1
        page = [JobEventFact(0, "pending", NOW), JobEventFact(1, "running", NOW + 1)]
        if self.calls >= 4:
            page.append(JobEventFact(2, "running", NOW + 2))
        return tuple(event for event in page if event.event_seq > after_seq)


async def test_keepalive_comments_interleave_without_touching_event_ids() -> None:
    store = Gapped()
    service = PublicJobs(Facts(), store, now_us=lambda: NOW)
    stream = cast(
        AsyncGenerator[bytes],
        http._event_stream(
            service, store.job.job_id, -1, heartbeat_seconds=0.01, poll_seconds=0.01
        ),
    )
    identifiers: list[str] = []
    keepalives: list[bytes] = []
    try:
        async with asyncio.timeout(10):
            while len(identifiers) < 3 or not keepalives:
                chunk = await anext(stream)
                if chunk == http._KEEPALIVE:
                    keepalives.append(chunk)
                    continue
                identifiers.append(chunk.decode().split("id: ", 1)[1].split("\n", 1)[0])
    finally:
        await stream.aclose()
    # The idle comment is never an event: the persisted sequence stays 1, 2, 3.
    assert identifiers == ["1", "2", "3"]
    assert keepalives
    assert all(b"id:" not in chunk and b"data:" not in chunk for chunk in keepalives)


async def test_a_client_that_cannot_drain_is_disconnected_and_logged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    release = asyncio.Event()
    sent: list[Message] = []
    closed = False

    async def send(message: Message) -> None:
        sent.append(message)
        body = cast(dict[str, Any], message)
        if message["type"] == "http.response.body" and body.get("more_body", False):
            await release.wait()

    async def frames() -> AsyncIterator[bytes]:
        nonlocal closed
        try:
            yield b"data: one\n\n"
            yield b"data: two\n\n"
        finally:
            closed = True

    response = http._EventStreamResponse(
        frames(),
        media_type="text/event-stream",
        headers={},
        send_timeout_seconds=0.01,
    )
    with caplog.at_level(logging.WARNING, logger="speechnexus_backend.speech.interfaces.http.jobs"):
        await asyncio.wait_for(response.stream_response(send), 5)
    assert closed, "a stalled frame must close the event source instead of buffering"
    # The first frame was accepted, the second could not be flushed, and no
    # further frame was generated for a client that is not reading.
    assert [message["type"] for message in sent] == [
        "http.response.start",
        "http.response.body",
    ]
    records = [
        record
        for record in caplog.records
        if record.getMessage() == "job_event_stream_backpressure"
    ]
    assert len(records) == 1
    record = cast(Any, records[0])
    assert record.reason == "slow_client"
    assert record.send_timeout_seconds == 0.01
    # No abort hook was supplied here, so the cutoff is the fallback close.
    assert record.connection_reset is False
    # Operational evidence stays identity-free: no job, request or client field.
    assert {"job_id", "request_id", "job", "request", "client"} & set(record.__dict__) == set()


@pytest.mark.parametrize(
    ("now", "beat", "expected"),
    [
        (0.0, 0.0, 0.5),  # nothing consumed yet: the poll boundary is nearer
        (0.3, 0.0, 0.21),  # a slow read leaves only the heartbeat remainder
        (0.51, 0.0, 0.0),  # the heartbeat deadline already passed
        (5.0, 5.0, 0.5),  # a beat that just fired waits for the poll instead
    ],
)
def test_pause_targets_the_earlier_deadline(now: float, beat: float, expected: float) -> None:
    assert http._pause_seconds(now, beat, heartbeat=0.51, poll=0.5) == pytest.approx(expected)


class _Cycle:
    """A stand-in for a server request cycle that owns a transport."""

    def __init__(self) -> None:
        self.transport = _Transport()
        self.disconnected = False

    async def receive(self) -> Message:
        return {"type": "http.disconnect"}


class _Transport:
    def __init__(self) -> None:
        self.aborted = False

    def abort(self) -> None:
        self.aborted = True


async def test_the_request_scope_carries_a_stalled_connection_abort_hook() -> None:
    cycle = _Cycle()
    invoked: list[bool] = []

    async def inner(scope: Any, receive: Any, send: Any) -> None:
        hook = scope.get(REQUEST_ABORT_KEY)
        assert hook is not None
        hook()
        invoked.append(True)

    async def send(message: Message) -> None:
        raise AssertionError(message)

    app = RequestIdMiddleware(inner)
    await app({"type": "http", "headers": [], "state": {}}, cycle.receive, send)
    assert invoked == [True]
    # Without an abort the stalled socket would wait for a peer that never reads.
    assert cycle.transport.aborted
    assert cycle.disconnected


async def test_a_server_without_a_request_cycle_gets_no_abort_hook() -> None:
    async def receive() -> Message:
        return {"type": "http.disconnect"}

    async def inner(scope: Any, receive: Any, send: Any) -> None:
        assert REQUEST_ABORT_KEY not in scope

    async def send(message: Message) -> None:
        raise AssertionError(message)

    await RequestIdMiddleware(inner)({"type": "http", "headers": [], "state": {}}, receive, send)


async def test_a_stalled_write_resets_the_connection(
    caplog: pytest.LogCaptureFixture,
) -> None:
    aborted: list[bool] = []

    async def send(message: Message) -> None:
        body = cast(dict[str, Any], message)
        if message["type"] == "http.response.body" and body.get("more_body", False):
            await asyncio.Event().wait()

    async def frames() -> AsyncIterator[bytes]:
        yield b"data: one\n\n"
        yield b"data: two\n\n"

    response = http._EventStreamResponse(
        frames(),
        media_type="text/event-stream",
        headers={},
        send_timeout_seconds=0.01,
        abort_connection=lambda: aborted.append(True),
    )
    with caplog.at_level(logging.WARNING, logger="speechnexus_backend.speech.interfaces.http.jobs"):
        await asyncio.wait_for(response.stream_response(send), 5)
    # A graceful close would wait for the blocked buffer to drain, which never
    # happens for a peer that stopped reading, so the connection is reset.
    assert aborted == [True]
    record = cast(Any, caplog.records[-1])
    assert record.getMessage() == "job_event_stream_backpressure"
    assert record.connection_reset is True


async def test_the_terminal_frame_is_bounded_too(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The last empty body blocks on the same buffer as a data frame."""
    aborted: list[bool] = []
    sent: list[Message] = []

    async def send(message: Message) -> None:
        sent.append(message)
        body = cast(dict[str, Any], message)
        if message["type"] == "http.response.body" and not body.get("more_body", False):
            # Only the terminal frame stalls: a guard that covers data frames
            # alone leaves this call waiting on a peer that never drains.
            await asyncio.Event().wait()

    async def frames() -> AsyncIterator[bytes]:
        yield b"data: one\n\n"

    response = http._EventStreamResponse(
        frames(),
        media_type="text/event-stream",
        headers={},
        send_timeout_seconds=0.01,
        abort_connection=lambda: aborted.append(True),
    )
    with caplog.at_level(logging.WARNING, logger="speechnexus_backend.speech.interfaces.http.jobs"):
        await asyncio.wait_for(response.stream_response(send), 5)
    # The one data frame drains, then the terminal frame stalls and is cut off.
    assert [message["type"] for message in sent] == [
        "http.response.start",
        "http.response.body",
        "http.response.body",
    ]
    assert not cast(dict[str, Any], sent[-1]).get("more_body", False)
    assert aborted == [True]
    record = cast(Any, caplog.records[-1])
    assert record.getMessage() == "job_event_stream_backpressure"
    assert record.connection_reset is True


async def test_an_abort_failure_is_reported_as_the_graceful_fallback(
    caplog: pytest.LogCaptureFixture,
) -> None:
    def abort() -> None:
        raise RuntimeError("transport already gone")

    async def send(message: Message) -> None:
        body = cast(dict[str, Any], message)
        if message["type"] == "http.response.body" and body.get("more_body", False):
            await asyncio.Event().wait()

    async def frames() -> AsyncIterator[bytes]:
        yield b"data: one\n\n"
        yield b"data: two\n\n"

    response = http._EventStreamResponse(
        frames(),
        media_type="text/event-stream",
        headers={},
        send_timeout_seconds=0.01,
        abort_connection=abort,
    )
    with caplog.at_level(logging.WARNING, logger="speechnexus_backend.speech.interfaces.http.jobs"):
        await asyncio.wait_for(response.stream_response(send), 5)
    # Nothing was reset, so uvicorn keeps owning the close and the record says so.
    record = cast(Any, caplog.records[-1])
    assert record.getMessage() == "job_event_stream_backpressure"
    assert record.connection_reset is False


class _BreakingSource:
    """An event source whose close fails after the first frame."""

    def __init__(self) -> None:
        self._sent = False

    def __aiter__(self) -> _BreakingSource:
        return self

    async def __anext__(self) -> bytes:
        if self._sent:
            raise StopAsyncIteration
        self._sent = True
        return b"data: one\n\n"

    async def aclose(self) -> None:
        raise RuntimeError("source close failed")


async def test_a_failing_source_still_resets_the_connection(
    caplog: pytest.LogCaptureFixture,
) -> None:
    aborted: list[bool] = []

    async def send(message: Message) -> None:
        body = cast(dict[str, Any], message)
        if message["type"] == "http.response.body" and body.get("more_body", False):
            await asyncio.Event().wait()

    response = http._EventStreamResponse(
        _BreakingSource(),
        media_type="text/event-stream",
        headers={},
        send_timeout_seconds=0.01,
        abort_connection=lambda: aborted.append(True),
    )
    with caplog.at_level(logging.WARNING, logger="speechnexus_backend.speech.interfaces.http.jobs"):
        await asyncio.wait_for(response.stream_response(send), 5)
    # The close failed, yet the socket is still cut and the timeout is reported.
    assert aborted == [True]
    record = cast(Any, caplog.records[-1])
    assert record.getMessage() == "job_event_stream_backpressure"
    assert record.connection_reset is True


class _ReceiveWrapper:
    """An ingress middleware that wraps receive, losing the bound server cycle."""

    def __init__(self, app: Any) -> None:
        self._app = app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        async def wrapped() -> Message:
            return cast(Message, await receive())

        await self._app(scope, wrapped, send)


def test_assembly_rejects_a_middleware_outside_the_abort_capture() -> None:
    app = Starlette()
    app.add_middleware(RequestIdMiddleware)
    require_request_abort_capture(app)
    app.add_middleware(_ReceiveWrapper)
    with pytest.raises(RuntimeError, match="RequestIdMiddleware must stay the outermost"):
        require_request_abort_capture(app)


def test_stream_timing_configuration_is_bounded() -> None:
    for heartbeat, send_timeout in ((0, 10), (61, 10), (15, 0), (15, 61)):
        with pytest.raises(ValueError):
            http.create_job_router(
                lambda: None,
                event_heartbeat_seconds=heartbeat,
                event_send_timeout_seconds=send_timeout,
            )
