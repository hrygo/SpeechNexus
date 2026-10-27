"""Fixed-origin, version-pinned reads of committed Artifact bytes."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from urllib.parse import urlsplit

from minio import Minio
from minio.error import S3Error
from urllib3.response import BaseHTTPResponse

from speechnexus_backend.speech.domain.artifacts import ArtifactError, ArtifactRecord
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


__all__ = ["MinioArtifactObjects"]
