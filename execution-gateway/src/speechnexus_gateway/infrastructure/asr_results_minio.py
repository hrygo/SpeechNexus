"""Operation-scoped async S3 writes/readback with total deadlines and cancellation."""

from __future__ import annotations

import asyncio
import ssl
from collections.abc import Callable

import httpx

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
from speechnexus_gateway.infrastructure.private_result_objects import (
    PrivateResultIOError,
    PrivateResultObjects,
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
        self._objects = PrivateResultObjects(
            endpoint=endpoint,
            bucket=bucket,
            access_key=access_key,
            secret_key=secret_key,
            ssl_context=ssl_context,
        )
        self._bucket = bucket

    def close(self) -> None:
        """No retained client: each awaited stage closes its own transport."""

    def _new_client(self) -> httpx.AsyncClient:
        return self._objects.new_client(
            scope_error=lambda: AsrResultError("asr_result_storage_unknown")
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
        try:
            return await self._objects.put(
                client,
                body=result.body,
                reference=reference,
                media_type=ASR_RESULT_MEDIA_TYPE,
                check=check,
            )
        except PrivateResultIOError as error:
            raise AsrResultError("asr_result_" + error.reason) from None
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
        try:
            await self._objects.readback(
                client,
                reference=receipt.reference,
                storage_version=receipt.storage_version,
                media_type=ASR_RESULT_MEDIA_TYPE,
                check=check,
            )
        except PrivateResultIOError as error:
            raise AsrResultError("asr_result_" + error.reason) from None
        except AsrResultError, AsrCallError:
            raise
        except Exception:
            check()
            raise AsrResultError("asr_result_readback_failed") from None
