"""Public synchronous transcription: bounded multipart in, one complete result out.

The frozen ``POST /v1/audio/transcriptions`` body is validated field by field
because no generated Python binding covers this multipart operation. The
module-level constants are asserted against ``contracts/speech-openapi.yaml`` by
the unit tests, so a contract change fails the build instead of drifting.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator, Callable
from typing import Any

from fastapi import APIRouter, Request
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import FormData, Headers, UploadFile
from starlette.formparsers import MultiPartException, MultiPartParser
from starlette.requests import ClientDisconnect
from starlette.responses import Response

from speechnexus_backend.speech.application.transcription import (
    DEFAULT_WORKSPACE,
    Transcription,
    TranscriptionAudioTooLongError,
    TranscriptionAudioUnsupportedError,
    TranscriptionDeadlineExceededError,
    TranscriptionEngineError,
    TranscriptionMediaTooLargeError,
    TranscriptionModelNotFoundError,
    TranscriptionOutputInvalidError,
    TranscriptionOutputLimitExceededError,
    TranscriptionProfileNotFoundError,
    TranscriptionUnavailableError,
    TranscriptionUnsupportedError,
    TranscriptionWorkspaceDisabledError,
    TranscriptionWorkspaceNotFoundError,
)
from speechnexus_backend.speech.domain.publications import PublicationUnavailableError
from speechnexus_backend.speech.domain.transcription import (
    MAX_TRANSCRIPTION_CLIENT_REFERENCE_CHARS,
    MAX_TRANSCRIPTION_LANGUAGE_CHARS,
    MAX_TRANSCRIPTION_PROMPT_CHARS,
    MAX_TRANSCRIPTION_UPLOAD_BYTES,
    TIMESTAMP_GRANULARITIES,
    TranscriptFormat,
    TranscriptionCommand,
    TranscriptionResult,
    TranscriptSegment,
    identifier,
)
from speechnexus_backend.speech.interfaces.http.transcription_errors import (
    transcription_error,
)

PREFIX = "/v1/audio/transcriptions"

RESPONSE_FORMATS = tuple(value.value for value in TranscriptFormat)
DEADLINE_MAX_MS = 600_000
MAX_NEXUS_CHARS = 8192
MAX_NEXUS_FIELDS = 8
SEGMENT_MIN_MS = 1000
SEGMENT_MAX_MS = 30_000

TOP_LEVEL_FIELDS = frozenset(
    {
        "file",
        "model",
        "language",
        "prompt",
        "response_format",
        "timestamp_granularities[]",
        "nexus",
    }
)
NEXUS_FIELDS = frozenset(
    {
        "workspace_id",
        "client_reference",
        "profile_id",
        "profile_revision",
        "overrides",
        "deadline_ms",
    }
)
OVERRIDE_FIELDS = frozenset({"asr"})
ASR_OVERRIDE_FIELDS = frozenset({"max_segment_ms"})

_FORMATS = {value: TranscriptFormat(value) for value in RESPONSE_FORMATS}


class _InvalidRequestError(ValueError):
    """A body that the frozen schema rejects; no default is substituted."""

    def __init__(self, param: str | None = None) -> None:
        super().__init__("Invalid transcription request")
        self.param = param


class _UnsupportedParameterError(ValueError):
    """A contract-valid body that this REST subset cannot honour."""

    def __init__(self, param: str) -> None:
        super().__init__("Unsupported transcription parameter")
        self.param = param


class _PayloadTooLargeError(ValueError):
    """The upload crossed the deployment ingress ceiling before parsing."""


def _object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result


def _constant(_: str) -> Any:
    raise ValueError("Nonfinite JSON number")


async def _receive(request: Request, max_bytes: int, receive_timeout: float) -> bytes:
    lengths = request.headers.getlist("content-length")
    if len(lengths) > 1:
        raise ValueError("Duplicate Content-Length")
    if lengths:
        try:
            declared = int(lengths[0])
        except ValueError:
            raise ValueError("Invalid Content-Length") from None
        if declared < 0:
            raise _InvalidRequestError()
        if declared > max_bytes:
            raise _PayloadTooLargeError()
    headers = Headers(scope=request.scope)
    content_type = headers.get("content-type", "").strip().lower()
    if not content_type.startswith("multipart/form-data") or "boundary=" not in content_type:
        raise _InvalidRequestError("file")
    chunks: list[bytes] = []
    size = 0
    try:
        async with asyncio.timeout(receive_timeout):
            async for chunk in request.stream():
                size += len(chunk)
                if size > max_bytes:
                    raise _PayloadTooLargeError()
                chunks.append(chunk)
    except TimeoutError:
        raise _InvalidRequestError() from None
    except ClientDisconnect:
        raise _InvalidRequestError() from None
    if not size:
        raise _InvalidRequestError("file")
    return b"".join(chunks)


async def _chunks(body: bytes) -> AsyncGenerator[bytes]:
    for offset in range(0, len(body), 65536):
        yield body[offset : offset + 65536]


async def _form(body: bytes, headers: Headers) -> FormData:
    parser = MultiPartParser(
        headers,
        _chunks(body),
        max_files=1,
        max_fields=MAX_NEXUS_FIELDS,
        max_part_size=MAX_NEXUS_CHARS,
    )
    try:
        form = await parser.parse()
    except MultiPartException, ValueError, KeyError, IndexError, UnicodeError:
        raise _InvalidRequestError() from None
    _closed_form(form)
    return form


def _closed_form(form: FormData) -> None:
    for name in form:
        if name not in TOP_LEVEL_FIELDS:
            raise _InvalidRequestError(name)


def _single(form: FormData, name: str) -> str | None:
    values = form.getlist(name)
    if len(values) > 1:
        raise _InvalidRequestError(name)
    if not values:
        return None
    value = values[0]
    if not isinstance(value, str):
        raise _InvalidRequestError(name)
    return value


def _bounded(value: str | None, name: str, maximum: int) -> str | None:
    if value is not None and len(value) > maximum:
        raise _InvalidRequestError(name)
    return value


def _revision(value: Any, name: str) -> int | None:
    if value is None:
        return None
    if (
        not isinstance(value, str)
        or not value.isdigit()
        or value.startswith("0")
        or len(value) > 19
    ):
        raise _InvalidRequestError(name)
    return int(value)


def _granularities(form: FormData) -> tuple[str, ...]:
    values = form.getlist("timestamp_granularities[]")
    if not values:
        return ()
    if len(values) > len(TIMESTAMP_GRANULARITIES):
        raise _InvalidRequestError("timestamp_granularities[]")
    result: list[str] = []
    for value in values:
        if not isinstance(value, str) or value not in TIMESTAMP_GRANULARITIES:
            raise _InvalidRequestError("timestamp_granularities[]")
        if value in result:
            raise _InvalidRequestError("timestamp_granularities[]")
        result.append(value)
    return tuple(result)


def _nexus(raw: str | None) -> dict[str, Any]:
    if raw is None:
        return {"workspace_id": DEFAULT_WORKSPACE}
    if len(raw) > MAX_NEXUS_CHARS:
        raise _InvalidRequestError("nexus")
    try:
        payload = json.loads(raw, object_pairs_hook=_object, parse_constant=_constant)
    except ValueError, TypeError, RecursionError, UnicodeDecodeError:
        raise _InvalidRequestError("nexus") from None
    if not isinstance(payload, dict):
        raise _InvalidRequestError("nexus")
    for key in payload:
        if key not in NEXUS_FIELDS:
            raise _InvalidRequestError("nexus." + key)
    workspace_id = payload.get("workspace_id", DEFAULT_WORKSPACE)
    if not isinstance(workspace_id, str):
        raise _InvalidRequestError("nexus.workspace_id")
    try:
        identifier(workspace_id)
    except ValueError:
        raise _InvalidRequestError("nexus.workspace_id") from None
    client_reference = _bounded(
        _optional_text(payload.get("client_reference"), "nexus.client_reference"),
        "nexus.client_reference",
        MAX_TRANSCRIPTION_CLIENT_REFERENCE_CHARS,
    )
    has_profile = "profile_id" in payload
    if has_profile != ("profile_revision" in payload):
        raise _InvalidRequestError("nexus.profile_id")
    profile_id = _optional_text(payload.get("profile_id"), "nexus.profile_id")
    if profile_id is not None:
        try:
            identifier(profile_id)
        except ValueError:
            raise _InvalidRequestError("nexus.profile_id") from None
    deadline_ms = payload.get("deadline_ms")
    if deadline_ms is not None and (
        isinstance(deadline_ms, bool)
        or not isinstance(deadline_ms, int)
        or not 1 <= deadline_ms <= DEADLINE_MAX_MS
    ):
        raise _InvalidRequestError("nexus.deadline_ms")
    overrides = payload.get("overrides")
    segment_ms = None
    if overrides is not None:
        if not isinstance(overrides, dict):
            raise _InvalidRequestError("nexus.overrides")
        for key in overrides:
            if key not in OVERRIDE_FIELDS:
                raise _InvalidRequestError("nexus.overrides." + key)
        asr = overrides.get("asr")
        if asr is not None:
            if not isinstance(asr, dict):
                raise _InvalidRequestError("nexus.overrides.asr")
            for key in asr:
                if key not in ASR_OVERRIDE_FIELDS:
                    raise _UnsupportedParameterError("nexus.overrides.asr." + key)
            segment_ms = asr.get("max_segment_ms")
            if segment_ms is not None and (
                isinstance(segment_ms, bool)
                or not isinstance(segment_ms, int)
                or not SEGMENT_MIN_MS <= segment_ms <= SEGMENT_MAX_MS
            ):
                raise _InvalidRequestError("nexus.overrides.asr.max_segment_ms")
    return {
        "workspace_id": workspace_id,
        "client_reference": client_reference,
        "profile_id": profile_id,
        "profile_revision": _revision(payload.get("profile_revision"), "nexus.profile_revision"),
        "deadline_ms": deadline_ms,
        "max_segment_ms": segment_ms,
    }


def _optional_text(value: Any, name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise _InvalidRequestError(name)
    return value


async def _command(form: FormData) -> TranscriptionCommand:
    uploads = form.getlist("file")
    if len(uploads) != 1 or not isinstance(uploads[0], UploadFile):
        raise _InvalidRequestError("file")
    model = _single(form, "model")
    if model is None or not 1 <= len(model) <= 128:
        raise _InvalidRequestError("model")
    language = _bounded(_single(form, "language"), "language", MAX_TRANSCRIPTION_LANGUAGE_CHARS)
    prompt = _bounded(_single(form, "prompt"), "prompt", MAX_TRANSCRIPTION_PROMPT_CHARS)
    response_format = _single(form, "response_format") or TranscriptFormat.JSON.value
    if response_format not in _FORMATS:
        raise _InvalidRequestError("response_format")
    granularities = _granularities(form)
    nexus = _nexus(_single(form, "nexus"))
    try:
        audio = await uploads[0].read()
    except OSError:
        raise _InvalidRequestError("file") from None
    if not audio:
        raise _InvalidRequestError("file")
    try:
        return TranscriptionCommand(
            model=model,
            audio=audio,
            response_format=_FORMATS[response_format],
            language=language,
            prompt=prompt or "",
            timestamp_granularities=granularities,
            **nexus,
        )
    except ValueError:
        raise _InvalidRequestError() from None


def _render(result: TranscriptionResult, response_format: TranscriptFormat) -> Response:
    headers = {"Cache-Control": "no-store"}
    if response_format is TranscriptFormat.TEXT:
        return Response(result.text, status_code=200, media_type="text/plain", headers=headers)
    if response_format is TranscriptFormat.SRT:
        return Response(
            _subtitle(result, vtt=False), status_code=200, media_type="text/plain", headers=headers
        )
    if response_format is TranscriptFormat.VTT:
        return Response(
            _subtitle(result, vtt=True), status_code=200, media_type="text/plain", headers=headers
        )
    usage = {"type": "duration", "seconds": result.duration_ms / 1000}
    if response_format is TranscriptFormat.JSON:
        return Response(
            json.dumps({"text": result.text, "usage": usage}, ensure_ascii=False),
            status_code=200,
            media_type="application/json",
            headers=headers,
        )
    return Response(
        json.dumps(
            {
                "duration": result.duration_ms / 1000,
                "language": result.language,
                "text": result.text,
                "segments": [_segment(segment) for segment in result.segments],
                "words": [
                    {"word": word.word, "start": word.start_ms / 1000, "end": word.end_ms / 1000}
                    for word in result.words
                ],
                "usage": usage,
            },
            ensure_ascii=False,
        ),
        status_code=200,
        media_type="application/json",
        headers=headers,
    )


def _segment(segment: TranscriptSegment) -> dict[str, Any]:
    return {
        "id": segment.id,
        "avg_logprob": segment.avg_logprob,
        "compression_ratio": segment.compression_ratio,
        "end": segment.end_ms / 1000,
        "no_speech_prob": segment.no_speech_prob,
        "seek": segment.seek,
        "start": segment.start_ms / 1000,
        "temperature": segment.temperature,
        "text": segment.text,
        "tokens": list(segment.tokens),
    }


def _timestamp(milliseconds: int, *, vtt: bool) -> str:
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    seconds, millis = divmod(remainder, 1000)
    separator = "." if vtt else ","
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}{separator}{millis:03d}"


def _subtitle(result: TranscriptionResult, *, vtt: bool) -> str:
    lines = ["WEBVTT", ""] if vtt else []
    for index, segment in enumerate(result.segments, start=1):
        if not vtt:
            lines.append(str(index))
        lines.append(
            f"{_timestamp(segment.start_ms, vtt=vtt)} --> {_timestamp(segment.end_ms, vtt=vtt)}"
        )
        lines.append(segment.text)
        lines.append("")
    return "\n".join(lines)


async def _create(
    service: Transcription, request: Request, max_bytes: int, receive_timeout: float
) -> Response:
    request_id = str(request.state.request_id)
    if request.headers.getlist("idempotency-key"):
        return transcription_error("unsupported_parameter", request_id, param="Idempotency-Key")
    try:
        body = await _receive(request, max_bytes, receive_timeout)
        form = await _form(body, Headers(scope=request.scope))
        try:
            command = await _command(form)
        finally:
            await form.close()
    except _UnsupportedParameterError as error:
        return transcription_error("unsupported_parameter", request_id, param=error.param)
    except _PayloadTooLargeError:
        return transcription_error("payload_too_large", request_id, param="file")
    except _InvalidRequestError as error:
        return transcription_error("invalid_request", request_id, param=error.param)
    except ValueError, TypeError, RecursionError, UnicodeDecodeError:
        return transcription_error("invalid_request", request_id)
    try:
        result = await run_in_threadpool(service.transcribe, command, request_id=request_id)
    except TranscriptionModelNotFoundError:
        return transcription_error("model_not_found", request_id)
    except TranscriptionProfileNotFoundError:
        return transcription_error("profile_not_published", request_id)
    except TranscriptionWorkspaceNotFoundError:
        return transcription_error("workspace_not_found", request_id)
    except TranscriptionWorkspaceDisabledError:
        return transcription_error("workspace_disabled", request_id)
    except TranscriptionAudioUnsupportedError:
        return transcription_error("invalid_request", request_id, param="file")
    except TranscriptionMediaTooLargeError:
        return transcription_error("payload_too_large", request_id, param="file")
    except TranscriptionAudioTooLongError:
        return transcription_error("async_required", request_id)
    except TranscriptionUnsupportedError as error:
        return transcription_error("unsupported_parameter", request_id, param=error.param)
    except TranscriptionDeadlineExceededError:
        return transcription_error("operation_timeout", request_id)
    except TranscriptionOutputLimitExceededError:
        return transcription_error("transcription_output_limit_exceeded", request_id)
    except TranscriptionOutputInvalidError:
        return transcription_error("transcription_output_invalid", request_id)
    except (
        TranscriptionEngineError,
        TranscriptionUnavailableError,
        PublicationUnavailableError,
    ):
        return transcription_error("backend_unavailable", request_id)
    except ValueError, TypeError, KeyError, RecursionError, UnicodeError, OverflowError:
        # A malformed adapter result is not a client error; never a 500 either.
        return transcription_error("transcription_output_invalid", request_id)
    try:
        return _render(result, command.response_format)
    except ValueError, TypeError, KeyError, AttributeError, RecursionError, UnicodeError:
        return transcription_error("transcription_output_invalid", request_id)


def create_transcription_router(
    get_service: Callable[[], Transcription | None],
    *,
    max_bytes: int = MAX_TRANSCRIPTION_UPLOAD_BYTES + 65536,
    receive_timeout_seconds: float = 30,
) -> APIRouter:
    if not 65536 <= max_bytes <= MAX_TRANSCRIPTION_UPLOAD_BYTES + 65536:
        raise ValueError("Transcription body limit must be bounded")
    if not 0 < receive_timeout_seconds <= 120:
        raise ValueError("Transcription receive timeout must be bounded")
    router = APIRouter(tags=["speech"])

    @router.post(PREFIX, operation_id="createTranscription")
    async def create_transcription(request: Request) -> Response:
        service = get_service()
        request_id = str(request.state.request_id)
        if service is None:
            return transcription_error("backend_unavailable", request_id)
        return await _create(service, request, max_bytes, receive_timeout_seconds)

    return router


__all__ = [
    "ASR_OVERRIDE_FIELDS",
    "DEADLINE_MAX_MS",
    "MAX_NEXUS_CHARS",
    "NEXUS_FIELDS",
    "OVERRIDE_FIELDS",
    "PREFIX",
    "RESPONSE_FORMATS",
    "TOP_LEVEL_FIELDS",
    "create_transcription_router",
]
