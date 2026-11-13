"""Fixed source-validated errors; never expose key, evidence or DB diagnostics."""

from __future__ import annotations

from speechnexus_contracts.admin import parse
from speechnexus_contracts.admin_http import validate_response
from starlette.responses import JSONResponse

OPERATION = "releaseQuarantinedAdmissionLease"
ERRORS: dict[str, tuple[int, str, str]] = {
    "quarantine_release_invalid": (
        400,
        "invalid_request_error",
        "Invalid quarantine release evidence.",
    ),
    "idempotency_key_invalid": (
        400,
        "invalid_request_error",
        "The Idempotency-Key header is invalid.",
    ),
    "quarantine_release_not_found": (
        404,
        "not_found_error",
        "The admission lease was not found.",
    ),
    "idempotency_conflict": (
        409,
        "invalid_request_error",
        "The key identifies another quarantine release intent.",
    ),
    "quarantine_release_conflict": (
        409,
        "invalid_request_error",
        "The lease cannot be released with this evidence.",
    ),
    "quarantine_release_unknown": (
        503,
        "service_unavailable_error",
        "The quarantine release result is unknown.",
    ),
    "quarantine_release_unavailable": (
        503,
        "service_unavailable_error",
        "Quarantine release storage is unavailable.",
    ),
    "quarantine_release_schema_not_migrated": (
        503,
        "service_unavailable_error",
        "Quarantine release storage is missing a table or column; migrate schema.",
    ),
    "quarantine_release_privilege_missing": (
        503,
        "service_unavailable_error",
        "Quarantine release role no longer holds its declared grants.",
    ),
    "quarantine_release_failed": (
        503,
        "service_unavailable_error",
        "Quarantine release transaction failed before releasing capacity.",
    ),
}
#: Only a transient storage outage can heal without an operator. A missing
#: migration, a revoked grant or an unexpected refusal needs a deployment fix,
#: so reporting them as retryable would make operator scripts poll forever.
RETRYABLE = frozenset({"quarantine_release_unavailable"})


def release_error(code: str, request_id: str) -> JSONResponse:
    if code not in ERRORS:
        code = "quarantine_release_unknown"
    status, kind, message = ERRORS[code]
    retryable = code in RETRYABLE
    headers = {"Cache-Control": "no-store", "X-Should-Retry": str(retryable).lower()}
    body = dict(
        parse(
            "ErrorResponse",
            {
                "error": {
                    "code": code,
                    "type": kind,
                    "message": message,
                    "param": None,
                    "request_id": request_id,
                    "retryable": retryable,
                }
            },
        )
    )
    validate_response(OPERATION, status, {"body": body, "headers": headers})
    return JSONResponse(body, status_code=status, headers=headers)


__all__ = ["ERRORS", "OPERATION", "RETRYABLE", "release_error"]
