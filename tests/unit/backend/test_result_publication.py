"""Publication keeps object-first ordering and one committed row across replays."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from typing import Any

import pytest

from speechnexus_backend.speech.application.results import (
    MAX_RESULT_RETENTION_US,
    ResultArtifactPublisher,
    _media_for,
)
from speechnexus_backend.speech.domain.artifacts import (
    ARTIFACT_TIMESTAMP_MAX_US,
    ArtifactError,
    ArtifactObjectWrite,
    ArtifactRecord,
    ArtifactStatus,
)
from speechnexus_backend.speech.domain.assets import hash_bytes
from speechnexus_backend.speech.domain.job_dispatch import (
    DispatchJob,
    DispatchProtocolError,
    DispatchUnavailableError,
    ExecutionSubmission,
    result_artifact_identity,
)

RETENTION_US = 30 * 24 * 60 * 60 * 1_000_000
DIGEST = "sha256:" + "a" * 64
BODY = b'{"text":"synthetic transcript","language":null,"duration_ms":null}'


def _job(kind: str = "asr") -> DispatchJob:
    return DispatchJob(
        "job-result-0001",
        "workspace-default",
        kind,  # type: ignore[arg-type]
        "running",
        2,
        1_700_000_000_000_000,
        DIGEST,
        '{"kind":"asr"}',
    )


def _submission() -> ExecutionSubmission:
    return ExecutionSubmission("submit-result-0001", {"submit_intent_id": "submit-result-0001"})


def _receipt(body: bytes = BODY, media_type: str = "application/json") -> dict[str, Any]:
    return {
        "request_digest": DIGEST,
        "reference": {
            "asset_or_artifact_id": "artifact-staging-1",
            "workspace_id": "workspace-default",
            "storage_bucket": "synthetic-results",
            "object_key": "staging/result.json",
            "content_sha256": hash_bytes(body),
            "byte_length": len(body),
        },
        "storage_version": "version-staging-1",
        "media_type": media_type,
    }


@dataclass
class Rows:
    records: dict[str, ArtifactRecord] = field(default_factory=dict)
    events: list[str] = field(default_factory=list)
    fail_next: bool = False
    inserts: int = 0

    def find(self, artifact_id: str) -> ArtifactRecord | None:
        return self.records.get(artifact_id)

    def insert_ready(self, artifact: ArtifactRecord) -> ArtifactRecord:
        self.events.append("row")
        self.inserts += 1
        if self.fail_next:
            self.fail_next = False
            raise ArtifactError("artifact_storage_unavailable")
        existing = self.records.get(artifact.artifact_id)
        if existing is not None:
            return existing
        self.records[artifact.artifact_id] = artifact
        return artifact


@dataclass
class Blobs:
    events: list[str] = field(default_factory=list)
    versions: dict[str, str] = field(default_factory=dict)
    versioning_required: bool = False
    writes: int = 0

    def check_writes_available(self) -> None:
        if self.versioning_required:
            raise ArtifactError("artifact_storage_versioning_required")

    def put_verified(self, write: ArtifactObjectWrite) -> str:
        self.events.append("object")
        existing = self.versions.get(write.object_key)
        if existing is not None:
            return existing
        self.writes += 1
        version = f"version-{self.writes}"
        self.versions[write.object_key] = version
        return version


@dataclass
class Staging:
    body: bytes = BODY
    reads: int = 0

    def read(self, reference: Mapping[str, Any], storage_version: str) -> bytes:
        self.reads += 1
        assert storage_version == "version-staging-1"
        return self.body


async def test_repeat_publication_is_one_row_one_object() -> None:
    rows, blobs, staging = Rows(), Blobs(), Staging()
    publisher = ResultArtifactPublisher(
        rows, blobs, staging, retention_us=RETENTION_US, now_us=lambda: 1_700_000_000_000_000
    )
    job = _job()

    first = await publisher.publish(job, _submission(), _receipt())
    second = await publisher.publish(job, _submission(), _receipt())

    assert first == second
    assert first.artifact_id == result_artifact_identity(job.job_id, job.kind)
    assert tuple(rows.records) == (first.artifact_id,)
    assert len(blobs.versions) == 1
    assert blobs.writes == 1
    assert staging.reads == 1
    assert rows.events == ["row"]


async def test_publication_records_the_configured_retention_deadline() -> None:
    """`expires_at_us` comes from configuration, not from the publication branch.

    Two publishers built with different windows must record different deadlines
    for the same clock, and the committed value must be exactly
    ``created_at_us + retention_us``. This is the assertion that stays red if the
    publication path goes back to writing ``None``.
    """
    created_at_us = 1_700_000_000_000_000
    for retention_us in (RETENTION_US, 7 * 24 * 60 * 60 * 1_000_000):
        rows, blobs, staging = Rows(), Blobs(), Staging()
        publisher = ResultArtifactPublisher(
            rows,
            blobs,
            staging,
            retention_us=retention_us,
            now_us=lambda: created_at_us,
        )
        published = await publisher.publish(_job(), _submission(), _receipt())
        committed = rows.records[published.artifact_id]
        assert committed.expires_at_us is not None
        assert committed.expires_at_us == created_at_us + retention_us
        assert committed.created_at_us == created_at_us


def test_result_retention_must_be_an_explicit_bounded_window() -> None:
    for retention_us in (0, -1, 10 * 365 * 24 * 60 * 60 * 1_000_000 + 1, True, 1.5):
        with pytest.raises(ValueError, match="explicit bounded window"):
            ResultArtifactPublisher(
                Rows(),
                Blobs(),
                Staging(),
                retention_us=retention_us,  # type: ignore[arg-type]
            )


@pytest.mark.parametrize("state", ["ready", "expired", "deleted"])
async def test_replay_settles_a_committed_result_after_retention_ended(
    state: ArtifactStatus,
) -> None:
    """A committed publication still settles when retention moved the row.

    The retention sweep changes ``artifact_state`` on its own clock, so a crashed
    dispatcher that replays the same publication hours later may find the row
    ``expired`` (or already ``deleted``). Settlement asks "is this my committed
    publication", not "is its content still servable": answering the second
    question here would leave the Job in ``outcome_unknown`` forever.
    """
    rows, blobs, staging = Rows(), Blobs(), Staging()
    publisher = ResultArtifactPublisher(
        rows, blobs, staging, retention_us=RETENTION_US, now_us=lambda: 1_700_000_000_000_000
    )
    job = _job()
    first = await publisher.publish(job, _submission(), _receipt())

    # The retention sweep expires the row (and may already have removed content).
    rows.records[first.artifact_id] = replace(rows.records[first.artifact_id], status=state)

    replayed = await publisher.publish(job, _submission(), _receipt())

    assert replayed == first
    assert rows.inserts == 1
    assert blobs.writes == 1
    assert staging.reads == 1


async def test_a_deadline_beyond_the_recorded_clock_range_is_a_protocol_refusal() -> None:
    """An unreachable deadline is a configuration error, not a retryable outage.

    Retrying would compute the same value, so the publisher must refuse it as a
    protocol violation instead of inviting the dispatcher to try again.
    """
    rows, blobs, staging = Rows(), Blobs(), Staging()
    publisher = ResultArtifactPublisher(
        rows,
        blobs,
        staging,
        retention_us=MAX_RESULT_RETENTION_US,
        now_us=lambda: ARTIFACT_TIMESTAMP_MAX_US - 1,
    )

    with pytest.raises(DispatchProtocolError):
        await publisher.publish(_job(), _submission(), _receipt())
    assert rows.inserts == 0


async def test_the_deadline_bound_is_reachable_exactly() -> None:
    rows, blobs, staging = Rows(), Blobs(), Staging()
    publisher = ResultArtifactPublisher(
        rows,
        blobs,
        staging,
        retention_us=1_000_000,
        now_us=lambda: ARTIFACT_TIMESTAMP_MAX_US - 1_000_000,
    )

    published = await publisher.publish(_job(), _submission(), _receipt())

    assert rows.records[published.artifact_id].expires_at_us == ARTIFACT_TIMESTAMP_MAX_US


async def test_object_is_written_before_the_committed_row() -> None:
    events: list[str] = []
    rows, blobs, staging = Rows(events=events), Blobs(events=events), Staging()

    await ResultArtifactPublisher(rows, blobs, staging, retention_us=RETENTION_US).publish(
        _job(), _submission(), _receipt()
    )

    assert events == ["object", "row"]


async def test_database_failure_after_object_write_retries_to_one_row_one_version() -> None:
    rows, blobs, staging = Rows(fail_next=True), Blobs(), Staging()
    publisher = ResultArtifactPublisher(rows, blobs, staging, retention_us=RETENTION_US)

    with pytest.raises(DispatchUnavailableError):
        await publisher.publish(_job(), _submission(), _receipt())

    assert rows.records == {}
    assert len(blobs.versions) == 1

    published = await publisher.publish(_job(), _submission(), _receipt())

    assert tuple(rows.records) == (published.artifact_id,)
    assert len(blobs.versions) == 1
    assert blobs.writes == 1
    assert rows.records[published.artifact_id].storage_version_id == "version-1"


async def test_invalid_media_type_never_writes_an_object_or_row() -> None:
    rows, blobs, staging = Rows(), Blobs(), Staging()
    publisher = ResultArtifactPublisher(rows, blobs, staging, retention_us=RETENTION_US)

    with pytest.raises(DispatchProtocolError):
        await publisher.publish(_job(), _submission(), _receipt(media_type="audio/wav"))

    assert rows.records == {} and blobs.versions == {} and staging.reads == 0


async def test_receipt_workspace_must_match_the_job() -> None:
    rows, blobs, staging = Rows(), Blobs(), Staging()
    receipt = _receipt()
    receipt["reference"]["workspace_id"] = "workspace-other"
    publisher = ResultArtifactPublisher(rows, blobs, staging, retention_us=RETENTION_US)

    with pytest.raises(DispatchProtocolError):
        await publisher.publish(_job(), _submission(), receipt)

    assert rows.records == {} and blobs.versions == {}


def test_result_kind_without_a_media_mapping_fails_closed() -> None:
    """A kind the receipt map does not know must never fall through as mutable."""
    with pytest.raises(DispatchProtocolError, match="no published media mapping"):
        _media_for("voice_preview")


def test_artifact_identity_is_deterministic_and_kind_specific() -> None:
    first = result_artifact_identity("job-result-0001", "asr")
    assert first == result_artifact_identity("job-result-0001", "asr")
    assert first != result_artifact_identity("job-result-0001", "tts")
    assert first.startswith("artifact-") and len(first) <= 128


def test_startup_requires_bucket_versioning() -> None:
    publisher = ResultArtifactPublisher(
        Rows(), Blobs(versioning_required=True), Staging(), retention_us=RETENTION_US
    )
    with pytest.raises(ArtifactError):
        publisher.check_ready()
