"""Public Job event projection, the live tail loop and the retry boundary.

httpx's ASGI transport buffers a whole response, so it cannot observe a stream
that is still growing. The loop is therefore exercised directly against a
scripted service whose facts change between polls.
"""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Any

import pytest
from speechnexus_contracts import parse

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
    http._EVENT_POLL_SECONDS = 0
    chunks = [chunk async for chunk in http._event_stream(service, job_id, after_seq)]
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
