"""Shared versioned PUT/readback mechanics; no result semantics or retries."""

import hashlib
import ssl
from collections.abc import Callable
from urllib.parse import quote

import httpx
from minio.credentials.credentials import Credentials
from minio.helpers import check_bucket_name

from speechnexus_gateway.domain.execution_inputs import ExecutionObjectReference
from speechnexus_gateway.infrastructure.private_s3 import (
    FixedS3Transport,
    private_endpoint,
    signed_headers,
)


class PrivateResultIOError(ValueError):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class PrivateResultObjects:
    """One fixed private origin, with a separate HTTP client per operation."""

    def __init__(
        self,
        *,
        endpoint: str,
        bucket: str,
        access_key: str,
        secret_key: str,
        ssl_context: ssl.SSLContext | None = None,
    ) -> None:
        try:
            self._netloc = private_endpoint(endpoint, ssl_context)
            check_bucket_name(bucket, strict=True)
            self._credentials = Credentials(access_key, secret_key)
        except Exception:
            raise ValueError(
                "configure a fixed private result origin, bucket and credentials"
            ) from None
        self.endpoint = endpoint.rstrip("/")
        self.bucket = bucket
        self.context = ssl_context or ssl.create_default_context()

    def new_client(self, *, scope_error: Callable[[], Exception]) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=FixedS3Transport(
                self.endpoint,
                self.context,
                methods=frozenset({"PUT", "HEAD", "GET"}),
                scope_error=scope_error,
            ),
            timeout=httpx.Timeout(3),
            trust_env=False,
            follow_redirects=False,
        )

    def _url(self, reference: ExecutionObjectReference, version: str | None = None) -> str:
        url = self.endpoint + "/" + self.bucket + "/" + reference.object_key
        return url if version is None else url + "?versionId=" + quote(version, safe="")

    def _headers(self, method: str, url: str, media_type: str, body: bytes = b"") -> dict[str, str]:
        return signed_headers(
            method,
            url,
            netloc=self._netloc,
            credentials=self._credentials,
            body=body,
            media_type=media_type,
        )

    async def put(
        self,
        client: httpx.AsyncClient,
        *,
        body: bytes,
        reference: ExecutionObjectReference,
        media_type: str,
        check: Callable[[], None],
    ) -> str:
        check()
        url = self._url(reference)
        async with client.stream(
            "PUT", url, content=body, headers=self._headers("PUT", url, media_type, body)
        ) as response:
            check()
            if response.status_code != 200:
                raise PrivateResultIOError("storage_unknown")
            versions = response.headers.get_list("x-amz-version-id")
            if len(versions) != 1 or not versions[0] or versions[0] == "null":
                raise PrivateResultIOError("versioning_required")
            version = versions[0]
        check()
        return version

    async def readback(
        self,
        client: httpx.AsyncClient,
        *,
        reference: ExecutionObjectReference,
        storage_version: str,
        media_type: str,
        check: Callable[[], None],
    ) -> None:
        url = self._url(reference)
        check()
        async with client.stream(
            "HEAD", url, headers=self._headers("HEAD", url, media_type)
        ) as response:
            check()
            self._check_read_headers(response, reference, storage_version)
        url = self._url(reference, storage_version)
        check()
        async with client.stream(
            "GET", url, headers=self._headers("GET", url, media_type)
        ) as response:
            check()
            self._check_read_headers(response, reference, storage_version)
            digest, length = hashlib.sha256(), 0
            async for chunk in response.aiter_raw(chunk_size=64 * 1024):
                check()
                length += len(chunk)
                if length > reference.byte_length:
                    raise PrivateResultIOError("readback_failed")
                digest.update(chunk)
            check()
            if (
                length != reference.byte_length
                or "sha256:" + digest.hexdigest() != reference.content_sha256
            ):
                raise PrivateResultIOError("readback_failed")

    @staticmethod
    def _check_read_headers(
        response: httpx.Response, reference: ExecutionObjectReference, storage_version: str
    ) -> None:
        if (
            response.status_code != 200
            or response.headers.get_list("Content-Length") != [str(reference.byte_length)]
            or response.headers.get_list("Content-Encoding") not in ([], ["identity"])
        ):
            raise PrivateResultIOError("readback_failed")
        if response.headers.get_list("x-amz-version-id") != [storage_version]:
            raise PrivateResultIOError("identity_mismatch")
