"""C-owned private object zone: its own bucket prefix, no shared speech object namespace.

The zone holds only the workbench's committed copies. It never creates the bucket, never
follows a redirect to another origin, and never accepts a URL, key or credential from a
workbench command. Blocking SDK calls are offloaded so no event loop is blocked and no
database transaction is held across object I/O.
"""

from __future__ import annotations

from typing import Any, BinaryIO
from urllib.parse import urlsplit

import anyio.to_thread
import urllib3
from minio import Minio
from urllib3.response import BaseHTTPResponse
from urllib3.util import Retry, Timeout

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
