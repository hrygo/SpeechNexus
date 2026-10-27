"""Production synchronous ASR over the one execution-gateway client.

The adapter never invents admission, capacity or result facts. It uploads the
bounded uploaded bytes, obtains a complete lease/fencing identity from admission,
submits the frozen command, polls the original identity, and reads the verified
private result receipt. Transport ambiguity remains unknown; only an explicit
terminal gateway failure becomes ``TranscriptionEngineError``.
"""

from __future__ import annotations

import asyncio
import hashlib
import threading
import time
from collections.abc import Mapping
from typing import Any, Protocol, cast

from speechnexus_contracts.execution_models import ArtifactStorageReference

from speechnexus_backend.speech.application.transcription import (
    Transcriber,
    TranscriptionAdmission,
    TranscriptionDeadlineExceededError,
    TranscriptionEngineError,
    TranscriptionInputObjects,
    TranscriptionOutcomeUnknownError,
    TranscriptionOutputInvalidError,
    TranscriptionUnavailableError,
)
from speechnexus_backend.speech.domain.transcription import (
    MAX_TRANSCRIPTION_AUDIO_MS,
    TranscriptionPlan,
    TranscriptionResult,
    TranscriptSegment,
    TranscriptWord,
)
from speechnexus_backend.speech.infrastructure.gateway_execution_client import (
    GatewayConnectionError,
    GatewayExecutionClient,
    GatewayRejectedError,
    GatewayResponseError,
    GatewayResultObjectError,
    GatewayServerError,
    GatewayTimeoutError,
)
from speechnexus_backend.speech.infrastructure.transcription_submit import (
    TranscriptionCommandPlanner,
)

_RESULT_FIELDS = frozenset({"text", "duration_ms", "language", "segments", "words"})
_SEGMENT_FIELDS = frozenset(
    {
        "id",
        "start_ms",
        "end_ms",
        "text",
        "tokens",
        "avg_logprob",
        "compression_ratio",
        "no_speech_prob",
        "seek",
        "temperature",
    }
)
_WORD_FIELDS = frozenset({"word", "start_ms", "end_ms"})


class TranscriptionResultObjects(Protocol):
    def read_json(
        self, reference: ArtifactStorageReference, storage_version: str
    ) -> dict[str, Any]: ...


class _GatewayLoop:
    """Keep one AsyncClient on one long-lived event loop for sync callers."""

    def __init__(self, client: GatewayExecutionClient) -> None:
        self._client = client
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._serve, name="transcription-gateway", daemon=True
        )
        self._guard = threading.Lock()
        self._closed = False
        self._thread.start()

    def _serve(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def call(self, operation: Any) -> Any:
        with self._guard:
            if self._closed:
                raise TranscriptionUnavailableError("Transcription gateway loop is closed")
            future = asyncio.run_coroutine_threadsafe(operation(), self._loop)
        return future.result()

    def close(self) -> None:
        with self._guard:
            if self._closed:
                return
            self._closed = True
        try:
            asyncio.run_coroutine_threadsafe(self._client.aclose(), self._loop).result(timeout=10)
        finally:
            self._loop.call_soon_threadsafe(self._loop.stop)
            self._thread.join(timeout=10)
            if not self._thread.is_alive():
                self._loop.close()


class GatewayTranscriber(Transcriber):
    """One bounded request; all identity comes from the injected admission port."""

    def __init__(
        self,
        gateway: GatewayExecutionClient,
        admission: TranscriptionAdmission,
        inputs: TranscriptionInputObjects,
        results: TranscriptionResultObjects,
        planner: TranscriptionCommandPlanner,
        *,
        poll_seconds: float = 0.25,
    ) -> None:
        if not 0 < poll_seconds <= 30:
            raise ValueError("Transcription poll interval must be bounded")
        self._gateway = gateway
        self._admission = admission
        self._inputs = inputs
        self._results = results
        self._planner = planner
        self._poll_seconds = float(poll_seconds)
        self._bridge = _GatewayLoop(gateway)

    def close(self) -> None:
        try:
            for resource in (self._inputs, self._results, self._admission):
                closer = getattr(resource, "close", None)
                if callable(closer):
                    closer()
        finally:
            self._bridge.close()

    def transcribe(self, plan: TranscriptionPlan) -> TranscriptionResult:
        return cast(TranscriptionResult, self._bridge.call(lambda: self._run(plan)))

    async def _run(self, plan: TranscriptionPlan) -> TranscriptionResult:
        # Reject unrepresentable client values before writing bytes or taking a
        # lease; admission is never released by a later validation failure.
        self._planner.validate(plan)
        submit_intent_id = _submit_identity(plan)
        audio = await asyncio.to_thread(self._inputs.put, plan)
        admitted = await asyncio.to_thread(
            self._admission.admit, plan, submit_intent_id=submit_intent_id, audio=audio
        )
        request = self._planner.build(plan, admitted, audio)
        reference, version = await self._execute(request, submit_intent_id, plan)
        try:
            document = await asyncio.to_thread(self._results.read_json, reference, version)
        except GatewayResultObjectError:
            raise TranscriptionOutcomeUnknownError("Private result object is unavailable") from None
        try:
            return _to_result(plan, document)
        except ValueError, TypeError, KeyError, OverflowError:
            raise TranscriptionOutputInvalidError("Gateway result is not interpretable") from None

    async def _execute(
        self,
        request: Any,
        submit_intent_id: str,
        plan: TranscriptionPlan,
    ) -> tuple[ArtifactStorageReference, str]:
        try:
            response = await self._submit(request)
            await self._poll(submit_intent_id, response, plan)
            receipt = await self._staging(submit_intent_id)
        except GatewayResponseError:
            raise _unknown() from None
        except GatewayConnectionError, GatewayTimeoutError, GatewayServerError:
            raise _unknown() from None
        except GatewayRejectedError as error:
            if error.status_code in (408, 429):
                raise _unknown() from None
            raise TranscriptionEngineError("Execution gateway rejected the request") from None
        staging = cast(Mapping[str, Any], receipt["staging_result"])
        if receipt.get("submit_intent_id") != submit_intent_id or staging.get(
            "request_digest"
        ) != receipt.get("request_digest"):
            raise TranscriptionOutcomeUnknownError("Result receipt identity is ambiguous")
        reference = cast(ArtifactStorageReference, staging["reference"])
        version = staging["storage_version"]
        if not isinstance(version, str) or not version:
            raise TranscriptionOutcomeUnknownError("Result receipt has no storage version")
        return reference, version

    async def _submit(self, request: Any) -> Mapping[str, Any]:
        response = await self._gateway.submit(request)
        if response.get("submit_intent_id") != request["submit_intent_id"]:
            raise TranscriptionOutcomeUnknownError(
                "Gateway submit response belongs to another intent"
            )
        if response.get("command_digest") != request["command_digest"]:
            raise TranscriptionOutcomeUnknownError("Gateway submit response changed the command")
        return cast(Mapping[str, Any], response)

    async def _poll(
        self, submit_intent_id: str, response: Mapping[str, Any], plan: TranscriptionPlan
    ) -> Mapping[str, Any]:
        state = response.get("execution_state")
        deadline = time.monotonic() + plan.timeout_seconds
        status: Mapping[str, Any] = response
        while True:
            if state == "succeeded":
                return status
            if state in ("failed", "aborted"):
                raise TranscriptionEngineError("Execution gateway reported a terminal failure")
            if state == "unknown_quarantined":
                raise _unknown()
            if time.monotonic() >= deadline:
                raise TranscriptionDeadlineExceededError
            await asyncio.sleep(self._poll_seconds)
            status = cast(Mapping[str, Any], await self._gateway.query(submit_intent_id))
            if status.get("submit_intent_id") != submit_intent_id:
                raise TranscriptionOutcomeUnknownError(
                    "Gateway status belongs to another execution"
                )
            state = status.get("execution_state")

    async def _staging(self, submit_intent_id: str) -> Mapping[str, Any]:
        receipt = await self._gateway.staging_result(submit_intent_id)
        if receipt.get("submit_intent_id") != submit_intent_id:
            raise TranscriptionOutcomeUnknownError("Gateway receipt belongs to another execution")
        if receipt.get("staging_result") is None:
            raise _unknown()
        return cast(Mapping[str, Any], receipt)


def _submit_identity(plan: TranscriptionPlan) -> str:
    digest = hashlib.sha256(
        ("synchronous-transcription:" + plan.request_id).encode("utf-8")
    ).hexdigest()[:48]
    return "submit-" + digest


def _unknown() -> TranscriptionOutcomeUnknownError:
    return TranscriptionOutcomeUnknownError("Gateway transport outcome is unknown")


def _to_result(plan: TranscriptionPlan, document: Mapping[str, Any]) -> TranscriptionResult:
    if set(document) - _RESULT_FIELDS or not isinstance(document.get("text"), str):
        raise TranscriptionOutputInvalidError("Transcript receipt has no valid text")
    duration = document.get("duration_ms")
    if duration is not None and (
        type(duration) is not int or not 0 < duration <= MAX_TRANSCRIPTION_AUDIO_MS
    ):
        raise TranscriptionOutputInvalidError("Transcript receipt has an invalid duration")
    language = document.get("language")
    segments = _segments(plan, document.get("segments"))
    words = _words(plan, document.get("words"))
    return TranscriptionResult(
        text=cast(str, document["text"]),
        language=cast(str | None, language),
        duration_ms=duration,
        segments=segments,
        words=words,
    )


def _segments(plan: TranscriptionPlan, value: Any) -> tuple[TranscriptSegment, ...]:
    if value is None:
        if plan.require_segments:
            raise TranscriptionOutputInvalidError("Requested segment evidence is absent")
        return ()
    if type(value) is not list:
        raise TranscriptionOutputInvalidError("Transcript segments are not a list")
    complete: list[TranscriptSegment] = []
    for item in value:
        if not isinstance(item, dict) or set(item) != _SEGMENT_FIELDS:
            if plan.require_segments:
                raise TranscriptionOutputInvalidError("Segment evidence is incomplete")
            return ()
        complete.append(
            TranscriptSegment(
                id=item["id"],
                start_ms=item["start_ms"],
                end_ms=item["end_ms"],
                text=item["text"],
                tokens=tuple(item["tokens"]),
                avg_logprob=item["avg_logprob"],
                compression_ratio=item["compression_ratio"],
                no_speech_prob=item["no_speech_prob"],
                seek=item["seek"],
                temperature=item["temperature"],
            )
        )
    if plan.require_segments and not complete:
        raise TranscriptionOutputInvalidError("Requested segment evidence is absent")
    return tuple(complete)


def _words(plan: TranscriptionPlan, value: Any) -> tuple[TranscriptWord, ...]:
    if value is None:
        if plan.require_words:
            raise TranscriptionOutputInvalidError("Requested word evidence is absent")
        return ()
    if type(value) is not list:
        raise TranscriptionOutputInvalidError("Transcript words are not a list")
    complete: list[TranscriptWord] = []
    for item in value:
        if not isinstance(item, dict) or set(item) != _WORD_FIELDS:
            if plan.require_words:
                raise TranscriptionOutputInvalidError("Word evidence is incomplete")
            return ()
        complete.append(
            TranscriptWord(word=item["word"], start_ms=item["start_ms"], end_ms=item["end_ms"])
        )
    if plan.require_words and not complete:
        raise TranscriptionOutputInvalidError("Requested word evidence is absent")
    return tuple(complete)


__all__ = ["GatewayTranscriber", "TranscriptionResultObjects"]
