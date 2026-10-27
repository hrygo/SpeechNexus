"""Public synchronous speech: strict request parsing, complete audio or a failure.

The frozen ``POST /v1/audio/speech`` body is validated here field by field
because no generated Python binding covers this operation. The module-level
constants are asserted against ``contracts/speech-openapi.yaml`` by a unit test,
so a contract change fails the build instead of drifting silently.
"""

from __future__ import annotations

import asyncio
import json
import math
from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Request
from starlette.concurrency import run_in_threadpool
from starlette.requests import ClientDisconnect
from starlette.responses import Response

from speechnexus_backend.speech.application.speech_synthesis import (
    DEFAULT_WORKSPACE,
    SpeechDeadlineExceededError,
    SpeechEngineError,
    SpeechModelNotFoundError,
    SpeechOutputInvalidError,
    SpeechOutputLimitExceededError,
    SpeechProfileNotFoundError,
    SpeechPronunciationNotFoundError,
    SpeechSynthesis,
    SpeechUnavailableError,
    SpeechUnsupportedError,
    SpeechVoiceNotFoundError,
    SpeechWorkspaceDisabledError,
    SpeechWorkspaceNotFoundError,
)
from speechnexus_backend.speech.domain.publications import PublicationUnavailableError
from speechnexus_backend.speech.domain.speech_synthesis import (
    MAX_SPEECH_INSTRUCTIONS_CHARS,
    MAX_SPEECH_TEXT_CHARS,
    SpeechCommand,
    SpeechFormat,
    identifier,
)
from speechnexus_backend.speech.interfaces.http.speech_errors import speech_error

PREFIX = "/v1/audio/speech"

RESPONSE_FORMATS = ("mp3", "wav", "pcm")
STREAM_FORMATS = ("audio",)
SPEED_MIN = 0.25
SPEED_MAX = 4.0
PLANNER_CHARS_MIN = 40
PLANNER_CHARS_MAX = 400
PLANNER_WAIT_MIN = 100
PLANNER_WAIT_MAX = 3000
DEADLINE_MAX_MS = 600_000
MAX_LANGUAGE_CHARS = 35
MAX_CLIENT_REFERENCE_CHARS = 256
TEXT_COMMIT_MODES = ("explicit", "sentence")

TOP_LEVEL_FIELDS = frozenset(
    {
        "model",
        "input",
        "voice",
        "instructions",
        "response_format",
        "speed",
        "stream_format",
        "nexus",
    }
)
NEXUS_FIELDS = frozenset(
    {
        "workspace_id",
        "client_reference",
        "profile_id",
        "profile_revision",
        "voice_revision",
        "pronunciation_revision",
        "overrides",
        "deadline_ms",
    }
)
OVERRIDE_FIELDS = frozenset({"tts"})
TTS_OVERRIDE_FIELDS = frozenset(
    {"language", "text_commit_mode", "audio_delivery", "seed", "planner"}
)
PLANNER_FIELDS = frozenset({"max_chars", "max_wait_ms"})

_FORMATS = {value: SpeechFormat(value) for value in RESPONSE_FORMATS}


class _InvalidRequestError(ValueError):
    """A body that the frozen schema rejects; no default is substituted."""

    def __init__(self, param: str | None = None) -> None:
        super().__init__("Invalid speech request")
        self.param = param


class _UnsupportedParameterError(ValueError):
    """A contract-valid body that this REST subset cannot honour."""

    def __init__(self, param: str) -> None:
        super().__init__("Unsupported speech parameter")
        self.param = param


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
        if declared < 0 or declared > max_bytes:
            raise _InvalidRequestError()
    content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if content_type != "application/json":
        raise _InvalidRequestError()
    chunks: list[bytes] = []
    size = 0
    try:
        async with asyncio.timeout(receive_timeout):
            async for chunk in request.stream():
                size += len(chunk)
                if size > max_bytes:
                    raise _InvalidRequestError()
                chunks.append(chunk)
    except TimeoutError:
        raise _InvalidRequestError() from None
    except ClientDisconnect:
        raise _InvalidRequestError() from None
    return b"".join(chunks)


def _text(payload: dict[str, Any], name: str, minimum: int, maximum: int) -> str | None:
    if name not in payload:
        return None
    value = payload[name]
    if not isinstance(value, str) or not minimum <= len(value) <= maximum:
        raise _InvalidRequestError(name)
    return value


def _revision(payload: dict[str, Any], name: str) -> int | None:
    if name not in payload:
        return None
    value = payload[name]
    if (
        not isinstance(value, str)
        or not value.isdigit()
        or value.startswith("0")
        or len(value) > 19
    ):
        raise _InvalidRequestError("nexus." + name)
    return int(value)


def _integer(payload: dict[str, Any], name: str, minimum: int, maximum: int) -> int | None:
    if name not in payload:
        return None
    value = payload[name]
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise _InvalidRequestError(name)
    return value


def _object_field(payload: dict[str, Any], name: str) -> dict[str, Any] | None:
    if name not in payload:
        return None
    value = payload[name]
    if not isinstance(value, dict):
        raise _InvalidRequestError(name)
    return value


def _closed(value: dict[str, Any], allowed: frozenset[str], prefix: str) -> None:
    for key in value:
        if key not in allowed:
            raise _InvalidRequestError(prefix + key)


def _command(payload: Any) -> SpeechCommand:
    if not isinstance(payload, dict):
        raise _InvalidRequestError()
    _closed(payload, TOP_LEVEL_FIELDS, "")
    model = _text(payload, "model", 1, 128)
    text = _text(payload, "input", 1, MAX_SPEECH_TEXT_CHARS)
    voice = _text(payload, "voice", 1, 128)
    if model is None or text is None or voice is None:
        raise _InvalidRequestError(
            "model" if model is None else "input" if text is None else "voice"
        )
    instructions = _text(payload, "instructions", 0, MAX_SPEECH_INSTRUCTIONS_CHARS)
    response_format = payload.get("response_format", "mp3")
    if not isinstance(response_format, str) or response_format not in _FORMATS:
        raise _InvalidRequestError("response_format")
    stream_format = payload.get("stream_format", "audio")
    if not isinstance(stream_format, str) or stream_format not in STREAM_FORMATS:
        raise _InvalidRequestError("stream_format")
    speed = payload.get("speed", 1.0)
    if (
        isinstance(speed, bool)
        or not isinstance(speed, (int, float))
        or not math.isfinite(float(speed))
        or not SPEED_MIN <= float(speed) <= SPEED_MAX
    ):
        raise _InvalidRequestError("speed")

    nexus = _object_field(payload, "nexus")
    options = _nexus(nexus) if nexus is not None else {}
    return SpeechCommand(
        model=model,
        text=text,
        voice=voice,
        response_format=_FORMATS[response_format],
        speed=float(speed),
        instructions=instructions or "",
        **options,
    )


def _nexus(nexus: dict[str, Any]) -> dict[str, Any]:
    _closed(nexus, NEXUS_FIELDS, "nexus.")
    workspace_id = nexus.get("workspace_id", DEFAULT_WORKSPACE)
    if not isinstance(workspace_id, str):
        raise _InvalidRequestError("nexus.workspace_id")
    try:
        identifier(workspace_id)
    except ValueError:
        raise _InvalidRequestError("nexus.workspace_id") from None
    client_reference = _text(nexus, "client_reference", 0, MAX_CLIENT_REFERENCE_CHARS)
    if ("profile_id" in nexus) != ("profile_revision" in nexus):
        raise _InvalidRequestError("nexus.profile_id")
    profile_id = _text(nexus, "profile_id", 3, 128)
    if profile_id is not None:
        try:
            identifier(profile_id)
        except ValueError:
            raise _InvalidRequestError("nexus.profile_id") from None
    deadline_ms = _integer(nexus, "deadline_ms", 1, DEADLINE_MAX_MS)
    overrides = _object_field(nexus, "overrides")
    tts = _tts_overrides(overrides.get("tts")) if overrides is not None else None
    return {
        "workspace_id": workspace_id,
        "client_reference": client_reference,
        "profile_id": profile_id,
        "profile_revision": _revision(nexus, "profile_revision"),
        "voice_revision": _revision(nexus, "voice_revision"),
        "pronunciation_revision": _revision(nexus, "pronunciation_revision"),
        "deadline_ms": deadline_ms,
        "language": tts.get("language") if tts else None,
        "seed": tts.get("seed") if tts else None,
        "planner_max_chars": tts.get("planner_max_chars") if tts else None,
        "planner_max_wait_ms": tts.get("planner_max_wait_ms") if tts else None,
    }


def _tts_overrides(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise _InvalidRequestError("nexus.overrides.tts")
    _closed(value, TTS_OVERRIDE_FIELDS, "nexus.overrides.tts.")
    language = _text(value, "language", 0, MAX_LANGUAGE_CHARS)
    mode = value.get("text_commit_mode", "explicit")
    if not isinstance(mode, str) or mode not in TEXT_COMMIT_MODES:
        raise _InvalidRequestError("nexus.overrides.tts.text_commit_mode")
    if mode != "explicit":
        raise _UnsupportedParameterError("nexus.overrides.tts.text_commit_mode")
    delivery = value.get("audio_delivery", "complete")
    if delivery != "complete":
        raise _InvalidRequestError("nexus.overrides.tts.audio_delivery")
    seed = value.get("seed")
    if seed is not None and (
        isinstance(seed, bool) or not isinstance(seed, int) or not 0 <= seed <= 2**31 - 1
    ):
        raise _InvalidRequestError("nexus.overrides.tts.seed")
    planner = _object_field(value, "planner")
    if planner is not None:
        _closed(planner, PLANNER_FIELDS, "nexus.overrides.tts.planner.")
    return {
        "language": language,
        "seed": seed,
        "planner_max_chars": _integer(
            planner or {}, "max_chars", PLANNER_CHARS_MIN, PLANNER_CHARS_MAX
        ),
        "planner_max_wait_ms": _integer(
            planner or {}, "max_wait_ms", PLANNER_WAIT_MIN, PLANNER_WAIT_MAX
        ),
    }


async def _create(
    service: SpeechSynthesis, request: Request, max_bytes: int, receive_timeout: float
) -> Response:
    request_id = str(request.state.request_id)
    if request.headers.getlist("idempotency-key"):
        # The frozen contract declares the header, but this chain has no durable
        # operation identity to replay. Refuse instead of pretending to honour it.
        return speech_error("unsupported_parameter", request_id, param="Idempotency-Key")
    try:
        raw = await _receive(request, max_bytes, receive_timeout)
        payload = json.loads(
            raw.decode("utf-8"), object_pairs_hook=_object, parse_constant=_constant
        )
        command = _command(payload)
    except _UnsupportedParameterError as error:
        return speech_error("unsupported_parameter", request_id, param=error.param)
    except _InvalidRequestError as error:
        return speech_error("invalid_request", request_id, param=error.param)
    except ValueError, TypeError, RecursionError, UnicodeDecodeError:
        return speech_error("invalid_request", request_id)
    try:
        audio = await run_in_threadpool(service.synthesize, command, request_id=request_id)
    except SpeechModelNotFoundError:
        return speech_error("model_not_found", request_id)
    except SpeechVoiceNotFoundError:
        return speech_error("voice_not_found", request_id)
    except SpeechProfileNotFoundError:
        return speech_error("profile_not_published", request_id)
    except SpeechPronunciationNotFoundError:
        return speech_error("pronunciation_not_found", request_id)
    except SpeechWorkspaceNotFoundError:
        return speech_error("workspace_not_found", request_id)
    except SpeechWorkspaceDisabledError:
        return speech_error("workspace_disabled", request_id)
    except SpeechUnsupportedError as error:
        return speech_error("unsupported_parameter", request_id, param=error.param)
    except SpeechDeadlineExceededError:
        return speech_error("operation_timeout", request_id)
    except SpeechOutputLimitExceededError:
        return speech_error("speech_output_limit_exceeded", request_id)
    except SpeechOutputInvalidError:
        return speech_error("speech_output_invalid", request_id)
    except (
        SpeechEngineError,
        SpeechUnavailableError,
        PublicationUnavailableError,
    ):
        return speech_error("backend_unavailable", request_id)
    except ValueError, TypeError, KeyError, RecursionError, UnicodeError, OverflowError:
        # A malformed adapter result is not a client error; never a 500 either.
        return speech_error("speech_output_invalid", request_id)
    return Response(
        audio.payload,
        status_code=200,
        media_type=audio.response_format.media_type,
        headers={
            "Cache-Control": "no-store",
            "Content-Length": str(len(audio.payload)),
            "X-Audio-Format": audio.response_format.header_name,
            "X-Sample-Rate-Hz": str(audio.sample_rate_hz),
            "X-Channels": str(audio.channels),
        },
    )


def create_speech_router(
    get_service: Callable[[], SpeechSynthesis | None],
    *,
    max_bytes: int = 65536,
    receive_timeout_seconds: float = 5,
) -> APIRouter:
    if not 1024 <= max_bytes <= 262144:
        raise ValueError("Speech body limit must be bounded")
    if not 0 < receive_timeout_seconds <= 30:
        raise ValueError("Speech receive timeout must be bounded")
    router = APIRouter(tags=["speech"])

    @router.post(PREFIX, operation_id="createSpeech")
    async def create_speech(request: Request) -> Response:
        service = get_service()
        request_id = str(request.state.request_id)
        if service is None:
            return speech_error("backend_unavailable", request_id)
        return await _create(service, request, max_bytes, receive_timeout_seconds)

    return router


__all__ = [
    "DEADLINE_MAX_MS",
    "MAX_LANGUAGE_CHARS",
    "NEXUS_FIELDS",
    "OVERRIDE_FIELDS",
    "PLANNER_CHARS_MAX",
    "PLANNER_CHARS_MIN",
    "PLANNER_FIELDS",
    "PLANNER_WAIT_MAX",
    "PLANNER_WAIT_MIN",
    "PREFIX",
    "RESPONSE_FORMATS",
    "SPEED_MAX",
    "SPEED_MIN",
    "STREAM_FORMATS",
    "TOP_LEVEL_FIELDS",
    "TTS_OVERRIDE_FIELDS",
    "create_speech_router",
]
