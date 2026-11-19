"""Stable public deletion errors; database and plan internals never cross."""

from __future__ import annotations

from speechnexus_contracts import parse
from starlette.responses import JSONResponse

_ERRORS: dict[str, tuple[int, str, str]] = {
    "deletion_owner_mismatch": (
        422,
        "invalid_request_error",
        "This endpoint owns only speech resources.",
    ),
    "deletion_target_unsupported": (
        422,
        "invalid_request_error",
        "This deployment cannot plan deletion for that speech resource kind.",
    ),
    "invalid_deletion_request": (
        422,
        "invalid_request_error",
        "Invalid deletion plan request.",
    ),
    "invalid_idempotency_key": (
        422,
        "invalid_request_error",
        "Invalid Idempotency-Key.",
    ),
    "resource_not_found": (404, "not_found_error", "The deletion target was not found."),
    "deletion_plan_changed": (
        409,
        "invalid_request_error",
        "The target revision changed since the request.",
    ),
    "deletion_plan_expired": (
        409,
        "invalid_request_error",
        "The deletion plan expired; create a new one.",
    ),
    "deletion_dependency_conflict": (
        409,
        "invalid_request_error",
        "A blocking dependency prevents this deletion.",
    ),
    "deletion_confirmation_mismatch": (
        422,
        "invalid_request_error",
        "The confirmation does not match the plan's complete scope.",
    ),
    "idempotency_conflict": (
        409,
        "invalid_request_error",
        "The key identifies another request.",
    ),
    "deletion_plans_unavailable": (
        503,
        "service_unavailable_error",
        "Deletion plan storage is unavailable.",
    ),
    "deletion_plans_not_configured": (
        503,
        "service_unavailable_error",
        "Deletion preview is missing its database grants; apply deletion_plan_grants.",
    ),
    "deletion_unavailable": (
        503,
        "service_unavailable_error",
        "Deletion execution storage is unavailable.",
    ),
    "deletion_not_configured": (
        503,
        "service_unavailable_error",
        "Deletion execution is missing its database grants; apply deletion_execution_grants.",
    ),
    "deletion_object_storage_unconfigured": (
        503,
        "service_unavailable_error",
        "Deletion execution requires the object-store identity.",
    ),
    "deletion_reference_unsupported": (
        503,
        "service_unavailable_error",
        "A live dependency cannot be judged by this deployment.",
    ),
}


def deletion_error(
    code: str,
    request_id: str,
    *,
    param: str | None = None,
) -> JSONResponse:
    if code not in _ERRORS:
        # Storage diagnostics are not a public protocol; collapse an unexpected
        # internal code to the retryable storage-unavailable answer.
        code = "deletion_plans_unavailable"
    status, kind, message = _ERRORS[code]
    # Every deletion operation is safe to replay with the same key: a plan
    # replays its stored row and an accepted deletion replays its ledger row. The
    # header therefore answers the transport question ("may the SDK resend?"), not
    # the logical one; `error.retryable` still says a configuration gap will not
    # fix itself. The contract pins header `true` for 503 for the same reason.
    return JSONResponse(
        status_code=status,
        headers={
            "Cache-Control": "no-store",
            "X-Should-Retry": "true" if status == 503 else "false",
        },
        content=parse(
            "ErrorResponse",
            {
                "error": {
                    "code": code,
                    "type": kind,
                    "message": message,
                    "param": param,
                    "request_id": request_id,
                    # A missing grant or object-store identity is a deployment
                    # gap, not a transient fault: retrying the same request
                    # cannot fix it.
                    "retryable": status == 503
                    and code
                    not in (
                        "deletion_plans_not_configured",
                        "deletion_not_configured",
                        "deletion_object_storage_unconfigured",
                    ),
                }
            },
        ),
    )


__all__ = ["deletion_error"]
