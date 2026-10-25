"""Single-PUT private ASR results, pinned readback and bounded cancellation cleanup."""

from __future__ import annotations

import asyncio
import io
import ssl
import threading
from collections.abc import Callable
from pathlib import Path
from time import monotonic

from minio import Minio

from speechnexus_gateway.domain.asr import AsrCallError
from speechnexus_gateway.domain.asr_results import (
    ASR_RESULT_MEDIA_TYPE,
    MAX_ASR_RESULT_BYTES,
    AsrResultError,
    PreparedAsrResult,
    StagedAsrResult,
)
from speechnexus_gateway.domain.execution_inputs import ExecutionInputError, ExecutionInputLimits
from speechnexus_gateway.infrastructure.execution_objects_minio import (
    MinioExecutionInputs,
    _endpoint,
    _FixedObjectHTTP,
)


class MinioAsrResults:
    """No bucket administration, public ACL, multipart, retry or deletion operations."""

    def __init__(
        self,
        *,
        endpoint: str,
        bucket: str,
        access_key: str,
        secret_key: str,
        spool_directory: Path,
        timeout_seconds: float,
        ssl_context: ssl.SSLContext | None = None,
    ) -> None:
        self._limits = ExecutionInputLimits(MAX_ASR_RESULT_BYTES, 64 * 1024, timeout_seconds)
        self._reader = MinioExecutionInputs(
            endpoint=endpoint,
            bucket=bucket,
            access_key=access_key,
            secret_key=secret_key,
            spool_directory=spool_directory,
            ssl_context=ssl_context,
        )
        try:
            self._http = _FixedObjectHTTP(endpoint, ssl_context, allowed_methods=frozenset({"PUT"}))
            self._client = Minio(
                _endpoint(endpoint, ssl_context),
                access_key=access_key,
                secret_key=secret_key,
                secure=endpoint.startswith("https://"),
                region="us-east-1",
                http_client=self._http,
            )
        except BaseException:
            self._reader.close()
            raise
        self._bucket = bucket

    def close(self) -> None:
        try:
            self._reader.close()
        finally:
            self._http.clear()

    async def stage(
        self, result: PreparedAsrResult, *, cancelled: Callable[[], bool]
    ) -> StagedAsrResult:
        stopping = threading.Event()

        def should_cancel() -> bool:
            return stopping.is_set() or cancelled()

        task = asyncio.create_task(asyncio.to_thread(self._stage, result, cancelled=should_cancel))
        interrupted = False
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                interrupted = True
                stopping.set()
            except Exception:
                # Retrieve the sanitized adapter error below, after cancellation wins.
                break
        if interrupted:
            # The private spool and writer have finished before the caller may close us.
            if not task.cancelled():
                task.exception()
            raise asyncio.CancelledError
        return task.result()

    def _stage(
        self, result: PreparedAsrResult, *, cancelled: Callable[[], bool]
    ) -> StagedAsrResult:
        deadline = monotonic() + self._limits.timeout_seconds

        def check_cancelled() -> bool:
            if cancelled():
                raise AsrResultError("asr_result_cancelled")
            if monotonic() >= deadline:
                raise AsrResultError("asr_result_deadline_exceeded")
            return False

        # Consumer guard failures retain their stable ASR identity. They never
        # carry content, credentials or a vendor exception message.
        check_cancelled()
        reference = result.reference(self._bucket)
        try:
            with io.BytesIO(result.body) as body:
                written = self._client.put_object(
                    self._bucket,
                    reference.object_key,
                    body,
                    reference.byte_length,
                    content_type=ASR_RESULT_MEDIA_TYPE,
                    num_parallel_uploads=1,
                )
            check_cancelled()
            version = written.version_id
            if not isinstance(version, str) or not version or version == "null":
                raise AsrResultError("asr_result_versioning_required")
            # No reference escapes until the entire pinned object has been read,
            # checked for exact EOF/length/digest and its private spool closed.
            with self._reader.open_verified(
                reference, limits=self._limits, cancelled=check_cancelled
            ) as verified:
                if verified.storage_version != version:
                    raise AsrResultError("asr_result_identity_mismatch")
            check_cancelled()
            return StagedAsrResult(result.request_digest, reference, version)
        except AsrResultError, AsrCallError:
            raise
        except ExecutionInputError:
            check_cancelled()
            raise AsrResultError("asr_result_readback_failed") from None
        except Exception:
            # A PUT timeout may already have committed. Leave its private object
            # untouched; never retry, delete it or invent a publication receipt.
            raise AsrResultError("asr_result_storage_unknown") from None
