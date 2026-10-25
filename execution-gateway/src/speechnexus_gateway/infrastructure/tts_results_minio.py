"""Private file-TTS versioned staging using the same verified S3 mechanics as ASR."""

import asyncio
import ssl
from collections.abc import Callable

import httpx

from speechnexus_gateway.domain.execution_inputs import ExecutionInputLimits
from speechnexus_gateway.domain.execution_results import (
    MAX_AUDIO_RESULT_BYTES,
    ExecutionAudioStagingResult,
)
from speechnexus_gateway.domain.tts import TtsCallError, TtsDeliveryError
from speechnexus_gateway.domain.tts_results import PreparedTtsResult, TtsResultError
from speechnexus_gateway.infrastructure.bounded_io import run_bounded_io
from speechnexus_gateway.infrastructure.private_result_objects import (
    PrivateResultIOError,
    PrivateResultObjects,
)


class MinioTtsResults:
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
        self._limits = ExecutionInputLimits(MAX_AUDIO_RESULT_BYTES, 64 * 1024, timeout_seconds)
        self._objects = PrivateResultObjects(
            endpoint=endpoint,
            bucket=bucket,
            access_key=access_key,
            secret_key=secret_key,
            ssl_context=ssl_context,
        )

    def close(self) -> None:
        """Each awaited stage closes its own client; no shared transport to close."""

    def _new_client(self) -> httpx.AsyncClient:
        return self._objects.new_client(
            scope_error=lambda: TtsResultError("tts_result_storage_unknown")
        )

    async def stage(
        self, result: PreparedTtsResult, *, cancelled: Callable[[], bool]
    ) -> ExecutionAudioStagingResult:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._limits.timeout_seconds

        def check() -> None:
            if cancelled():
                raise TtsResultError("tts_result_cancelled")
            if loop.time() >= deadline:
                raise TtsResultError("tts_result_deadline_exceeded")

        try:
            return await run_bounded_io(
                lambda: self._stage(result, check), deadline=deadline, check=check
            )
        except TimeoutError:
            raise TtsResultError("tts_result_deadline_exceeded") from None

    async def _stage(
        self, result: PreparedTtsResult, check: Callable[[], None]
    ) -> ExecutionAudioStagingResult:
        reference = result.reference(self._objects.bucket)
        phase_error = "tts_result_storage_unknown"
        try:
            async with self._new_client() as client:
                version = await self._objects.put(
                    client,
                    body=result.body,
                    reference=reference,
                    media_type=result.media_type,
                    check=check,
                )
                receipt = result.receipt(self._objects.bucket, version)
                phase_error = "tts_result_readback_failed"
                await self._objects.readback(
                    client,
                    reference=reference,
                    storage_version=version,
                    media_type=result.media_type,
                    check=check,
                )
                check()
                return receipt
        except PrivateResultIOError as error:
            raise TtsResultError("tts_result_" + error.reason) from None
        except TtsResultError, TtsCallError, TtsDeliveryError:
            raise
        except Exception:
            check()
            raise TtsResultError(phase_error) from None
