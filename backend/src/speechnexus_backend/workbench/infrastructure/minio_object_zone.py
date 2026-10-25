"""C-owned private object zone: its own bucket prefix, no shared speech object namespace.

The zone holds only the workbench's committed copies. It never creates the bucket, never
follows a redirect to another origin, and never accepts a URL, key or credential from a
workbench command. Blocking SDK calls are offloaded so no event loop is blocked and no
database transaction is held across object I/O.
"""

from __future__ import annotations

import asyncio
import hashlib
import tempfile
import threading
import time
from typing import Any, BinaryIO
from urllib.parse import urlsplit

import anyio.to_thread
import urllib3
from minio import Minio
from urllib3.response import BaseHTTPResponse
from urllib3.util import Retry, Timeout

from speechnexus_backend.workbench.application.downloads import DownloadContent, DownloadError
from speechnexus_backend.workbench.application.saved_assets import ObjectZoneUnavailableError

MAX_ERASED_OBJECTS = 10_000
# A bounded sweep: an object written while a pass is listing lands after that listing, so
# one pass can leave a late object under a prefix that was already reported erased.
MAX_ERASE_PASSES = 5
_CONTENT_TYPE = "application/octet-stream"


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
            timeout=Timeout(connect=3, read=10),
            retries=Retry(total=0, connect=0, read=0, redirect=0, status=0),
        )

    def urlopen(self, method: str, url: str, *args: Any, **kw: Any) -> BaseHTTPResponse:
        if _origin(url) != self._origin:
            # Neither SDK redirects nor a rewound pool may widen the configured origin.
            raise ObjectZoneUnavailableError
        kw["pool_timeout"] = 3
        kw["redirect"] = False
        return super().urlopen(method, url, *args, **kw)


class MinioObjectZone:
    """Opaque private objects under one dedicated prefix of the configured bucket."""

    def __init__(
        self,
        endpoint: str,
        bucket: str,
        access_key: str,
        secret_key: str,
        *,
        prefix: str = "saved",
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
            or not prefix
            or prefix.strip("/") != prefix
            or any(
                character not in "abcdefghijklmnopqrstuvwxyz0123456789-_/" for character in prefix
            )
        ):
            raise ValueError("configure an explicit workbench object zone")
        self._bucket = bucket
        self._prefix = prefix
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

    def _name(self, key: str) -> str:
        if not key or key.startswith("/") or ".." in key.split("/") or "\x00" in key:
            # Keys are derived from project ids and digests, never from request text.
            raise ValueError("invalid workbench object key")
        return f"{self._prefix}/{key}"

    def _put(self, key: str, body: BinaryIO, length: int) -> None:
        name = self._name(key)
        self._client.put_object(
            self._bucket,
            name,
            body,
            length,
            content_type=_CONTENT_TYPE,
            num_parallel_uploads=1,
        )
        if self._client.stat_object(self._bucket, name).size != length:
            raise ObjectZoneUnavailableError

    async def put(self, *, key: str, body: BinaryIO, length: int) -> None:
        if type(length) is not int or length < 1:
            raise ValueError("invalid workbench object length")
        try:
            await anyio.to_thread.run_sync(self._put, key, body, length)
        except ObjectZoneUnavailableError:
            raise
        except Exception:
            # Never echo driver text: it can contain the endpoint, bucket or object name.
            raise ObjectZoneUnavailableError from None

    def _erase(self, prefix: str) -> int:
        name = self._name(prefix.rstrip("/") + "/")
        erased = 0
        for _pass in range(MAX_ERASE_PASSES):
            pass_erased = 0
            for item in self._client.list_objects(self._bucket, prefix=name, recursive=True):
                if erased >= MAX_ERASED_OBJECTS:
                    raise ObjectZoneUnavailableError
                self._client.remove_object(self._bucket, item.object_name)
                erased += 1
                pass_erased += 1
            if pass_erased == 0:
                break
        return erased

    async def erase_prefix(self, *, prefix: str) -> int:
        try:
            return await anyio.to_thread.run_sync(self._erase, prefix)
        except ObjectZoneUnavailableError:
            raise
        except Exception:
            raise ObjectZoneUnavailableError from None


async def _join(task: asyncio.Task[Any]) -> bool:
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
        except Exception:
            break
    # Retrieve exceptions even after timeout/disconnect; do not log private SDK errors.
    if not task.cancelled():
        task.exception()
    return cancelled


class _VerifiedFile:
    def __init__(self, body: tempfile.SpooledTemporaryFile[bytes]) -> None:
        self._body = body

    def _read(self, offset: int, size: int) -> bytes:
        self._body.seek(offset)
        return self._body.read(size)

    async def read(self, offset: int, size: int) -> bytes:
        task = asyncio.create_task(asyncio.to_thread(self._read, offset, size))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            await _join(task)
            raise

    async def close(self) -> None:
        self._body.close()


class MinioDownloadReader:
    """Full verification into a bounded, private, unlinked spool before HTTP headers.

    The SDK's fixed-origin connect/read/pool timeouts still apply. Cooperative total
    time/cancellation checks run between reads; a native read may exceed the total
    deadline until its socket timeout. Never return while that read owns the spool.
    """

    def __init__(self, zone: MinioObjectZone, *, max_bytes: int, timeout_seconds: float) -> None:
        if max_bytes < 1 or timeout_seconds <= 0:
            raise ValueError("invalid download limits")
        self._zone, self._max_bytes, self._timeout = zone, max_bytes, timeout_seconds

    def _copy(
        self,
        content: DownloadContent,
        body: tempfile.SpooledTemporaryFile[bytes],
        stopped: threading.Event,
        deadline: float,
    ) -> None:
        response = None
        try:
            response = self._zone._client.get_object(
                self._zone._bucket, self._zone._name(content.key)
            )
            size, digest = 0, hashlib.sha256()
            while True:
                if stopped.is_set() or time.monotonic() >= deadline:
                    raise DownloadError("download_unavailable")
                # Read at most one byte beyond the declared length to detect excess data.
                chunk = response.read(min(64 * 1024, content.size - size + 1))
                if not chunk:
                    break
                size += len(chunk)
                if size > content.size:
                    raise DownloadError("download_unavailable")
                digest.update(chunk)
                body.write(chunk)
            if size != content.size or "sha256:" + digest.hexdigest() != content.sha256:
                raise DownloadError("download_unavailable")
            body.seek(0)
        finally:
            if response is not None:
                try:
                    response.close()
                finally:
                    response.release_conn()

    async def prepare(self, content: DownloadContent) -> _VerifiedFile:
        if content.size > self._max_bytes:
            raise DownloadError("download_too_large")
        # Ownership transfers to the response; all failed preparation paths close it below.
        body = tempfile.SpooledTemporaryFile(  # noqa: SIM115
            max_size=min(1024**2, self._max_bytes), mode="w+b"
        )
        stopped = threading.Event()
        task = asyncio.create_task(
            asyncio.to_thread(
                self._copy,
                content,
                body,
                stopped,
                time.monotonic() + self._timeout,
            )
        )
        try:
            done, _ = await asyncio.wait({task}, timeout=self._timeout)
            if not done:
                raise DownloadError("download_unavailable")
            task.result()
            return _VerifiedFile(body)
        except BaseException as error:
            stopped.set()
            cancelled = await _join(task)
            body.close()
            if isinstance(error, asyncio.CancelledError) or cancelled:
                raise asyncio.CancelledError from None
            if not isinstance(error, Exception):
                raise
            raise DownloadError("download_unavailable") from None
