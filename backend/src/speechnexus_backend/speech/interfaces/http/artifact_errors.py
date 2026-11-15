"""Stable public Artifact errors; object and database diagnostics never cross."""

from __future__ import annotations

from speechnexus_contracts import parse
from starlette.responses import JSONResponse

_ERRORS: dict[str, tuple[int, str, str]] = {
    "artifact_not_found": (404, "not_found_error", "The artifact was not found."),
    # An identifier inside the minted result space with no committed row is a
    # publication that has not committed (its bytes stay in private staging),
    # not an identifier this API never issued. Both 404s stay distinguishable.
    "artifact_not_published": (
        404,
        "not_found_error",
        "The artifact has no committed publication in this workspace.",
    ),
    # The retention window ended: the metadata row still exists and stays
    # readable, so this refusal names the retention decision rather than
    # claiming the artifact never existed.
    "artifact_expired": (
        410,
        "not_found_error",
        "The artifact content retention period has ended.",
    ),
    # Retention cleanup removed the content after the window ended. This is a
    # different fact from `expired`: the bytes are gone, not merely unserved.
    "artifact_deleted": (
        410,
        "not_found_error",
        "The artifact content was removed after its retention period.",
    ),
    "artifact_unavailable": (410, "not_found_error", "The artifact content is unavailable."),
    "artifact_range_not_satisfiable": (
        416,
        "invalid_request_error",
        "The byte range is not satisfiable.",
    ),
    "artifact_service_unavailable": (
        503,
        "service_unavailable_error",
        "The artifact service is unavailable.",
    ),
    "artifact_storage_unavailable": (
        503,
        "service_unavailable_error",
        "Artifact storage is unavailable.",
    ),
    "artifact_method_not_allowed": (
        405,
        "invalid_request_error",
        "The artifact method is not allowed.",
    ),
}


def artifact_error(
    code: str,
    request_id: str,
    *,
    headers: dict[str, str] | None = None,
    param: str | None = None,
) -> JSONResponse:
    if code not in _ERRORS:
        # Storage SDK and database diagnostics are not a public protocol. An
        # unexpected internal code may be a key, version or endpoint detail, so
        # collapse it to the retryable storage-unavailable response.
        code = "artifact_storage_unavailable"
    status, kind, message = _ERRORS[code]
    # Publication may still commit, so the same request may succeed later. The
    # three terminal content refusals never become available again.
    retryable = code in (
        "artifact_service_unavailable",
        "artifact_storage_unavailable",
        "artifact_not_published",
    )
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


__all__ = ["artifact_error"]
