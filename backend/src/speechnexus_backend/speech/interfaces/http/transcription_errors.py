"""Stable public transcription errors; engine, storage and endpoint detail stays inside."""

from __future__ import annotations

from speechnexus_contracts import parse
from starlette.responses import JSONResponse

_ERRORS: dict[str, tuple[int, str, str, bool]] = {
    "invalid_request": (
        400,
        "invalid_request_error",
        "The transcription request is invalid.",
        False,
    ),
    "unsupported_parameter": (
        422,
        "invalid_request_error",
        "The transcription request is not supported by this binding.",
        False,
    ),
    "async_required": (
        422,
        "invalid_request_error",
        "The audio exceeds the synchronous transcription budget.",
        False,
    ),
    "payload_too_large": (
        413,
        "invalid_request_error",
        "The uploaded audio exceeds the request upload ceiling.",
        False,
    ),
    "model_not_found": (404, "not_found_error", "The model is not published.", False),
    "profile_not_published": (
        422,
        "invalid_request_error",
        "The profile revision is not published.",
        False,
    ),
    "workspace_not_found": (422, "invalid_request_error", "The workspace is not published.", False),
    "workspace_disabled": (422, "invalid_request_error", "The workspace is disabled.", False),
    "transcription_method_not_allowed": (
        405,
        "invalid_request_error",
        "The transcription method is not allowed.",
        False,
    ),
    "backend_unavailable": (
        503,
        "service_unavailable_error",
        "The speech backend is unavailable.",
        True,
    ),
    "operation_timeout": (
        504,
        "service_unavailable_error",
        "The transcription request exceeded its deadline.",
        True,
    ),
    "transcription_output_limit_exceeded": (
        503,
        "api_error",
        "The transcriber exceeded the request media ceiling.",
        False,
    ),
    "transcription_output_invalid": (
        503,
        "api_error",
        "The transcriber returned a transcript that does not match the request.",
        False,
    ),
}


def transcription_error(
    code: str,
    request_id: str,
    *,
    param: str | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    if code not in _ERRORS:
        code = "backend_unavailable"
    status, kind, message, retryable = _ERRORS[code]
    return JSONResponse(
        status_code=status,
        headers={"Cache-Control": "no-store", **(headers or {})},
        content=parse(
            "ErrorResponse",
            {
                "error": {
                    "code": code,
                    "type": kind,
                    "message": message,
                    "param": param,
                    "request_id": request_id,
                    "retryable": retryable,
                }
            },
        ),
    )


__all__ = ["transcription_error"]
