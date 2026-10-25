"""Operation-scoped async S3 writes/readback with total deadlines and cancellation."""

from __future__ import annotations

import asyncio
import hashlib
import ssl
from collections.abc import Callable
from urllib.parse import quote

import httpx
from minio.credentials.credentials import Credentials
from minio.helpers import check_bucket_name

from speechnexus_gateway.domain.asr import AsrCallError
from speechnexus_gateway.domain.asr_results import (
    ASR_RESULT_MEDIA_TYPE,
    MAX_ASR_RESULT_BYTES,
    AsrResultError,
    PreparedAsrResult,
    StagedAsrResult,
)
from speechnexus_gateway.domain.execution_inputs import (
    ExecutionInputLimits,
    ExecutionObjectReference,
)
from speechnexus_gateway.infrastructure.bounded_io import run_bounded_io
from speechnexus_gateway.infrastructure.private_s3 import (
    FixedS3Transport,
    private_endpoint,
    signed_headers,
)


class _FixedS3Transport(FixedS3Transport):
    def __init__(self, endpoint: str, context: ssl.SSLContext | None) -> None:
        super().__init__(
            endpoint,
            context,
            methods=frozenset({"PUT", "HEAD", "GET"}),
            scope_error=lambda: AsrResultError("asr_result_storage_unknown"),
        )


class MinioAsrResults:
    """Each stage owns its client; cancellation closes no concurrent operation's I/O."""

    def __init__(
        self,
        *,
        endpoint: str,
        bucket: str,
        access_key: str,
        secret_key: str,
        timeout_seconds: float,
        ssl_context: ssl.SSLContext | None = None,
    ) -> None:
        self._limits = ExecutionInputLimits(MAX_ASR_RESULT_BYTES, 64 * 1024, timeout_seconds)
        try:
            self._netloc = private_endpoint(endpoint, ssl_context)
            check_bucket_name(bucket, strict=True)
            self._credentials = Credentials(access_key, secret_key)
        except Exception:
            raise ValueError(
                "configure a fixed private result origin, bucket and credentials"
            ) from None
        self._endpoint = endpoint.rstrip("/")
        self._bucket = bucket
        self._context = ssl_context or ssl.create_default_context()

    def close(self) -> None:
        """No retained client: each awaited stage closes its own transport."""

    def _new_client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=_FixedS3Transport(self._endpoint, self._context),
            timeout=httpx.Timeout(3),
            trust_env=False,
            follow_redirects=False,
        )

    def _url(self, reference: ExecutionObjectReference, version: str | None = None) -> str:
        url = self._endpoint + "/" + self._bucket + "/" + reference.object_key
        return url if version is None else url + "?versionId=" + quote(version, safe="")

    def _headers(self, method: str, url: str, body: bytes = b"") -> dict[str, str]:
        return signed_headers(
            method,
            url,
            netloc=self._netloc,
            credentials=self._credentials,
            body=body,
            media_type=ASR_RESULT_MEDIA_TYPE,
        )

    async def stage(
        self, result: PreparedAsrResult, *, cancelled: Callable[[], bool]
    ) -> StagedAsrResult:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._limits.timeout_seconds

        def check() -> None:
            if cancelled():
                raise AsrResultError("asr_result_cancelled")
            if loop.time() >= deadline:
                raise AsrResultError("asr_result_deadline_exceeded")

        try:
            return await run_bounded_io(
                lambda: self._stage(result, check), deadline=deadline, check=check
            )
        except TimeoutError:
            raise AsrResultError("asr_result_deadline_exceeded") from None

    async def _stage(self, result: PreparedAsrResult, check: Callable[[], None]) -> StagedAsrResult:
        reference = result.reference(self._bucket)
        phase_error = "asr_result_storage_unknown"
        try:
            async with self._new_client() as client:
                version = await self._put(client, result, reference, check)
                receipt = StagedAsrResult(result.request_digest, reference, version)
                phase_error = "asr_result_readback_failed"
                await self._readback(client, receipt, check)
                check()
                return receipt
        except AsrResultError, AsrCallError:
            raise
        except Exception:
            check()
            raise AsrResultError(phase_error) from None

    async def _put(
        self,
        client: httpx.AsyncClient,
        result: PreparedAsrResult,
        reference: ExecutionObjectReference,
        check: Callable[[], None],
    ) -> str:
        check()
        url = self._url(reference)
        try:
            async with client.stream(
                "PUT", url, content=result.body, headers=self._headers("PUT", url, result.body)
            ) as response:
                check()
                if response.status_code != 200:
                    raise AsrResultError("asr_result_storage_unknown")
                versions = response.headers.get_list("x-amz-version-id")
                if len(versions) != 1 or not versions[0] or versions[0] == "null":
                    raise AsrResultError("asr_result_versioning_required")
                version = versions[0]
            check()
            return version
        except AsrResultError, AsrCallError:
            raise
        except Exception:
            # Includes transport ExecutionInputError and response-close failures:
            # no readback has started and the PUT may already have committed.
            check()
            raise AsrResultError("asr_result_storage_unknown") from None

    async def _readback(
        self, client: httpx.AsyncClient, receipt: StagedAsrResult, check: Callable[[], None]
    ) -> None:
        reference = receipt.reference
        try:
            url = self._url(reference)
            check()
            async with client.stream("HEAD", url, headers=self._headers("HEAD", url)) as response:
                check()
                self._check_read_headers(response, receipt)
            url = self._url(reference, receipt.storage_version)
            check()
            async with client.stream("GET", url, headers=self._headers("GET", url)) as response:
                check()
                self._check_read_headers(response, receipt)
                digest, length = hashlib.sha256(), 0
                async for chunk in response.aiter_raw(chunk_size=self._limits.memory_bytes):
                    check()
                    length += len(chunk)
                    if length > reference.byte_length:
                        raise AsrResultError("asr_result_readback_failed")
                    digest.update(chunk)
                check()
                if (
                    length != reference.byte_length
                    or "sha256:" + digest.hexdigest() != reference.content_sha256
                ):
                    raise AsrResultError("asr_result_readback_failed")
        except AsrResultError, AsrCallError:
            raise
        except Exception:
            check()
            raise AsrResultError("asr_result_readback_failed") from None

    @staticmethod
    def _check_read_headers(response: httpx.Response, receipt: StagedAsrResult) -> None:
        if (
            response.status_code != 200
            or response.headers.get_list("Content-Length") != [str(receipt.reference.byte_length)]
            or response.headers.get_list("Content-Encoding") not in ([], ["identity"])
        ):
            raise AsrResultError("asr_result_readback_failed")
        if response.headers.get_list("x-amz-version-id") != [receipt.storage_version]:
            raise AsrResultError("asr_result_identity_mismatch")
