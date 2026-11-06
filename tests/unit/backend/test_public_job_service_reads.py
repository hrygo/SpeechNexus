"""Service reads behind the retry route, the event page and result Artifacts."""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Any

import pytest
from speechnexus_contracts import parse

from speechnexus_backend.speech.application.public_jobs import (
    IdempotencyConflictError,
    JobCleanupPendingError,
    PublicJobs,
)
from speechnexus_backend.speech.domain.public_jobs import (
    JobArtifactFact,
    JobEventFact,
    PublicJob,
    PublicJobUnavailableError,
)
from tests.unit.backend.test_public_job_event_stream import RetryStore
from tests.unit.backend.test_public_jobs import NOW, Facts, _body

DIGEST = "sha256:" + "c" * 64


def _submitted() -> PublicJob:
    service, _, _ = _service()
    return service.submit(
        parse("JobCreateRequest", _body()), request_id="req-read-1", idempotency_key=None
    )


def _service() -> tuple[PublicJobs, Facts, Reads]:
    facts, store = Facts(), Reads()
    return PublicJobs(facts, store, now_us=lambda: NOW), facts, store


class Reads(RetryStore):
    """The lineage-aware fake plus the two reads a projection needs."""

    def __init__(self) -> None:
        super().__init__()
        self.page: tuple[JobEventFact, ...] = ()
        self.fact: JobArtifactFact | None = None
        self.calls: list[tuple[str, Any]] = []

    def events(self, job_id: str, *, after_seq: int, limit: int) -> tuple[JobEventFact, ...]:
        self.calls.append(("events", (job_id, after_seq, limit)))
        return self.page

    def artifact(self, workspace_id: str, artifact_id: str) -> JobArtifactFact | None:
        self.calls.append(("artifact", (workspace_id, artifact_id)))
        return self.fact


def _artifact(**changes: Any) -> JobArtifactFact:
    fact = JobArtifactFact(
        artifact_id="artifact-read-1",
        kind="audio",
        media_type="audio/wav",
        bytes=64,
        sha256=DIGEST,
        receipt_id="receipt-read-1",
        expires_at_us=NOW + 100,
    )
    return replace(fact, **changes)


def _succeeded(kind: str, result: dict[str, Any] | None) -> PublicJob:
    job = _submitted()
    return replace(job, state="succeeded", result_json=json.dumps(result))


@pytest.mark.parametrize(
    ("after_seq", "limit"),
    [
        (-2, 10),
        (9_223_372_036_854_775_807, 10),
        (True, 10),
        (-1, 0),
        (-1, 501),
        (-1, True),
    ],
)
def test_event_pages_reject_a_cursor_or_page_outside_the_persisted_bounds(
    after_seq: Any, limit: Any
) -> None:
    service, _, store = _service()
    with pytest.raises(ValueError):
        service.events("job-00000000000000000000000000000001", after_seq=after_seq, limit=limit)
    assert store.calls == []


def test_event_pages_forward_the_bounded_cursor_and_page() -> None:
    service, _, store = _service()
    store.page = (JobEventFact(4, "failed", NOW),)
    events = service.events("job-00000000000000000000000000000001", after_seq=3, limit=500)
    assert [event.event_seq for event in events] == [4]
    assert store.calls == [("events", ("job-00000000000000000000000000000001", 3, 500))]


def test_artifacts_are_projected_only_for_a_committed_audio_result() -> None:
    service, _, store = _service()
    job = _succeeded(
        "tts",
        {"kind": "tts", "audio_artifact_id": "artifact-read-1", "receipt_id": "receipt-read-1"},
    )
    store.fact = _artifact()
    assert service.artifacts(job) == (store.fact,)
    assert service.artifacts(replace(job, state="running", result_json=None)) == ()

    asr = replace(
        _succeeded(
            "asr",
            {
                "kind": "asr",
                "transcript_artifact_id": "artifact-read-1",
                "receipt_id": "receipt-read-1",
            },
        ),
        kind="asr",
        model_id="asr-whisper",
    )
    store.fact = _artifact(kind="transcript")
    assert service.artifacts(asr) == (store.fact,)


@pytest.mark.parametrize(
    "result",
    [
        "not-json",
        {},
        {"kind": "asr"},
        {"kind": "tts", "audio_artifact_id": "artifact-read-1"},
        {"kind": "tts", "audio_artifact_id": "bad id", "receipt_id": "receipt-read-1"},
        {"kind": "tts", "audio_artifact_id": 7, "receipt_id": "receipt-read-1"},
    ],
)
def test_artifacts_fail_closed_when_the_result_is_not_a_complete_reference(
    result: Any,
) -> None:
    service, _, _ = _service()
    job = _succeeded("tts", result)
    with pytest.raises(PublicJobUnavailableError):
        service.artifacts(job)


@pytest.mark.parametrize(
    "fact",
    [
        None,
        _artifact(artifact_id="artifact-other-1"),
        _artifact(kind="transcript"),
        _artifact(receipt_id="receipt-other-1"),
    ],
)
def test_artifacts_fail_closed_when_the_row_disagrees_with_the_result(
    fact: JobArtifactFact | None,
) -> None:
    service, _, store = _service()
    store.fact = fact
    job = _succeeded(
        "tts",
        {"kind": "tts", "audio_artifact_id": "artifact-read-1", "receipt_id": "receipt-read-1"},
    )
    with pytest.raises(PublicJobUnavailableError):
        service.artifacts(job)


def test_retry_refuses_a_quarantined_execution_and_a_key_owned_by_another_parent() -> None:
    service, _, store = _service()
    parent = service.submit(
        parse("JobCreateRequest", _body()), request_id="req-read-2", idempotency_key=None
    )
    service.advance(parent.job_id, "failed", error_code="cancellation_unconfirmed")
    with pytest.raises(JobCleanupPendingError):
        service.retry(parent.job_id, request_id="req-read-3", idempotency_key=None)

    other = service.submit(
        parse("JobCreateRequest", _body()), request_id="req-read-4", idempotency_key=None
    )
    service.advance(other.job_id, "failed", error_code="engine_failure")
    child = service.retry(other.job_id, request_id="req-read-5", idempotency_key="retry-key-0002")
    assert child.retry_of_job_id == other.job_id
    # The same key replays the replacement this parent already owns.
    replay = service.retry(other.job_id, request_id="req-read-6", idempotency_key="retry-key-0002")
    assert replay.job_id == child.job_id
    third = service.submit(
        parse("JobCreateRequest", _body()), request_id="req-read-7", idempotency_key=None
    )
    service.advance(third.job_id, "failed", error_code="engine_failure")
    with pytest.raises(IdempotencyConflictError):
        service.retry(third.job_id, request_id="req-read-8", idempotency_key="retry-key-0002")
    assert len(store.requests) == 4
