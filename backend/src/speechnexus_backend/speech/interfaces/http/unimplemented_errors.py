"""Stable error for a contract operation this deployment does not implement.

The frozen speech contract declares operations whose owning feature is not
built yet. Without a registered route the framework answers a bare
``{"detail": "Not Found"}`` (or an ingress-specific 400/405), so a consumer
cannot tell "not implemented here" from "bad request", "unknown resource" or a
real outage. This map is deliberately single-entry: a placeholder route can
only ever answer ``operation_not_implemented``, and a real failure keeps its
own code such as ``backend_unavailable``.
"""

from __future__ import annotations

from speechnexus_contracts import parse
from starlette.responses import JSONResponse

_ERRORS: dict[str, tuple[int, str, str, bool]] = {
    "operation_not_implemented": (
        503,
        "service_unavailable_error",
        "This operation is declared by the contract and not implemented by this deployment.",
        False,
    ),
}

# Derived, so the exported code cannot drift from the table the conformance
# gate parses statically.
OPERATION_NOT_IMPLEMENTED = next(iter(_ERRORS))


def operation_not_implemented_error(code: str, request_id: str) -> JSONResponse:
    status, kind, message, retryable = _ERRORS[code]
    return JSONResponse(
        status_code=status,
        headers={"Cache-Control": "no-store", "X-Should-Retry": str(retryable).lower()},
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


__all__ = ["OPERATION_NOT_IMPLEMENTED", "operation_not_implemented_error"]
