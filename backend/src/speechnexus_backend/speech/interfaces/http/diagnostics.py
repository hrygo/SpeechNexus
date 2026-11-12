"""Redacted diagnostics assembled from the readiness probes already in the process.

The snapshot carries only the aggregate readiness state, the generation time and
each probe's name, status and safe detail. Content, audio, feature vectors,
credentials, object keys and private model paths never enter the report: the
readiness probes already discard them, and this boundary adds no field.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

from fastapi import APIRouter, Request
from speechnexus_contracts.admin_http import validate_response
from starlette.responses import JSONResponse, Response

from speechnexus_backend.speech.application.readiness import (
    ProbeStatus,
    ReadinessReport,
    ReadinessService,
)

OPERATION = "getDiagnostics"
PATH = "/v1/nexus/admin/diagnostics"


def diagnostics_body(report: ReadinessReport, now: datetime) -> dict[str, object]:
    """Map the readiness report onto the declared diagnostics shape.

    This deployment reports ``ok`` when every probe passed and ``unready``
    otherwise. No probe distinguishes a reduced capability from a failed one, so
    the declared ``degraded`` status is never emitted.
    """
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("Diagnostics generation time requires a timezone")
    moment = now.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")
    return {
        "status": "ok" if report.ready else "unready",
        "generated_at": moment,
        "checks": [
            {
                "name": check.name,
                "status": "ok" if check.status is ProbeStatus.OK else "failed",
                "detail": check.detail,
            }
            for check in report.checks
        ],
    }


def unavailable_error(request_id: str) -> JSONResponse:
    body = {
        "error": {
            "code": "diagnostics_unavailable",
            "type": "service_unavailable_error",
            "message": "Diagnostics are unavailable.",
            "param": None,
            "request_id": request_id,
            "retryable": True,
        }
    }
    validate_response(OPERATION, 503, {"body": body})
    return JSONResponse(
        body,
        status_code=503,
        headers={"Cache-Control": "no-store", "X-Should-Retry": "true"},
    )


def create_diagnostics_router(
    readiness: ReadinessService,
    *,
    now: Callable[[], datetime] | None = None,
) -> APIRouter:
    router = APIRouter(tags=["diagnostics"])
    clock = now or (lambda: datetime.now(UTC))

    @router.get(PATH, operation_id=OPERATION)
    async def get_diagnostics(request: Request) -> Response:
        request_id = str(request.state.request_id)
        try:
            report = await readiness.inspect()
            body = diagnostics_body(report, clock())
            validate_response(
                OPERATION, 200, {"body": body, "headers": {"X-Request-ID": request_id}}
            )
        except Exception:
            # A diagnostics snapshot that cannot be assembled is unavailable, not
            # evidence that the deployment is ready; raw probe errors stay here.
            return unavailable_error(request_id)
        return JSONResponse(content=body, headers={"Cache-Control": "no-store"})

    return router
