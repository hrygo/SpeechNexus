"""Fixed-origin, version-pinned MinIO/S3 object I/O; no metadata transactions."""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any, BinaryIO
from urllib.parse import urlsplit

import urllib3
from minio import Minio
from minio.commonconfig import ENABLED
from minio.error import S3Error
from urllib3.response import BaseHTTPResponse
from urllib3.util import Retry, Timeout

from speechnexus_backend.speech.domain.assets import (
    MAX_ASSET_BYTES,
    AssetError,
    AssetRecord,
    VerifiedObject,
)


def _origin(url: str) -> tuple[str, str | None, int]:
    parsed = urlsplit(url)
    return parsed.scheme, parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80)


class _FixedOriginHTTP(urllib3.PoolManager):
    def __init__(self, endpoint: str) -> None:
        self._origin = _origin(endpoint)
        super().__init__(
            num_pools=1,
            maxsize=4,
            block=True,
            timeout=Timeout(connect=3, read=3),
            retries=Retry(total=0, connect=0, read=0, redirect=0, status=0),
        )

    def urlopen(self, method: str, url: str, *args: Any, **kw: Any) -> BaseHTTPResponse:
        if _origin(url) != self._origin:
            raise AssetError("asset_storage_origin_rejected")
        # MinIO's SDK delegates to urllib3, whose default otherwise follows 307s
        # to another host. Neither SDK redirects nor pool waits may widen scope.
        kw["pool_timeout"] = 3
        kw["redirect"] = False
        return super().urlopen(method, url, *args, **kw)


class _ObjectReader:
    def __init__(self, response: BaseHTTPResponse) -> None:
        self._response = response

    def read(self, size: int) -> bytes:
        try:
            return self._response.read(size)
        except Exception:
            raise AssetError("asset_content_incomplete") from None


class MinioAssetObjects:
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
            or not access_key
            or not secret_key
        ):
            raise ValueError("Configure an explicit object endpoint and credentials")
        self._bucket = bucket
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
        self._http.clear()

    def check_writes_available(self) -> None:
        try:
            config = self._client.get_bucket_versioning(self._bucket)
            if config.status != ENABLED:
                raise AssetError("asset_storage_versioning_required")
        except AssetError:
            raise
        except Exception:
            raise AssetError("asset_storage_unavailable") from None

    def put_verified(self, asset: AssetRecord, body: BinaryIO) -> VerifiedObject:
        if asset.phase != "writing" or asset.writer_id is None:
            raise AssetError("asset_write_not_authorized")
        if asset.bytes > MAX_ASSET_BYTES:
            raise AssetError("asset_too_large")
        try:
            result = self._client.put_object(
                self._bucket,
                asset.object_key,
                body,
                asset.bytes,
                content_type=asset.media.media_type,
                num_parallel_uploads=1,
                metadata={
                    "nexus-asset-id": asset.asset_id,
                    "nexus-upload-digest": asset.request_digest,
                    "nexus-writer-id": asset.writer_id,
                },
            )
            if not result.version_id or result.version_id == "null":
                raise AssetError("asset_storage_versioning_required")
            return self._verify(asset, result.version_id)
        except AssetError:
            raise
        except Exception:
            raise AssetError("asset_upload_unknown") from None

    def inspect_original(self, asset: AssetRecord) -> VerifiedObject | None:
        try:
            stat = self._client.stat_object(self._bucket, asset.object_key)
            if not stat.version_id or stat.version_id == "null":
                raise AssetError("asset_storage_versioning_required")
            return self._verify(asset, stat.version_id)
        except S3Error as error:
            if error.code in ("NoSuchKey", "NoSuchObject", "NoSuchVersion"):
                return None  # Absence is not permission to issue another PUT.
            raise AssetError("asset_storage_unavailable") from None
        except AssetError:
            raise
        except Exception:
            raise AssetError("asset_storage_unavailable") from None

    def _verify(self, asset: AssetRecord, version_id: str) -> VerifiedObject:
        if asset.writer_id is None:
            raise AssetError("asset_write_not_authorized")
        stat = self._client.stat_object(self._bucket, asset.object_key, version_id=version_id)
        metadata = {key.lower(): value for key, value in (stat.metadata or {}).items()}
        expected = {
            "x-amz-meta-nexus-asset-id": asset.asset_id,
            "x-amz-meta-nexus-upload-digest": asset.request_digest,
            "x-amz-meta-nexus-writer-id": asset.writer_id,
        }
        if stat.size != asset.bytes or any(metadata.get(k) != v for k, v in expected.items()):
            raise AssetError("asset_object_integrity_mismatch")
        response = self._client.get_object(self._bucket, asset.object_key, version_id=version_id)
        try:
            if response.headers.get("x-amz-version-id") != version_id:
                raise AssetError("asset_object_integrity_mismatch")
            size, sha = 0, hashlib.sha256()
            while chunk := response.read(65536):
                size += len(chunk)
                if size > asset.bytes:
                    raise AssetError("asset_object_integrity_mismatch")
                sha.update(chunk)
            if size != asset.bytes or "sha256:" + sha.hexdigest() != asset.sha256:
                raise AssetError("asset_object_integrity_mismatch")
        finally:
            response.close()
            response.release_conn()
        proof = VerifiedObject(
            asset.asset_id, asset.object_key, version_id, asset.bytes, asset.sha256, asset.writer_id
        )
        proof.require_original(asset)
        return proof

    @contextmanager
    def open(self, proof: VerifiedObject, offset: int, length: int) -> Iterator[_ObjectReader]:
        try:
            response = self._client.get_object(
                self._bucket,
                proof.object_key,
                offset=offset,
                length=length,
                version_id=proof.version_id,
            )
        except Exception:
            raise AssetError("asset_unavailable") from None
        try:
            if (
                response.status != 206
                or response.headers.get("x-amz-version-id") != proof.version_id
                or response.headers.get("content-range")
                != f"bytes {offset}-{offset + length - 1}/{proof.bytes}"
                or response.headers.get("content-length") != str(length)
            ):
                raise AssetError("asset_object_integrity_mismatch")
            yield _ObjectReader(response)
        finally:
            response.close()
            response.release_conn()

    def delete_version(self, proof: VerifiedObject) -> None:
        try:
            self._client.remove_object(self._bucket, proof.object_key, version_id=proof.version_id)
            try:
                self._client.stat_object(
                    self._bucket, proof.object_key, version_id=proof.version_id
                )
            except S3Error as error:
                if error.code in ("NoSuchKey", "NoSuchObject", "NoSuchVersion"):
                    return
                raise
        except Exception:
            raise AssetError("asset_cleanup_unconfirmed") from None
        raise AssetError("asset_cleanup_unconfirmed")
