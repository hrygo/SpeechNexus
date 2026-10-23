"""Stable public errors; no payload, key, storage endpoint or SDK diagnostics."""

from __future__ import annotations

from speechnexus_contracts import parse
from starlette.responses import JSONResponse

# Public messages are deliberately fixed. Inspector/vendor errors have no path
# into the response; additional supported codes must be explicitly mapped here.
_ERRORS: dict[str, tuple[int, str, str]] = {
    "asset_host_rejected": (403, "permission_error", "The asset Host is not allowed."),
    "asset_origin_rejected": (403, "permission_error", "The asset Origin is not allowed."),
    "asset_method_not_allowed": (405, "invalid_request_error", "The asset method is not allowed."),
    "invalid_asset_request": (400, "invalid_request_error", "Invalid asset request."),
    "invalid_idempotency_key": (400, "invalid_request_error", "Invalid Idempotency-Key."),
    "asset_empty": (422, "invalid_request_error", "The asset is empty."),
    "asset_digest_mismatch": (422, "invalid_request_error", "The asset digest does not match."),
    "invalid_asset_text": (422, "invalid_request_error", "The asset is not valid UTF-8 text."),
    "asset_text_too_long": (422, "invalid_request_error", "The text exceeds the character limit."),
    "asset_too_large": (413, "invalid_request_error", "The asset exceeds the upload limit."),
    "asset_receive_timeout": (408, "invalid_request_error", "The upload receive deadline expired."),
    "asset_ingress_full": (429, "rate_limit_error", "The bounded upload capacity is full."),
    "asset_not_found": (404, "not_found_error", "The asset was not found."),
    "asset_unavailable": (410, "not_found_error", "The asset content is unavailable."),
    "workspace_not_found": (404, "not_found_error", "The workspace was not found."),
    "workspace_unavailable": (409, "invalid_request_error", "The workspace is unavailable."),
    "idempotency_key_conflict": (
        409,
        "invalid_request_error",
        "The key identifies another request.",
    ),
    "asset_upload_in_progress": (409, "api_error", "Query the original asset at Location."),
    "asset_upload_unknown": (503, "service_unavailable_error", "Reconcile the original upload."),
    "asset_metadata_unknown": (503, "service_unavailable_error", "Reconcile the original upload."),
    "asset_retention_not_configured": (
        503,
        "service_unavailable_error",
        "Set a TTL or configure retention.",
    ),
    "asset_inspection_unavailable": (
        503,
        "service_unavailable_error",
        "Asset inspection is unavailable.",
    ),
    "range_not_satisfiable": (416, "invalid_request_error", "The byte range is not satisfiable."),
    "asset_service_unavailable": (
        503,
        "service_unavailable_error",
        "Asset storage is unavailable.",
    ),
}


def asset_error(
    code: str,
    request_id: str,
    *,
    headers: dict[str, str] | None = None,
    param: str | None = None,
) -> JSONResponse:
    # operation_in_progress belongs to OperationRecovery and requires a real
    # Operation ID. An asset-only pending upload must never invent one.
    if code == "operation_in_progress":
        code = "asset_upload_in_progress"
    if code not in _ERRORS:
        code = "asset_service_unavailable"
    status, kind, message = _ERRORS[code]
    response_headers = {"Cache-Control": "no-store", "X-Should-Retry": "false", **(headers or {})}
    if code == "asset_ingress_full":
        response_headers["Retry-After"] = "1"
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
                    "param": param,
                    "request_id": request_id,
                    "retryable": False,
                }
            },
        ),
    )
