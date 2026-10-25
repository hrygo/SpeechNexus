"""Cancellable version-pinned S3 reads; expose only a complete private spool."""

from __future__ import annotations

import asyncio
import hashlib
import os
import ssl
import stat
import tempfile
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import BinaryIO, cast
from urllib.parse import quote

import httpx
from minio.credentials.credentials import Credentials
from minio.helpers import check_bucket_name

from speechnexus_gateway.domain.asr import AsrCallError
from speechnexus_gateway.domain.execution_inputs import (
    ExecutionInputError,
    ExecutionInputLimits,
    ExecutionObjectReference,
    VerifiedExecutionObject,
    is_fixed_storage_version,
)
from speechnexus_gateway.infrastructure.bounded_io import run_bounded_io
from speechnexus_gateway.infrastructure.private_s3 import (
    FixedS3Transport,
    private_endpoint,
    signed_headers,
)

_CHUNK_BYTES = 64 * 1024


class _FixedObjectTransport(FixedS3Transport):
    def __init__(self, endpoint: str, context: ssl.SSLContext | None) -> None:
        super().__init__(
            endpoint,
            context,
            methods=frozenset({"HEAD", "GET"}),
            scope_error=lambda: ExecutionInputError("execution_input_origin_rejected"),
        )


class MinioExecutionInputs:
    def __init__(
        self,
        *,
        endpoint: str,
        bucket: str,
        access_key: str,
        secret_key: str,
        spool_directory: Path,
        ssl_context: ssl.SSLContext | None = None,
    ) -> None:
        self._netloc = private_endpoint(endpoint, ssl_context)
        try:
            check_bucket_name(bucket, strict=True)
            info = spool_directory.stat()
            if (
                not spool_directory.is_absolute()
                or not stat.S_ISDIR(info.st_mode)
                or info.st_uid != os.getuid()
                or info.st_mode & 0o077
                or not access_key
                or not secret_key
            ):
                raise ValueError
            self._credentials = Credentials(access_key, secret_key)
        except Exception:
            raise ValueError(
                "configure an explicit private object input and spool directory"
            ) from None
        self._endpoint = endpoint.rstrip("/")
        self._bucket = bucket
        self._directory = spool_directory
        self._context = ssl_context or ssl.create_default_context()

    def close(self) -> None:
        """No retained client: every input scope owns and closes its transport."""

    def _new_client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=_FixedObjectTransport(self._endpoint, self._context),
            timeout=httpx.Timeout(3),
            trust_env=False,
            follow_redirects=False,
        )

    def _url(self, reference: ExecutionObjectReference, version: str | None = None) -> str:
        # Keep opaque keys opaque. A literal '%' must not become a URL escape.
        url = self._endpoint + "/" + self._bucket + "/" + quote(reference.object_key, safe="/~")
        return url if version is None else url + "?versionId=" + quote(version, safe="")

    def _headers(self, method: str, url: str) -> dict[str, str]:
        return signed_headers(method, url, netloc=self._netloc, credentials=self._credentials)

    @asynccontextmanager
    async def open_verified(
        self,
        reference: ExecutionObjectReference,
        *,
        limits: ExecutionInputLimits,
        cancelled: Callable[[], bool],
    ) -> AsyncIterator[VerifiedExecutionObject]:
        if reference.storage_bucket != self._bucket:
            raise ExecutionInputError("execution_input_scope_mismatch")
        if (
            not reference.object_key.startswith("private/")
            or any(part in {"", ".", ".."} for part in reference.object_key.split("/"))
            or any(char in reference.object_key for char in "\x00\\?#")
            or any(ord(char) < 32 or ord(char) == 127 for char in reference.object_key)
        ):
            raise ExecutionInputError("execution_input_scope_mismatch")
        if reference.byte_length > limits.max_bytes:
            raise ExecutionInputError("execution_input_limit_exceeded")
        loop = asyncio.get_running_loop()
        deadline = loop.time() + limits.timeout_seconds

        def check() -> None:
            if cancelled():
                raise ExecutionInputError("execution_input_cancelled")
            if loop.time() >= deadline:
                raise ExecutionInputError("execution_input_deadline_exceeded")

        check()
        with tempfile.SpooledTemporaryFile(
            mode="w+b", max_size=limits.memory_bytes, dir=str(self._directory)
        ) as spool:
            try:
                version = await run_bounded_io(
                    lambda: self._read(reference, cast(BinaryIO, spool), check),
                    deadline=deadline,
                    check=check,
                )
            except TimeoutError:
                raise ExecutionInputError("execution_input_deadline_exceeded") from None
            check()
            spool.seek(0)
            # All HTTP work and cleanup has ended before exposing any content.
            yield VerifiedExecutionObject(reference, cast(BinaryIO, spool), version)

    async def _read(
        self, reference: ExecutionObjectReference, spool: BinaryIO, check: Callable[[], None]
    ) -> str:
        try:
            async with self._new_client() as client:
                version = await self._head(client, reference, check)
                url = self._url(reference, version)
                check()
                async with client.stream("GET", url, headers=self._headers("GET", url)) as response:
                    check()
                    self._check_headers(response, reference)
                    if response.headers.get_list("x-amz-version-id") != [version]:
                        raise ExecutionInputError("execution_input_identity_mismatch")
                    digest, total = hashlib.sha256(), 0
                    async for chunk in response.aiter_raw(chunk_size=_CHUNK_BYTES):
                        check()
                        total += len(chunk)
                        if total > reference.byte_length:
                            raise ExecutionInputError("execution_input_identity_mismatch")
                        digest.update(chunk)
                        spool.write(chunk)
                    check()
                    if (
                        total != reference.byte_length
                        or "sha256:" + digest.hexdigest() != reference.content_sha256
                    ):
                        raise ExecutionInputError("execution_input_identity_mismatch")
                check()
            check()
            return version
        except ExecutionInputError, AsrCallError:
            raise
        except Exception:
            check()
            raise ExecutionInputError("execution_input_unavailable") from None

    async def _head(
        self,
        client: httpx.AsyncClient,
        reference: ExecutionObjectReference,
        check: Callable[[], None],
    ) -> str:
        check()
        url = self._url(reference)
        async with client.stream("HEAD", url, headers=self._headers("HEAD", url)) as response:
            check()
            self._check_headers(response, reference)
            versions = response.headers.get_list("x-amz-version-id")
            if len(versions) != 1 or not is_fixed_storage_version(versions[0]):
                raise ExecutionInputError("execution_input_identity_mismatch")
            version = versions[0]
        check()
        return version

    @staticmethod
    def _check_headers(response: httpx.Response, reference: ExecutionObjectReference) -> None:
        if response.status_code != 200:
            raise ExecutionInputError("execution_input_unavailable")
        if response.headers.get_list("Content-Length") != [
            str(reference.byte_length)
        ] or response.headers.get_list("Content-Encoding") not in ([], ["identity"]):
            raise ExecutionInputError("execution_input_identity_mismatch")
