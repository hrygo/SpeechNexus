"""Fixed source-validated errors; never expose intent, key or DB diagnostics."""

from typing import Any

from speechnexus_contracts.admin import parse
from speechnexus_contracts.profile_http import validate_response
from starlette.responses import JSONResponse

CREATE = "createProfileRevision"
READ = "getProfileRevisionForAdministration"
ERRORS = {
    "invalid_profile_request": (400, "invalid_request_error", "Invalid profile request."),
    "invalid_profile_definition": (422, "invalid_request_error", "Invalid profile definition."),
    "profile_host_rejected": (403, "permission_error", "The profile Host is not allowed."),
    "profile_origin_rejected": (403, "permission_error", "The profile Origin is not allowed."),
    "profile_method_not_allowed": (
        405,
        "invalid_request_error",
        "The profile method is not allowed.",
    ),
    "profile_receive_timeout": (
        408,
        "invalid_request_error",
        "The profile receive deadline expired.",
    ),
    "profile_too_large": (
        413,
        "invalid_request_error",
        "The profile request exceeds the byte limit.",
    ),
    "profile_media_unsupported": (415, "invalid_request_error", "Profile creation requires JSON."),
    "profile_ingress_full": (429, "rate_limit_error", "Profile receive capacity is full."),
    "profile_not_found": (404, "not_found_error", "The fixed profile revision was not found."),
    "configuration_changed": (
        409,
        "invalid_request_error",
        "The profile allocation revision changed.",
    ),
    "idempotency_conflict": (
        409,
        "invalid_request_error",
        "The key identifies another profile intent.",
    ),
    "profile_result_unknown": (
        503,
        "service_unavailable_error",
        "The original profile result is unknown.",
    ),
    "profile_unavailable": (
        503,
        "service_unavailable_error",
        "Fixed profile metadata is unavailable.",
    ),
}


def profile_error(
    code: str,
    request_id: str,
    *,
    operation: str | None,
    allow: str | None = None,
) -> JSONResponse:
    if code not in ERRORS:
        code = "profile_result_unknown" if operation == CREATE else "profile_unavailable"
    status, kind, message = ERRORS[code]
    retryable = code == "profile_unavailable"
    headers = {"Cache-Control": "no-store", "X-Should-Retry": str(retryable).lower()}
    if allow is not None:
        headers["Allow"] = allow
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
    if operation is not None:
        validate_response(operation, status, {"body": body, "headers": headers})
    return JSONResponse(body, status_code=status, headers=headers)


def ingress_error(code: str, request_id: str, **kwargs: Any) -> JSONResponse:
    return profile_error(
        "invalid_profile_request" if code == "invalid_request" else "profile_" + code,
        request_id,
        **kwargs,
    )
