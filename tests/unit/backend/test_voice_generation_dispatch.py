"""Voice generation lane: kinds, result projection, publication and timeouts."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from typing import Any, cast

import pytest

from speechnexus_backend.speech.application.job_dispatcher import JobDispatcher
from speechnexus_backend.speech.application.results import ResultArtifactPublisher
from speechnexus_backend.speech.domain.artifacts import (
    ArtifactObjectWrite,
    ArtifactRecord,
)
from speechnexus_backend.speech.domain.assets import hash_bytes
from speechnexus_backend.speech.domain.job_dispatch import (
    DISPATCHABLE_JOB_KIND_UNION,
    DISPATCHABLE_JOB_KINDS,
    VOICE_GENERATION_JOB_KINDS,
    ClaimedJob,
    DispatchJob,
    DispatchLimits,
    DispatchProtocolError,
    DispatchUnavailableError,
    ExecutionSubmission,
    GatewayReply,
    PublishedResult,
    result_artifact_identity,
    terminal_result,
)
from speechnexus_backend.speech.infrastructure.job_dispatch_postgres import (
    PostgresDispatchQueue,
)

NOW = 1_700_000_000_000_000
DIGEST = "sha256:" + "a" * 64
BODY = b"synthetic-voice-preview"


def _digest(document: str) -> str:
    return "sha256:" + hashlib.sha256(document.encode("utf-8")).hexdigest()


def _creation_document(kind: str) -> str:
    return json.dumps(
        {"kind": kind, "input": {"description": "Synthetic voice", "preview_text": "Synthetic"}}
    )


def _generation_document(kind: str, draft_id: str, expected_revision: str) -> str:
    return json.dumps(
        {
            "kind": kind,
            "input": {"description": "Synthetic voice", "preview_text": "Synthetic"},
            "draft_target": {"id": draft_id, "expected_revision": expected_revision},
        }
    )


def _job(
    kind: str = "voice_design",
    document: str | None = None,
    *,
    state: str = "pending",
    created_at_us: int = NOW,
) -> DispatchJob:
    request = _creation_document(kind) if document is None else document
    return DispatchJob(
        "job-voice-0001",
        "workspace-default",
        kind,  # type: ignore[arg-type]
        state,  # type: ignore[arg-type]
        1,
        created_at_us,
        _digest(request),
        request,
    )


def test_default_lane_stays_asr_and_tts_while_the_union_adds_voice() -> None:
    assert DISPATCHABLE_JOB_KINDS == ("asr", "tts")
    assert VOICE_GENERATION_JOB_KINDS == ("voice_design", "voice_clone")
    assert DISPATCHABLE_JOB_KIND_UNION == ("asr", "tts", "voice_design", "voice_clone")


@pytest.mark.parametrize("kind", DISPATCHABLE_JOB_KIND_UNION)
def test_dispatch_job_accepts_every_union_kind(kind: str) -> None:
    assert _job(kind).kind == kind


@pytest.mark.parametrize("kind", ["voice_preview", "voice_validation", "unknown"])
def test_dispatch_job_rejects_a_kind_outside_the_union(kind: str) -> None:
    with pytest.raises(ValueError, match="Invalid dispatch Job kind"):
        _job(kind)


def test_creation_result_names_the_draft_allocated_beside_the_job() -> None:
    job = _job("voice_design")

    result = json.loads(terminal_result(job, "preview-synthetic", "receipt-synthetic"))

    assert result == {
        "kind": "voice_design",
        "voice_draft_id": "draft-job-voice-0001",
        "draft_revision": "1",
        "preview_artifact_ids": ["preview-synthetic"],
        "receipt_id": "receipt-synthetic",
    }


def test_generation_result_names_the_appended_revision_not_the_target() -> None:
    # ``create_revision`` stores the append as expected_revision + 1 and moves
    # the draft head onto it; the draft projection compares the result with
    # that stored revision.
    job = _job("voice_clone", _generation_document("voice_clone", "draft-existing", "3"))

    result = json.loads(terminal_result(job, "preview-synthetic", "receipt-synthetic"))

    assert result["voice_draft_id"] == "draft-existing"
    assert result["draft_revision"] == "4"
    assert result["preview_artifact_ids"] == ["preview-synthetic"]


@pytest.mark.parametrize(
    "document",
    [
        "not-json",
        "[]",
        json.dumps({"kind": "voice_design", "draft_target": {"id": "draft-existing"}}),
        json.dumps(
            {
                "kind": "voice_design",
                "draft_target": {"id": "draft-existing", "expected_revision": "0"},
            }
        ),
        json.dumps(
            {
                "kind": "voice_design",
                "draft_target": {"id": "Draft-Existing", "expected_revision": "1"},
            }
        ),
    ],
)
def test_unreadable_voice_draft_target_is_a_protocol_error(document: str) -> None:
    job = _job("voice_design", document)

    with pytest.raises(DispatchProtocolError):
        terminal_result(job, "preview-synthetic", "receipt-synthetic")


def test_voice_request_kind_must_match_the_job_kind() -> None:
    """The stored command and the authoritative Job row must name one kind."""
    job = _job("voice_design", _generation_document("voice_clone", "draft-existing", "1"))

    with pytest.raises(DispatchProtocolError, match="kind disagrees"):
        terminal_result(job, "preview-synthetic", "receipt-synthetic")


@dataclass
class _Cursor:
    def fetchall(self) -> list[dict[str, Any]]:
        return []


@dataclass
class _Connection:
    parameters: list[Any] = field(default_factory=list)

    def execute(self, statement: str, parameters: Any = None) -> _Cursor:
        self.parameters.append(parameters)
        return _Cursor()

    def __enter__(self) -> _Connection:
        return self

    def __exit__(self, *_: object) -> None:
        return None


@dataclass
class _Pool:
    connection_log: _Connection = field(default_factory=_Connection)

    def connection(self, timeout: float) -> _Connection:
        del timeout
        return self.connection_log


def _queue(kinds: tuple[str, ...] | None = None) -> tuple[PostgresDispatchQueue, _Pool]:
    pool = _Pool()
    queue = (
        PostgresDispatchQueue(cast(Any, pool))
        if kinds is None
        else PostgresDispatchQueue(cast(Any, pool), kinds=kinds)
    )
    return queue, pool


def test_queue_defaults_to_the_asr_tts_lane() -> None:
    queue, _ = _queue()

    assert queue.kinds == DISPATCHABLE_JOB_KINDS


def test_queue_accepts_an_explicit_voice_lane() -> None:
    queue, _ = _queue(VOICE_GENERATION_JOB_KINDS)

    assert queue.kinds == VOICE_GENERATION_JOB_KINDS


@pytest.mark.parametrize(
    ("kinds", "message"),
    [
        ((), "at least one Job kind"),
        (("asr", "asr"), "must be unique"),
        (("voice_preview",), "outside the dispatchable union"),
        (("asr", "voice_validation"), "outside the dispatchable union"),
    ],
)
def test_queue_rejects_an_invalid_lane(kinds: tuple[str, ...], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        _queue(kinds)


def test_claim_uses_the_instance_lane_not_the_module_default() -> None:
    queue, pool = _queue(VOICE_GENERATION_JOB_KINDS)

    assert queue._claim("worker-synthetic", 30_000, 4) == []
    claim_parameters = pool.connection_log.parameters[-1]

    assert claim_parameters[0] == list(VOICE_GENERATION_JOB_KINDS)


@dataclass
class _Rows:
    records: dict[str, ArtifactRecord] = field(default_factory=dict)

    def find(self, artifact_id: str) -> ArtifactRecord | None:
        return self.records.get(artifact_id)

    def insert_ready(self, artifact: ArtifactRecord) -> ArtifactRecord:
        self.records[artifact.artifact_id] = artifact
        return artifact


@dataclass
class _Blobs:
    writes: int = 0

    def check_writes_available(self) -> None:
        return None

    def put_verified(self, write: ArtifactObjectWrite) -> str:
        del write
        self.writes += 1
        return "version-1"


@dataclass
class _Staging:
    reads: int = 0

    def read(self, reference: Mapping[str, Any], storage_version: str) -> bytes:
        assert reference["byte_length"] == len(BODY)
        assert storage_version == "version-staging-1"
        self.reads += 1
        return BODY


def _receipt(media_type: str = "audio/wav") -> dict[str, Any]:
    return {
        "request_digest": DIGEST,
        "reference": {
            "asset_or_artifact_id": "artifact-staging-1",
            "workspace_id": "workspace-default",
            "storage_bucket": "synthetic-results",
            "object_key": "staging/voice-preview",
            "content_sha256": hash_bytes(BODY),
            "byte_length": len(BODY),
        },
        "storage_version": "version-staging-1",
        "media_type": media_type,
    }


@pytest.mark.parametrize("kind", VOICE_GENERATION_JOB_KINDS)
def test_voice_result_publication_commits_one_preview_artifact(kind: str) -> None:
    rows, blobs, staging = _Rows(), _Blobs(), _Staging()
    publisher = ResultArtifactPublisher(rows, blobs, staging, now_us=lambda: NOW)
    job = _job(kind, state="running")

    published = publisher._publish(job, _receipt())

    commit = rows.records[published.artifact_id]
    assert commit.kind == "preview"
    assert commit.media_type == "audio/wav"
    assert published.artifact_id == result_artifact_identity(job.job_id, job.kind)
    assert published.receipt_id == commit.receipt_id
    assert staging.reads == 1 and blobs.writes == 1


def test_voice_publication_rejects_a_non_preview_media_type() -> None:
    rows, blobs, staging = _Rows(), _Blobs(), _Staging()
    publisher = ResultArtifactPublisher(rows, blobs, staging)

    with pytest.raises(DispatchProtocolError):
        publisher._publish(_job(state="running"), _receipt(media_type="application/json"))

    assert rows.records == {} and staging.reads == 0


class _Queue:
    def __init__(self, claims: list[ClaimedJob]) -> None:
        self._claims = claims

    async def claim_due(self, worker_id: str, lease_ms: int, limit: int) -> list[ClaimedJob]:
        del worker_id, lease_ms, limit
        return self._claims


class _States:
    def __init__(self, job: DispatchJob) -> None:
        self.job = job
        self.finished: list[tuple[str, str | None]] = []
        self.events: list[str] = []

    async def reload(self, job_id: str) -> DispatchJob:
        del job_id
        return self.job

    async def mark_running(self, job: DispatchJob) -> DispatchJob:
        self.job = replace(job, state="running", status_version=job.status_version + 1)
        self.events.append("running")
        return self.job

    async def renew(self, claim: ClaimedJob, lease_ms: int) -> ClaimedJob:
        del lease_ms
        return claim

    async def record_cancellation_not_submitted(self, job_id: str, *, observer: str) -> None:
        del job_id, observer

    async def record_progress(
        self, claim: ClaimedJob, *, unit: str, completed: int, total: int | None
    ) -> bool:
        del claim, unit, completed, total
        return True

    async def finish(
        self,
        claim: ClaimedJob,
        target: str,
        *,
        result_json: str | None,
        error_code: str | None,
    ) -> DispatchJob:
        self.finished.append((target, error_code))
        self.job = replace(
            self.job, state=cast(Any, target), status_version=self.job.status_version + 1
        )
        del claim, result_json
        return self.job


class _Planner:
    async def plan(self, job: DispatchJob, submit_intent_id: str) -> ExecutionSubmission:
        del job
        return ExecutionSubmission(
            submit_intent_id,
            {"submit_intent_id": submit_intent_id, "command_digest": DIGEST},
        )


class _UnreachableGateway:
    def __init__(self) -> None:
        self.queries = 0
        self.submits = 0

    async def query(self, submit_intent_id: str) -> GatewayReply:
        del submit_intent_id
        self.queries += 1
        return GatewayReply("not_found")

    async def submit(self, submission: ExecutionSubmission) -> GatewayReply:
        del submission
        self.submits += 1
        raise AssertionError("a dispatcher timeout must not submit an execution")

    async def result_receipt(self, submit_intent_id: str, *, audio: bool) -> Any:
        del submit_intent_id, audio
        raise AssertionError("a dispatcher timeout must not read a receipt")


class _Publisher:
    async def publish(
        self, job: DispatchJob, submission: ExecutionSubmission, receipt: Mapping[str, Any]
    ) -> PublishedResult:
        del job, submission, receipt
        raise AssertionError("a dispatcher timeout must not publish")


def _dispatcher(
    job: DispatchJob,
    gateway: Any,
    *,
    now_us: Any = None,
    limits: DispatchLimits | None = None,
) -> tuple[JobDispatcher, _States]:
    claim = ClaimedJob(job, "worker-synthetic", 1, NOW + 30_000_000)
    states = _States(job)
    dispatcher = JobDispatcher(
        _Queue([claim]),
        cast(Any, states),
        _Planner(),
        gateway,
        _Publisher(),
        limits=limits or DispatchLimits(poll_seconds=0.01, job_timeout_seconds=60),
        now_us=now_us or (lambda: NOW),
    )
    return dispatcher, states


async def test_dispatcher_timeout_before_submit_stays_unknown_and_running() -> None:
    job = _job(state="pending")
    gateway = _UnreachableGateway()
    dispatcher, states = _dispatcher(
        job,
        gateway,
        now_us=lambda: NOW + 2 * 3_600_000_000,
    )

    batch = await dispatcher.run_once("worker-synthetic")

    assert batch.unknown == 1 and batch.failed == 0
    assert states.job.state == "running"
    assert states.finished == []
    assert gateway.submits == 0


class _EngineGateway:
    """Query returns not_found until submit, then the scripted engine state."""

    def __init__(self, execution_state: str, error_code: str | None = None) -> None:
        self._execution_state, self._error_code = execution_state, error_code
        self._submit_intent_id: str | None = None
        self.queries = 0
        self.submits = 0

    def _status(self) -> dict[str, Any]:
        assert self._submit_intent_id is not None
        status: dict[str, Any] = {
            "submit_intent_id": self._submit_intent_id,
            "execution_state": self._execution_state,
        }
        if self._error_code is not None:
            status["error_code"] = self._error_code
        return status

    async def query(self, submit_intent_id: str) -> GatewayReply:
        del submit_intent_id
        self.queries += 1
        if self._submit_intent_id is None:
            return GatewayReply("not_found")
        return GatewayReply("status", self._status())

    async def submit(self, submission: ExecutionSubmission) -> GatewayReply:
        self.submits += 1
        self._submit_intent_id = submission.submit_intent_id
        return GatewayReply(
            "status",
            {
                "submit_intent_id": submission.submit_intent_id,
                "execution_state": "executing",
            },
        )

    async def result_receipt(self, submit_intent_id: str, *, audio: bool) -> Any:
        del submit_intent_id, audio
        raise AssertionError("a failed engine execution has no receipt")


@pytest.mark.parametrize("state", ["failed", "aborted"])
async def test_engine_reported_voice_timeout_is_projected_failed(state: str) -> None:
    job = _job("voice_design")
    gateway = _EngineGateway(state, "voice_generation_timeout")
    dispatcher, states = _dispatcher(job, gateway)

    batch = await dispatcher.run_once("worker-synthetic")

    assert batch.failed == 1 and batch.unknown == 0
    assert states.finished == [("failed", "voice_generation_timeout")]
    assert states.job.state == "failed"


async def test_unavailable_state_store_does_not_publish_or_finish_a_voice_job() -> None:
    job = _job("voice_clone")

    class _Unavailable(_States):
        async def renew(self, claim: ClaimedJob, lease_ms: int) -> ClaimedJob:
            del claim, lease_ms
            raise DispatchUnavailableError("storage unavailable")

    claim = ClaimedJob(job, "worker-synthetic", 1, NOW + 30_000_000)
    states = _Unavailable(job)
    dispatcher = JobDispatcher(
        _Queue([claim]),
        cast(Any, states),
        _Planner(),
        _UnreachableGateway(),
        _Publisher(),
        limits=DispatchLimits(poll_seconds=0.01, job_timeout_seconds=60),
    )

    batch = await dispatcher.run_once("worker-synthetic")

    assert batch.unknown == 1 and batch.failed == 0
    assert states.finished == []
