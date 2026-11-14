"""Stable public Job errors; storage and intent details never cross this boundary."""

from __future__ import annotations

from speechnexus_contracts import parse
from starlette.responses import JSONResponse

_ERRORS: dict[str, tuple[int, str, str]] = {
    "invalid_job_request": (400, "invalid_request_error", "Invalid Job request."),
    "job_request_too_large": (
        413,
        "invalid_request_error",
        "The Job request body exceeds the deployment receive limit.",
    ),
    "job_not_found": (404, "not_found_error", "The Job was not found."),
    "job_method_not_allowed": (405, "invalid_request_error", "The Job method is not allowed."),
    "idempotency_conflict": (
        409,
        "invalid_request_error",
        "The key identifies another Job intent.",
    ),
    "execution_cleanup_pending": (
        409,
        "invalid_request_error",
        "The original execution is not confirmed stopped.",
    ),
    "job_retry_not_allowed": (
        409,
        "invalid_request_error",
        "The Job state does not allow a retry.",
    ),
    "job_retry_unavailable": (
        409,
        "invalid_request_error",
        "The original Job intent is no longer available.",
    ),
    "voice_draft_stale_revision": (
        409,
        "invalid_request_error",
        "The draft revision is no longer current.",
    ),
    "voice_draft_not_generatable": (
        409,
        "invalid_request_error",
        "The draft lifecycle no longer accepts this operation.",
    ),
    "unsupported_parameter": (422, "invalid_request_error", "The Job request is not supported."),
    "job_kind_not_supported": (
        422,
        "invalid_request_error",
        "This execution unit does not accept this Job kind.",
    ),
    "workspace_not_found": (422, "invalid_request_error", "The workspace was not found."),
    "workspace_disabled": (422, "invalid_request_error", "The workspace is disabled."),
    # The workspace row exists but has no ``workspace_quotas`` row: admission has
    # no reservation to charge, and no retry can produce one. This is a
    # deterministic configuration refusal, deliberately distinct from the 503
    # ``job_unavailable`` storage-outage code.
    "workspace_not_configured": (
        422,
        "invalid_request_error",
        "The workspace has no provisioned Job quota.",
    ),
    "model_not_published": (422, "invalid_request_error", "The model is not published."),
    "model_binding_not_published": (
        422,
        "invalid_request_error",
        "The selected binding revision is not published.",
    ),
    "model_binding_retired": (
        422,
        "invalid_request_error",
        "The selected binding revision is retired.",
    ),
    "model_binding_not_current": (
        422,
        "invalid_request_error",
        "The selected binding revision is no longer current.",
    ),
    "model_binding_mismatch": (
        422,
        "invalid_request_error",
        "The model does not match the selected binding.",
    ),
    "model_binding_task_not_supported": (
        422,
        "invalid_request_error",
        "The selected binding does not support file TTS.",
    ),
    "voice_not_published": (422, "invalid_request_error", "The voice revision is not published."),
    "profile_not_published": (
        422,
        "invalid_request_error",
        "The profile revision is not published.",
    ),
    "pronunciation_not_found": (
        422,
        "invalid_request_error",
        "The pronunciation revision was not found.",
    ),
    "asset_not_ready": (422, "invalid_request_error", "The input asset is not ready."),
    "voice_draft_not_candidate": (
        422,
        "invalid_request_error",
        "The draft revision is not a validation candidate.",
    ),
    "job_outcome_unknown": (
        503,
        "service_unavailable_error",
        "The original Job outcome is unknown.",
    ),
    "job_capacity_exhausted": (
        429,
        "rate_limit_error",
        "Job capacity is exhausted; retry after the indicated delay.",
    ),
    "job_binding_incomplete": (
        422,
        "invalid_request_error",
        "The model binding does not declare complete execution parameters.",
    ),
    "job_text_asset_unsupported": (
        422,
        "invalid_request_error",
        "Text asset input is not supported for TTS Jobs.",
    ),
    "job_unavailable": (503, "service_unavailable_error", "Job storage is unavailable."),
    "job_admission_contended": (
        503,
        "service_unavailable_error",
        "Admission state changed concurrently; retry the request.",
    ),
}


def job_error(code: str, request_id: str, *, headers: dict[str, str] | None = None) -> JSONResponse:
    if code not in _ERRORS:
        code = "job_unavailable"
    status, kind, message = _ERRORS[code]
    # Only an unknown execution outcome promises that retrying can succeed. An
    # unavailable read may be a permanently expired or deleted result, so clients
    # reconcile the original identity instead of retrying it blindly.
    retryable = code in (
        "job_outcome_unknown",
        "job_capacity_exhausted",
        "job_admission_contended",
    )
    response_headers = {"Cache-Control": "no-store", **(headers or {})}
    if code == "job_capacity_exhausted":
        # One second is the minimum claim-lease granularity (see
        # PostgresDispatchQueue: 1_000 <= lease_ms). A capacity refusal is
        # therefore retried no sooner than the next possible lease release.
        response_headers.setdefault("Retry-After", "1")
    return JSONResponse(
        status_code=status,
        headers=response_headers,
        content=parse(
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
        ),
    )
