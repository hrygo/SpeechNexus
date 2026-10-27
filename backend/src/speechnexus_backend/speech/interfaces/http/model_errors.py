"""Stable model-discovery errors; no storage detail or binding identity leaks out."""

from __future__ import annotations

from speechnexus_contracts import parse
from starlette.responses import JSONResponse

_ERRORS: dict[str, tuple[int, str, str]] = {
    "model_catalogue_unavailable": (
        503,
        "service_unavailable_error",
        "The model catalogue is unavailable.",
    ),
    "model_not_found": (404, "not_found_error", "The model was not found."),
}


def model_error(code: str, request_id: str) -> JSONResponse:
    if code not in _ERRORS:
        code = "model_catalogue_unavailable"
    status, kind, message = _ERRORS[code]
    return JSONResponse(
        status_code=status,
        headers={"Cache-Control": "no-store", "X-Should-Retry": "false"},
        content=parse(
            "ErrorResponse",
            {
                "error": {
                    "code": code,
                    "type": kind,
                    "message": message,
                    "param": None,
                    "request_id": request_id,
                    "retryable": False,
                }
            },
        ),
    )
