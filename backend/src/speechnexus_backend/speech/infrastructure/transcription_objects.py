"""Versioned private input staging for the synchronous file-ASR gateway.

The public upload is not an Asset: it has no metadata row, retention decision or
download surface. This adapter writes one content-addressed object to the
configured gateway-visible input bucket, pins its S3 version and reads those
exact bytes back before admission may refer to the reference.
"""

from __future__ import annotations

import hashlib
import io
from typing import Any
from urllib.parse import urlsplit

from minio import Minio
from minio.error import S3Error

from speechnexus_backend.speech.application.transcription import (
    StagedAudio,
    TranscriptionUnavailableError,
)
from speechnexus_backend.speech.domain.transcription import TranscriptionPlan
from speechnexus_backend.speech.infrastructure.assets_minio import _FixedOriginHTTP


class MinioTranscriptionInputs:
    """Write and verify one immutable input in the gateway's configured bucket."""

    def __init__(
        self,
        *,
        endpoint: str,
        bucket: str,
        access_key: str,
        secret_key: str,
        _client: Any | None = None,
    ) -> None:
        parsed = urlsplit(endpoint)
        if (
            parsed.scheme not in ("http", "https")
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.path not in ("", "/")
            or parsed.query
            or parsed.fragment
            or not bucket
            or not access_key
            or not secret_key
        ):
            raise ValueError("Configure an explicit transcription input endpoint and bucket")
        self._bucket = bucket
        self._http: _FixedOriginHTTP | None = None
        if _client is not None:
            self._client = _client
        else:
            self._http = _FixedOriginHTTP(endpoint)
            self._client = Minio(
                parsed.netloc,
                access_key=access_key,
                secret_key=secret_key,
                secure=parsed.scheme == "https",
                region="us-east-1",
                http_client=self._http,
            )

    def close(self) -> None:
        if self._http is not None:
            self._http.clear()

    def put(self, plan: TranscriptionPlan) -> StagedAudio:
        digest = "sha256:" + hashlib.sha256(plan.audio).hexdigest()
        hexdigest = digest.removeprefix("sha256:")
        key = f"private/transcription-inputs/{plan.workspace_id}/{hexdigest}.audio"
        asset_id = "transcription-input-" + hexdigest[:32]
        try:
            result = self._client.put_object(
                self._bucket,
                key,
                io.BytesIO(plan.audio),
                len(plan.audio),
                content_type=plan.audio_media_type,
                num_parallel_uploads=1,
                metadata={
                    "nexus-transcription-digest": digest,
                    "nexus-transcription-bytes": str(len(plan.audio)),
                },
            )
            version = result.version_id
            if not version or version == "null":
                raise ValueError
            stat = self._client.stat_object(self._bucket, key, version_id=version)
            if (
                stat.version_id != version
                or stat.size != len(plan.audio)
                or stat.content_type != plan.audio_media_type
            ):
                raise ValueError
            response = self._client.get_object(
                self._bucket, key, offset=0, length=len(plan.audio), version_id=version
            )
            try:
                if (
                    response.status != 206
                    or response.headers.get("x-amz-version-id") != version
                    or response.headers.get("content-range")
                    != f"bytes 0-{len(plan.audio) - 1}/{len(plan.audio)}"
                    or response.headers.get("content-length") != str(len(plan.audio))
                ):
                    raise ValueError
                body = response.read(len(plan.audio) + 1)
            finally:
                response.close()
                response.release_conn()
            if (
                not isinstance(body, bytes)
                or len(body) != len(plan.audio)
                or "sha256:" + hashlib.sha256(body).hexdigest() != digest
            ):
                raise ValueError
        except S3Error, ValueError, TypeError, OSError:
            # A write confirmation may have been lost. Do not retry or claim a
            # readable reference; the request never reaches admission.
            raise TranscriptionUnavailableError(
                "Transcription input storage did not confirm an immutable object"
            ) from None
        except Exception:
            raise TranscriptionUnavailableError(
                "Transcription input storage is unavailable"
            ) from None
        return StagedAudio(
            asset_id=asset_id,
            workspace_id=plan.workspace_id,
            bucket=self._bucket,
            object_key=key,
            content_sha256=digest,
            byte_length=len(plan.audio),
        )


__all__ = ["MinioTranscriptionInputs"]
