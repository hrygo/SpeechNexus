"""Fixed-origin, version-pinned reads and writes of committed Artifact bytes."""

from __future__ import annotations

import io
from collections.abc import Iterator
from contextlib import contextmanager
from urllib.parse import urlsplit

from minio import Minio
from minio.commonconfig import ENABLED
from minio.error import S3Error
from urllib3.response import BaseHTTPResponse

from speechnexus_backend.speech.domain.artifacts import (
    ArtifactError,
    ArtifactObjectWrite,
    ArtifactRecord,
)
from speechnexus_backend.speech.domain.assets import AssetError
from speechnexus_backend.speech.infrastructure.assets_minio import _FixedOriginHTTP


class _ObjectReader:
    def __init__(self, response: BaseHTTPResponse) -> None:
        self._response = response

    def read(self, size: int) -> bytes:
        try:
            return self._response.read(size)
        except Exception:
            raise ArtifactError("artifact_unavailable") from None


class MinioArtifactObjects:
    def __init__(self, endpoint: str, bucket: str, access_key: str, secret_key: str) -> None:
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
            raise ValueError("Configure an explicit object endpoint and credentials")
        try:
            self._http = _FixedOriginHTTP(endpoint)
        except AssetError:
            raise ValueError("Invalid Artifact object endpoint") from None
        self._bucket = bucket
        self._client = Minio(
            parsed.netloc,
            access_key=access_key,
            secret_key=secret_key,
            secure=parsed.scheme == "https",
            region="us-east-1",
            http_client=self._http,
        )

    def close(self) -> None:
        self._http.clear()

    @contextmanager
    def open(self, artifact: ArtifactRecord, offset: int, length: int) -> Iterator[_ObjectReader]:
        try:
            response = self._client.get_object(
                self._bucket,
                artifact.object_key,
                offset=offset,
                length=length,
                version_id=artifact.storage_version_id,
            )
        except S3Error as error:
            if error.code in ("NoSuchKey", "NoSuchObject", "NoSuchVersion"):
                raise ArtifactError("artifact_unavailable") from None
            raise ArtifactError("artifact_storage_unavailable") from None
        except Exception:
            raise ArtifactError("artifact_storage_unavailable") from None
        try:
            if (
                response.status != 206
                or response.headers.get("x-amz-version-id") != artifact.storage_version_id
                or response.headers.get("content-range")
                != f"bytes {offset}-{offset + length - 1}/{artifact.bytes}"
                or response.headers.get("content-length") != str(length)
            ):
                raise ArtifactError("artifact_unavailable")
            yield _ObjectReader(response)
        finally:
            response.close()
            response.release_conn()


class MinioArtifactPublisher(MinioArtifactObjects):
    """Write one immutable object, reusing an identical version after a retry."""

    def check_writes_available(self) -> None:
        try:
            config = self._client.get_bucket_versioning(self._bucket)
            if config.status != ENABLED:
                raise ArtifactError("artifact_storage_versioning_required")
        except ArtifactError:
            raise
        except Exception:
            raise ArtifactError("artifact_storage_unavailable") from None

    def put_verified(self, write: ArtifactObjectWrite) -> str:
        try:
            existing = self._existing_version(write)
            if existing is not None:
                return existing
            result = self._client.put_object(
                self._bucket,
                write.object_key,
                io.BytesIO(write.body),
                write.bytes,
                content_type=write.media_type,
                num_parallel_uploads=1,
                metadata={
                    "nexus-artifact-id": write.artifact_id,
                    "nexus-artifact-sha256": write.sha256,
                    "nexus-workspace-id": write.workspace_id,
                },
            )
            version_id = result.version_id
            if not version_id or version_id == "null":
                raise ArtifactError("artifact_storage_versioning_required")
            self._verify(write, version_id)
            return version_id
        except ArtifactError:
            raise
        except Exception:
            raise ArtifactError("artifact_storage_unavailable") from None

    def _existing_version(self, write: ArtifactObjectWrite) -> str | None:
        try:
            stat = self._client.stat_object(self._bucket, write.object_key)
        except S3Error as error:
            if error.code in ("NoSuchKey", "NoSuchObject", "NoSuchVersion"):
                return None
            raise ArtifactError("artifact_storage_unavailable") from None
        except Exception:
            raise ArtifactError("artifact_storage_unavailable") from None
        if not stat.version_id or stat.version_id == "null":
            raise ArtifactError("artifact_storage_versioning_required")
        metadata = {key.lower(): value for key, value in (stat.metadata or {}).items()}
        if (
            stat.size != write.bytes
            or metadata.get("x-amz-meta-nexus-artifact-id") != write.artifact_id
            or metadata.get("x-amz-meta-nexus-artifact-sha256") != write.sha256
            or metadata.get("x-amz-meta-nexus-workspace-id") != write.workspace_id
        ):
            raise ArtifactError("artifact_object_conflict")
        self._verify(write, stat.version_id)
        return stat.version_id

    def _verify(self, write: ArtifactObjectWrite, version_id: str) -> None:
        stat = self._client.stat_object(self._bucket, write.object_key, version_id=version_id)
        metadata = {key.lower(): value for key, value in (stat.metadata or {}).items()}
        if (
            stat.size != write.bytes
            or metadata.get("x-amz-meta-nexus-artifact-sha256") != write.sha256
        ):
            raise ArtifactError("artifact_object_integrity_mismatch")
        response = self._client.get_object(
            self._bucket, write.object_key, offset=0, length=write.bytes, version_id=version_id
        )
        try:
            if (
                response.status != 206
                or response.headers.get("x-amz-version-id") != version_id
                or response.headers.get("content-range")
                != f"bytes 0-{write.bytes - 1}/{write.bytes}"
                or response.headers.get("content-length") != str(write.bytes)
            ):
                raise ArtifactError("artifact_object_integrity_mismatch")
            body = response.read(write.bytes + 1)
        finally:
            response.close()
            response.release_conn()
        if not isinstance(body, bytes) or body != write.body:
            raise ArtifactError("artifact_object_integrity_mismatch")


__all__ = ["MinioArtifactObjects", "MinioArtifactPublisher"]
