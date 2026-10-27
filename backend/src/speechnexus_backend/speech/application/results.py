"""Externalize one verified staging result into a committed public Artifact."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Mapping
from typing import Any, Protocol

from speechnexus_backend.speech.domain.artifacts import (
    ArtifactError,
    ArtifactKind,
    ArtifactObjectWrite,
    ArtifactRecord,
)
from speechnexus_backend.speech.domain.assets import DIGEST, hash_bytes, identifier
from speechnexus_backend.speech.domain.job_dispatch import (
    DispatchJob,
    DispatchProtocolError,
    DispatchUnavailableError,
    ExecutionSubmission,
    PublishedResult,
    result_artifact_identity,
    result_object_key,
    result_receipt_identity,
)

MAX_RESULT_BYTES = 64 * 1024 * 1024
_RESULT_MEDIA: dict[str, dict[str, ArtifactKind]] = {
    "asr": {"application/json": "transcript"},
    "tts": {"audio/wav": "audio", "audio/pcm": "audio", "audio/mpeg": "audio"},
}
_REFERENCE_FIELDS = frozenset(
    {
        "asset_or_artifact_id",
        "workspace_id",
        "storage_bucket",
        "object_key",
        "content_sha256",
        "byte_length",
    }
)


class ArtifactRows(Protocol):
    def find(self, artifact_id: str) -> ArtifactRecord | None: ...

    def insert_ready(self, artifact: ArtifactRecord) -> ArtifactRecord: ...


class ArtifactBlobs(Protocol):
    def check_writes_available(self) -> None: ...

    def put_verified(self, write: ArtifactObjectWrite) -> str: ...


class StagingResultObjects(Protocol):
    def read(self, reference: Mapping[str, Any], storage_version: str) -> bytes: ...


class ResultArtifactPublisher:
    """Write object bytes first, then one deterministic committed Artifact row."""

    def __init__(
        self,
        rows: ArtifactRows,
        blobs: ArtifactBlobs,
        staging: StagingResultObjects,
        *,
        now_us: Callable[[], int] | None = None,
        close: Callable[[], None] | None = None,
    ) -> None:
        self._rows, self._blobs, self._staging = rows, blobs, staging
        self._now_us = now_us or (lambda: time.time_ns() // 1_000)
        self._close = close

    def close(self) -> None:
        if self._close is not None:
            self._close()

    def check_ready(self) -> None:
        """Fail startup unless the Artifact bucket can return immutable versions."""
        self._blobs.check_writes_available()

    async def publish(
        self, job: DispatchJob, submission: ExecutionSubmission, receipt: Mapping[str, Any]
    ) -> PublishedResult:
        del submission
        return await asyncio.to_thread(self._publish, job, receipt)

    def _publish(self, job: DispatchJob, receipt: Mapping[str, Any]) -> PublishedResult:
        artifact_id = result_artifact_identity(job.job_id, job.kind)
        try:
            existing = self._rows.find(artifact_id)
            if existing is not None:
                return self._published(existing, job)
            reference, storage_version, media_type, kind = _result_receipt(job, receipt)
            body = self._staging.read(reference, storage_version)
            sha256 = hash_bytes(body)
            if len(body) != reference["byte_length"] or sha256 != reference["content_sha256"]:
                raise DispatchProtocolError("Staging result bytes disagree with their receipt")
            object_key = result_object_key(job.workspace_id, artifact_id)
            write = ArtifactObjectWrite(
                artifact_id,
                job.workspace_id,
                object_key,
                media_type,
                len(body),
                sha256,
                body,
            )
            version_id = self._blobs.put_verified(write)
            created_at_us = self._now_us()
            if type(created_at_us) is not int or created_at_us < 0:
                raise DispatchUnavailableError("Result publication clock is unavailable")
            committed = self._rows.insert_ready(
                ArtifactRecord(
                    artifact_id,
                    job.workspace_id,
                    kind,
                    "ready",
                    media_type,
                    len(body),
                    sha256,
                    result_receipt_identity(job.job_id),
                    job.job_id,
                    created_at_us,
                    None,
                    object_key,
                    version_id,
                )
            )
            return self._published(committed, job)
        except ArtifactError as error:
            if error.code in {"artifact_object_conflict", "artifact_object_integrity_mismatch"}:
                raise DispatchProtocolError("Committed Artifact facts disagree") from None
            raise DispatchUnavailableError("Result publication storage is unavailable") from None
        except ValueError:
            raise DispatchProtocolError("Result receipt is outside the frozen contract") from None

    @staticmethod
    def _published(record: ArtifactRecord, job: DispatchJob) -> PublishedResult:
        expected_kind = _RESULT_MEDIA[job.kind].get(record.media_type)
        if (
            record.workspace_id != job.workspace_id
            or expected_kind is None
            or record.kind != expected_kind
            or record.status != "ready"
            or record.receipt_id is None
        ):
            raise DispatchProtocolError("Committed Artifact belongs to another result")
        return PublishedResult(record.artifact_id, record.receipt_id)


def _result_receipt(
    job: DispatchJob, receipt: Mapping[str, Any]
) -> tuple[Mapping[str, Any], str, str, ArtifactKind]:
    if not isinstance(receipt, Mapping):
        raise DispatchProtocolError("Result receipt is not an object")
    reference = receipt.get("reference")
    if not isinstance(reference, Mapping) or set(reference) != _REFERENCE_FIELDS:
        raise DispatchProtocolError("Result receipt has no exact storage reference")
    media_type = receipt.get("media_type")
    if not isinstance(media_type, str):
        raise DispatchProtocolError("Result receipt has no media type")
    kind = _RESULT_MEDIA[job.kind].get(media_type)
    if kind is None:
        raise DispatchProtocolError("Result media type is not allowed for this Job")
    storage_version = receipt.get("storage_version")
    if (
        not isinstance(storage_version, str)
        or not 1 <= len(storage_version) <= 1024
        or storage_version == "null"
        or any(ord(char) < 32 or ord(char) == 127 for char in storage_version)
    ):
        raise DispatchProtocolError("Result receipt has no immutable storage version")
    if reference.get("workspace_id") != job.workspace_id:
        raise DispatchProtocolError("Result receipt belongs to another workspace")
    for field in ("asset_or_artifact_id", "workspace_id"):
        value = reference.get(field)
        if not isinstance(value, str):
            raise DispatchProtocolError("Result receipt has an invalid identity")
        identifier(value)
    bucket = reference.get("storage_bucket")
    object_key = reference.get("object_key")
    if not isinstance(bucket, str) or not 1 <= len(bucket) <= 255:
        raise DispatchProtocolError("Result receipt has an invalid bucket")
    if not isinstance(object_key, str) or not 1 <= len(object_key) <= 512:
        raise DispatchProtocolError("Result receipt has an invalid object key")
    content_sha256 = reference.get("content_sha256")
    if not isinstance(content_sha256, str) or DIGEST.fullmatch(content_sha256) is None:
        raise DispatchProtocolError("Result receipt has an invalid digest")
    byte_length = reference.get("byte_length")
    if type(byte_length) is not int or not 1 <= byte_length <= MAX_RESULT_BYTES:
        raise DispatchProtocolError("Result receipt has an invalid byte length")
    return reference, storage_version, media_type, kind


__all__ = ["ArtifactBlobs", "ArtifactRows", "ResultArtifactPublisher", "StagingResultObjects"]
