"""Actual versioned MinIO staging for the synchronous transcription input.

Synthetic bytes only. Proves the adapter writes one content-addressed object,
keeps it versioned, re-reads the exact pinned bytes and refuses an unversioned
bucket. It never exercises PostgreSQL admission, the gateway or an engine.
"""

from __future__ import annotations

import hashlib
import os
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

import pytest
from minio import Minio
from minio.commonconfig import ENABLED
from minio.versioningconfig import VersioningConfig

from speechnexus_backend.speech.application.transcription import (
    TranscriptionUnavailableError,
)
from speechnexus_backend.speech.domain.transcription import (
    TranscriptFormat,
    TranscriptionPlan,
)
from speechnexus_backend.speech.infrastructure.transcription_objects import (
    MinioTranscriptionInputs,
)

pytestmark = pytest.mark.integration

ENDPOINT_VARIABLE = "SPEECHNEXUS_TEST_MINIO_ENDPOINT"
ACCESS_VARIABLE = "SPEECHNEXUS_TEST_MINIO_ACCESS_KEY"
SECRET_VARIABLE = "SPEECHNEXUS_TEST_MINIO_SECRET_KEY"
WORKSPACE = "workspace-default"
AUDIO = b"RIFF" + bytes(range(256)) * 16


@dataclass
class Sandbox:
    endpoint: str
    access_key: str
    secret_key: str
    admin: Minio
    bucket: str

    def inputs(self, *, bucket: str | None = None) -> MinioTranscriptionInputs:
        return MinioTranscriptionInputs(
            endpoint=self.endpoint,
            bucket=bucket or self.bucket,
            access_key=self.access_key,
            secret_key=self.secret_key,
        )


def _admin(endpoint: str, access_key: str, secret_key: str) -> Minio:
    return Minio(
        endpoint.removeprefix("http://").removeprefix("https://"),
        access_key=access_key,
        secret_key=secret_key,
        secure=endpoint.startswith("https://"),
        region="us-east-1",
    )


@pytest.fixture()
def sandbox() -> Iterator[Sandbox]:
    values = tuple(
        os.environ.get(name) for name in (ENDPOINT_VARIABLE, ACCESS_VARIABLE, SECRET_VARIABLE)
    )
    if not all(values):
        pytest.skip("Configure an isolated MinIO test endpoint and keys")
    endpoint, access_key, secret_key = (str(value) for value in values)
    admin = _admin(endpoint, access_key, secret_key)
    bucket = "transcription-inputs-" + uuid.uuid4().hex[:16]
    admin.make_bucket(bucket)
    admin.set_bucket_versioning(bucket, VersioningConfig(ENABLED))
    try:
        yield Sandbox(endpoint, access_key, secret_key, admin, bucket)
    finally:
        for item in admin.list_objects(bucket, recursive=True, include_version=True):
            admin.remove_object(bucket, item.object_name, version_id=item.version_id)
        admin.remove_bucket(bucket)


def _plan(request_id: str) -> TranscriptionPlan:
    return TranscriptionPlan(
        request_id=request_id,
        workspace_id=WORKSPACE,
        model="asr-quality",
        binding_publication_id="binding-rev-3",
        audio=AUDIO,
        audio_media_type="audio/wav",
        audio_sample_rate_hz=16_000,
        audio_channels=1,
        response_format=TranscriptFormat.JSON,
        language="auto",
    )


def _versions(sandbox: Sandbox, key: str) -> list[Any]:
    return list(
        sandbox.admin.list_objects(sandbox.bucket, prefix=key, recursive=True, include_version=True)
    )


def test_real_versioned_bucket_pins_the_uploaded_bytes(sandbox: Sandbox) -> None:
    staged = sandbox.inputs().put(_plan("request-real-input"))

    digest = "sha256:" + hashlib.sha256(AUDIO).hexdigest()
    assert staged.bucket == sandbox.bucket
    assert staged.workspace_id == WORKSPACE
    assert staged.content_sha256 == digest
    assert staged.byte_length == len(AUDIO)
    assert staged.object_key == (
        "private/transcription-inputs/" + WORKSPACE + "/" + digest[7:] + ".audio"
    )

    versions = _versions(sandbox, staged.object_key)
    assert len(versions) == 1
    pinned = versions[0]
    found = sandbox.admin.stat_object(
        sandbox.bucket, staged.object_key, version_id=pinned.version_id
    )
    assert found.size == len(AUDIO)
    response = sandbox.admin.get_object(
        sandbox.bucket,
        staged.object_key,
        offset=0,
        length=len(AUDIO),
        version_id=pinned.version_id,
    )
    try:
        assert response.read(len(AUDIO)) == AUDIO
    finally:
        response.close()
        response.release_conn()


def test_repeat_upload_keeps_every_version_of_the_same_content(sandbox: Sandbox) -> None:
    client = sandbox.inputs()
    first = client.put(_plan("request-real-input-a"))
    second = client.put(_plan("request-real-input-b"))

    assert first.object_key == second.object_key
    assert len(_versions(sandbox, first.object_key)) == 2


def test_unversioned_bucket_never_yields_a_readable_reference(sandbox: Sandbox) -> None:
    unversioned = "transcription-unversioned-" + uuid.uuid4().hex[:16]
    sandbox.admin.make_bucket(unversioned)
    try:
        with pytest.raises(TranscriptionUnavailableError):
            sandbox.inputs(bucket=unversioned).put(_plan("request-real-unversioned"))
    finally:
        for item in sandbox.admin.list_objects(unversioned, recursive=True, include_version=True):
            sandbox.admin.remove_object(unversioned, item.object_name, version_id=item.version_id)
        sandbox.admin.remove_bucket(unversioned)
