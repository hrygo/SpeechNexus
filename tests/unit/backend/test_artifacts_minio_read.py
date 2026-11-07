"""Whole-version reads for committed validation reports."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

import pytest

from speechnexus_backend.speech.domain.artifacts import ArtifactError, ArtifactRecord
from speechnexus_backend.speech.infrastructure.artifacts_minio import MinioArtifactObjects


class _Reader:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def read(self, size: int) -> bytes:
        return self._body[:size]


@contextmanager
def _opened(body: bytes, artifact: ArtifactRecord, offset: int, length: int) -> Iterator[_Reader]:
    assert offset == 0
    assert length == artifact.bytes
    yield _Reader(body)


def _artifact(*, size: int, digest: str) -> ArtifactRecord:
    return ArtifactRecord(
        artifact_id="artifact-validation",
        workspace_id="workspace-default",
        kind="validation_report",
        status="ready",
        media_type="application/json",
        bytes=size,
        sha256=digest,
        receipt_id=None,
        source_operation_id=None,
        created_at_us=1,
        expires_at_us=None,
        object_key="private/artifacts/report.json",
        storage_version_id="version-1",
    )


def test_read_returns_the_exact_committed_bytes(monkeypatch: pytest.MonkeyPatch) -> None:
    body = b'{"coverage": {}}'
    artifact = _artifact(size=len(body), digest="sha256:" + "a" * 64)
    objects = object.__new__(MinioArtifactObjects)
    monkeypatch.setattr(objects, "open", lambda a, o, n: _opened(body, a, o, n))
    assert objects.read(artifact) == body


def test_read_rejects_a_short_version(monkeypatch: pytest.MonkeyPatch) -> None:
    artifact = _artifact(size=10, digest="sha256:" + "a" * 64)
    objects = object.__new__(MinioArtifactObjects)
    monkeypatch.setattr(objects, "open", lambda a, o, n: _opened(b"short", a, o, n))
    with pytest.raises(ArtifactError) as error:
        objects.read(artifact)
    assert error.value.code == "artifact_unavailable"


def test_read_rejects_an_object_outside_the_size_bound() -> None:
    artifact = _artifact(size=64 * 1024 * 1024 + 1, digest="sha256:" + "a" * 64)
    objects = object.__new__(MinioArtifactObjects)
    with pytest.raises(ArtifactError) as error:
        objects.read(artifact)
    assert error.value.code == "artifact_unavailable"
