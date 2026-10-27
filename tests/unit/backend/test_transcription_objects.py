"""Private input staging verifies the exact version-pinned bytes before admission."""

from __future__ import annotations

import hashlib
from typing import Any

import pytest

from speechnexus_backend.speech.application.transcription import TranscriptionUnavailableError
from speechnexus_backend.speech.domain.transcription import TranscriptFormat, TranscriptionPlan
from speechnexus_backend.speech.infrastructure.transcription_objects import (
    MinioTranscriptionInputs,
)
from tests.unit.backend.test_transcription_http import AUDIO


class _Put:
    def __init__(self, version_id: str) -> None:
        self.version_id = version_id


class _Stat:
    def __init__(self, version: str, size: int, content_type: str) -> None:
        self.version_id = version
        self.size = size
        self.content_type = content_type


class _Response:
    def __init__(self, body: bytes, *, version: str) -> None:
        self.status = 206
        self._body = body
        self.headers = {
            "x-amz-version-id": version,
            "content-range": f"bytes 0-{len(body) - 1}/{len(body)}",
            "content-length": str(len(body)),
        }
        self.closed = False
        self.released = False

    def read(self, limit: int = -1) -> bytes:
        return self._body[:limit]

    def close(self) -> None:
        self.closed = True

    def release_conn(self) -> None:
        self.released = True


class _Client:
    def __init__(
        self,
        body: bytes,
        *,
        version: str = "version-1",
        stat_version: str | None = None,
        content_type: str = "audio/wav",
    ) -> None:
        self.body = body
        self.version = version
        self.stat_version = stat_version or version
        self.content_type = content_type
        self.calls: list[tuple[str, Any]] = []
        self.response: _Response | None = None

    def put_object(self, bucket: str, key: str, stream: Any, length: int, **kwargs: Any) -> _Put:
        self.calls.append(("put", (bucket, key, length, kwargs)))
        return _Put(self.version)

    def stat_object(self, bucket: str, key: str, *, version_id: str) -> _Stat:
        self.calls.append(("stat", (bucket, key, version_id)))
        return _Stat(self.stat_version, len(self.body), self.content_type)

    def get_object(
        self, bucket: str, key: str, *, offset: int, length: int, version_id: str
    ) -> _Response:
        self.calls.append(("get", (bucket, key, offset, length, version_id)))
        self.response = _Response(self.body[:length], version=version_id)
        return self.response


def _plan() -> TranscriptionPlan:
    return TranscriptionPlan(
        request_id="request-input-object",
        workspace_id="workspace-default",
        model="asr-quality",
        binding_publication_id="binding-rev-3",
        audio=AUDIO,
        audio_media_type="audio/wav",
        audio_sample_rate_hz=16_000,
        audio_channels=1,
        response_format=TranscriptFormat.JSON,
        language="auto",
    )


def _inputs(client: _Client) -> MinioTranscriptionInputs:
    return MinioTranscriptionInputs(
        endpoint="http://127.0.0.1:9000",
        bucket="transcription-inputs",
        access_key="synthetic-access",
        secret_key="synthetic-secret",
        _client=client,
    )


def test_input_is_version_pinned_and_read_back_before_admission() -> None:
    client = _Client(AUDIO)
    staged = _inputs(client).put(_plan())

    digest = "sha256:" + hashlib.sha256(AUDIO).hexdigest()
    assert staged.content_sha256 == digest
    assert staged.byte_length == len(AUDIO)
    assert staged.bucket == "transcription-inputs"
    assert staged.object_key == (
        "private/transcription-inputs/workspace-default/"
        + digest.removeprefix("sha256:")
        + ".audio"
    )
    assert [name for name, _ in client.calls] == ["put", "stat", "get"]
    assert client.response is not None
    assert client.response.closed is True and client.response.released is True


def test_unversioned_write_is_not_a_readable_reference() -> None:
    with pytest.raises(TranscriptionUnavailableError):
        _inputs(_Client(AUDIO, version="null")).put(_plan())


def test_readback_that_differs_from_the_write_is_refused() -> None:
    client = _Client(b"tampered")
    with pytest.raises(TranscriptionUnavailableError):
        _inputs(client).put(_plan())


def test_relative_input_endpoint_is_refused() -> None:
    with pytest.raises(ValueError):
        MinioTranscriptionInputs(
            endpoint="127.0.0.1:9000",
            bucket="transcription-inputs",
            access_key="synthetic-access",
            secret_key="synthetic-secret",
        )
