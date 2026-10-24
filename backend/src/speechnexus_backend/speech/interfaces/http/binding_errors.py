"""Fixed source-validated errors; never expose intent, key or DB diagnostics."""

from typing import Any

from speechnexus_contracts.admin import parse
from speechnexus_contracts.binding_http import validate_response
from starlette.responses import JSONResponse

CREATE = "createModelBindingDraft"
READ = "getModelBindingRevision"
ERRORS = {
    "invalid_binding_request": (400, "invalid_request_error", "Invalid binding request."),
    "invalid_binding_definition": (422, "invalid_request_error", "Invalid binding definition."),
    "binding_host_rejected": (403, "permission_error", "The binding Host is not allowed."),
    "binding_origin_rejected": (403, "permission_error", "The binding Origin is not allowed."),
    "binding_method_not_allowed": (
        405,
        "invalid_request_error",
        "The binding method is not allowed.",
    ),
    "binding_receive_timeout": (
        408,
        "invalid_request_error",
        "The binding receive deadline expired.",
    ),
    "binding_too_large": (
        413,
        "invalid_request_error",
        "The binding request exceeds the byte limit.",
    ),
    "binding_media_unsupported": (415, "invalid_request_error", "Binding creation requires JSON."),
    "binding_ingress_full": (429, "rate_limit_error", "Binding receive capacity is full."),
    "binding_not_found": (404, "not_found_error", "The fixed binding revision was not found."),
    "idempotency_conflict": (
        409,
        "invalid_request_error",
        "The key identifies another binding intent.",
    ),
    "binding_result_unknown": (
        503,
        "service_unavailable_error",
        "The original binding result is unknown.",
    ),
    "binding_unavailable": (
        503,
        "service_unavailable_error",
        "Fixed binding metadata is unavailable.",
    ),
}


def binding_error(
    code: str,
    request_id: str,
    *,
    operation: str | None,
    allow: str | None = None,
) -> JSONResponse:
    if code not in ERRORS:
        code = "binding_result_unknown" if operation == CREATE else "binding_unavailable"
    status, kind, message = ERRORS[code]
    retryable = code == "binding_unavailable"
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
    return binding_error(
        "invalid_binding_request" if code == "invalid_request" else "binding_" + code,
        request_id,
        **kwargs,
    )
