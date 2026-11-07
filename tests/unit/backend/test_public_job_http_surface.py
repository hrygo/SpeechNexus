"""HTTP surface of retry and the event stream, driven by an in-memory service.

These assertions are about the boundary contract: status codes, error codes, SSE
framing and resume translation. Durable behaviour lives in the PostgreSQL tests.
"""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Any, cast

import pytest
from httpx import ASGITransport, AsyncClient
from speechnexus_contracts import parse

from speechnexus_backend.app import create_app
from speechnexus_backend.speech.application.public_jobs import (
    AssetNotReadyError,
    DisabledWorkspaceError,
    IdempotencyConflictError,
    JobCleanupPendingError,
    JobOutcomeUnknownError,
    JobRejectedError,
    JobRetryNotAllowedError,
    JobRetryUnavailableError,
    ModelBindingError,
    ModelNotPublishedError,
    ProfileNotPublishedError,
    PronunciationNotFoundError,
    UnknownWorkspaceError,
    VoiceNotPublishedError,
)
from speechnexus_backend.speech.domain.public_jobs import (
    JobArtifactFact,
    JobEventFact,
    PublicJob,
    PublicJobNotFoundError,
    PublicJobUnavailableError,
)
from speechnexus_backend.speech.interfaces.http import jobs as http
from tests.unit.backend.test_health import _settings
from tests.unit.backend.test_public_jobs import NOW, _body, _service

RETRY = "/v1/nexus/jobs/{job_id}/retries"
EVENTS = "/v1/nexus/jobs/{job_id}/events"
DIGEST = "sha256:" + "e" * 64


def _sample_job() -> PublicJob:
    service, _, _ = _service()
    return service.submit(
        parse("JobCreateRequest", _body()), request_id="req-http-1", idempotency_key=None
    )


class FakeJobs:
    """Only the reads the two routes need; the real service owns every write."""

    def __init__(
        self,
        job: PublicJob,
        *,
        page: tuple[JobEventFact, ...] = (),
        artifacts: tuple[JobArtifactFact, ...] = (),
        child: PublicJob | None = None,
        failure: Exception | None = None,
        read_failure: Exception | None = None,
    ) -> None:
        self.job = job
        self.page = page
        self.artifact_facts = artifacts
        self.child = child if child is not None else job
        self.failure = failure
        self.read_failure = read_failure
        self.calls: list[tuple[str, Any]] = []

    def get(self, job_id: str) -> PublicJob:
        self.calls.append(("get", job_id))
        if self.read_failure is not None:
            raise self.read_failure
        if job_id != self.job.job_id:
            raise PublicJobNotFoundError
        return self.job

    def events(self, job_id: str, *, after_seq: int, limit: int) -> tuple[JobEventFact, ...]:
        self.calls.append(("events", after_seq))
        if self.read_failure is not None:
            raise self.read_failure
        return tuple(event for event in self.page if event.event_seq > after_seq)[:limit]

    def artifacts(self, job: PublicJob) -> tuple[JobArtifactFact, ...]:
        self.calls.append(("artifacts", job.state))
        return self.artifact_facts if job.state == "succeeded" else ()

    def retry(self, job_id: str, *, request_id: str, idempotency_key: str | None) -> PublicJob:
        self.calls.append(("retry", (job_id, request_id, idempotency_key)))
        if self.failure is not None:
            raise self.failure
        return self.child


def _app(fake: FakeJobs) -> Any:
    # The routes only use the read half of the service; the domain owns the rest.
    return create_app(settings=_settings(), jobs=cast(Any, fake))


def _client(app: Any) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


def _frames(body: str) -> list[dict[str, Any]]:
    frames: list[dict[str, Any]] = []
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
        frames.append(event)
    return frames


async def test_retry_returns_202_with_the_new_job_and_no_lineage_field() -> None:
    parent = _sample_job()
    child = replace(parent, job_id="job-00000000000000000000000000000002")
    fake = FakeJobs(parent, child=child)
    async with _client(_app(fake)) as client:
        response = await client.post(
            RETRY.format(job_id=parent.job_id), headers={"Idempotency-Key": "synthetic-http-key"}
        )
    assert response.status_code == 202, response.text
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    assert body["id"] == child.job_id and body["status"] == "queued"
    assert "retry_of_job_id" not in body
    recorded = next(call for call in fake.calls if call[0] == "retry")
    assert recorded[1][0] == parent.job_id and recorded[1][2] == "synthetic-http-key"
    assert recorded[1][1]  # The request-scoped identity, never the Job submission id.


@pytest.mark.parametrize(
    ("error", "status", "code"),
    [
        (JobCleanupPendingError(), 409, "execution_cleanup_pending"),
        (JobRetryNotAllowedError(), 409, "job_retry_not_allowed"),
        (JobRetryUnavailableError(), 409, "job_retry_unavailable"),
        (IdempotencyConflictError(), 409, "idempotency_conflict"),
        (JobOutcomeUnknownError(), 503, "job_outcome_unknown"),
        (UnknownWorkspaceError(), 422, "workspace_not_found"),
        (DisabledWorkspaceError(), 422, "workspace_disabled"),
        (ModelNotPublishedError(), 422, "model_not_published"),
        (ModelBindingError("model_binding_retired"), 422, "model_binding_retired"),
        (VoiceNotPublishedError(), 422, "voice_not_published"),
        (ProfileNotPublishedError(), 422, "profile_not_published"),
        (PronunciationNotFoundError(), 422, "pronunciation_not_found"),
        (AssetNotReadyError(), 422, "asset_not_ready"),
        (JobRejectedError("facts_changed"), 503, "job_unavailable"),
        (PublicJobNotFoundError(), 404, "job_not_found"),
        (PublicJobUnavailableError(), 503, "job_unavailable"),
        (ValueError("synthetic"), 404, "job_not_found"),
    ],
)
async def test_retry_maps_every_domain_failure_to_its_declared_error(
    error: Exception, status: int, code: str
) -> None:
    parent = _sample_job()
    async with _client(_app(FakeJobs(parent, failure=error))) as client:
        response = await client.post(RETRY.format(job_id=parent.job_id))
    assert response.status_code == status, response.text
    assert response.json()["error"]["code"] == code
    # Only an unknown execution outcome stays retryable; an unavailable read may
    # describe a result that is permanently gone.
    assert response.json()["error"]["retryable"] is (code == "job_outcome_unknown")


async def test_retry_rejects_a_malformed_idempotency_key_before_reading_the_job() -> None:
    parent = _sample_job()
    fake = FakeJobs(parent)
    async with _client(_app(fake)) as client:
        response = await client.post(
            RETRY.format(job_id=parent.job_id), headers={"Idempotency-Key": "short"}
        )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_job_request"
    assert fake.calls == []


async def test_event_stream_orders_frames_and_resumes_strictly_after_the_cursor() -> None:
    job = _sample_job()
    failed = replace(job, state="failed", error_code="engine_failure")
    page = (
        JobEventFact(0, "pending", NOW),
        JobEventFact(1, "running", NOW + 1),
        JobEventFact(2, "failed", NOW + 2),
    )
    async with _client(_app(FakeJobs(failed, page=page))) as client:
        first = await client.get(EVENTS.format(job_id=job.job_id))
        assert first.status_code == 200, first.text
        assert first.headers["content-type"].startswith("text/event-stream")
        assert first.headers["cache-control"] == "no-cache"
        events = _frames(first.text)
        assert [item["id"] for item in events] == ["1", "2", "3"]
        assert [item["type"] for item in events] == [
            "job.queued",
            "job.started",
            "job.failed",
        ]
        assert events[-1]["data"]["cleanup_state"] == "not_required"

        resume = await client.get(EVENTS.format(job_id=job.job_id), headers={"Last-Event-ID": "2"})
        assert [item["id"] for item in _frames(resume.text)] == ["3"]

        settled = await client.get(EVENTS.format(job_id=job.job_id), headers={"Last-Event-ID": "3"})
        assert _frames(settled.text) == []


@pytest.mark.parametrize("header", ["0", "01", "-1", "abc", "1.0", "+2"])
async def test_event_stream_rejects_a_malformed_last_event_id(header: str) -> None:
    job = _sample_job()
    async with _client(_app(FakeJobs(job))) as client:
        response = await client.get(
            EVENTS.format(job_id=job.job_id), headers={"Last-Event-ID": header}
        )
    assert response.status_code == 400, response.text
    assert response.json()["error"]["code"] == "invalid_job_request"


async def test_event_stream_rejects_a_cursor_past_the_persisted_sequence_bound() -> None:
    job = replace(_sample_job(), state="failed", error_code="engine_failure")
    async with _client(_app(FakeJobs(job))) as client:
        beyond = await client.get(
            EVENTS.format(job_id=job.job_id), headers={"Last-Event-ID": "9223372036854775808"}
        )
        # The largest reachable cursor is the largest sequence plus one.
        edge = await client.get(
            EVENTS.format(job_id=job.job_id), headers={"Last-Event-ID": "9223372036854775807"}
        )
    assert beyond.status_code == 400
    assert beyond.json()["error"]["code"] == "invalid_job_request"
    assert edge.status_code == 200 and _frames(edge.text) == []


async def test_event_stream_reports_missing_and_unavailable_storage() -> None:
    job = _sample_job()
    async with _client(_app(FakeJobs(job, read_failure=PublicJobNotFoundError()))) as client:
        missing = await client.get(EVENTS.format(job_id="job-00000000000000000000000000000009"))
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "job_not_found"

    async with _client(_app(FakeJobs(job, read_failure=PublicJobUnavailableError()))) as client:
        unavailable = await client.get(EVENTS.format(job_id=job.job_id))
    assert unavailable.status_code == 503
    assert unavailable.json()["error"]["code"] == "job_unavailable"


async def test_stream_emits_its_heartbeat_without_inventing_an_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job = _sample_job()
    failed = replace(job, state="failed", error_code="engine_failure")
    fake = FakeJobs(job)
    reads = {"count": 0}

    original_get = fake.get

    def get_then_finish(job_id: str) -> PublicJob:
        reads["count"] += 1
        # The stream also reads the snapshot once to validate the response, so
        # the terminal fact must be delayed far enough to observe an idle beat.
        fake.job = failed if reads["count"] > 4 else job
        return original_get(job_id)

    monkeypatch.setattr(fake, "get", get_then_finish)
    # The heartbeat interval is process configuration now, so the cadence is
    # injected through Settings instead of the module default.
    settings = _settings().model_copy(update={"job_event_heartbeat_seconds": 0.005})
    monkeypatch.setattr(http, "_EVENT_POLL_SECONDS", 0.01)
    app = create_app(settings=settings, jobs=cast(Any, fake))
    async with _client(app) as client:
        response = await client.get(EVENTS.format(job_id=job.job_id))
    assert response.status_code == 200, response.text
    assert ": keepalive" in response.text
    assert _frames(response.text) == []


def test_terminal_projection_carries_the_committed_artifact_and_result() -> None:
    job = _sample_job()
    artifact = JobArtifactFact(
        artifact_id="artifact-unit-result",
        kind="audio",
        media_type="audio/wav",
        bytes=2048,
        sha256=DIGEST,
        receipt_id="receipt-unit-result",
        expires_at_us=NOW + 1_000,
    )
    succeeded = replace(
        job,
        state="succeeded",
        result_json=json.dumps(
            {
                "kind": job.kind,
                "audio_artifact_id": artifact.artifact_id,
                "receipt_id": artifact.receipt_id,
            }
        ),
    )
    event = http.public_event(JobEventFact(2, "succeeded", NOW + 2), succeeded, (artifact,))
    assert event["type"] == "job.succeeded"
    assert event["data"]["artifacts"][0]["artifact_id"] == artifact.artifact_id
    assert event["data"]["artifacts"][0]["expires_at"].endswith("Z")
    assert event["data"]["result"]["receipt_id"] == artifact.receipt_id
    projected = http.public_job(succeeded, (artifact,))
    assert projected["artifacts"][0]["kind"] == "audio"
    assert projected["artifacts"][0]["receipt_id"] == artifact.receipt_id
