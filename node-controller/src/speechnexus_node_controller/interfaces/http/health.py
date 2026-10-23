"""Operational health endpoints for the node controller."""

from __future__ import annotations

from fastapi import APIRouter, Response, status

from speechnexus_node_controller.application.readiness import ReadinessService
from speechnexus_node_controller.interfaces.http.schemas import (
    HealthResponse,
    ReadinessCheckResponse,
    ReadinessResponse,
)

_REQUEST_ID_RESPONSE_HEADER = {
    "X-Request-ID": {
        "description": "Validated client request ID or a server-generated correlation ID.",
        "schema": {"type": "string"},
    }
}


def create_health_router(
    *,
    service_name: str,
    version: str,
    readiness: ReadinessService,
) -> APIRouter:
    router = APIRouter(tags=["operations"])

    @router.get(
        "/healthz",
        operation_id="getHealth",
        response_model=HealthResponse,
        responses={200: {"headers": _REQUEST_ID_RESPONSE_HEADER}},
        summary="Report process liveness",
    )
    async def healthz() -> HealthResponse:
        return HealthResponse(service=service_name, version=version)

    @router.get(
        "/readyz",
        operation_id="getReadiness",
        response_model=ReadinessResponse,
        responses={
            200: {"headers": _REQUEST_ID_RESPONSE_HEADER},
            503: {
                "description": "A required node-local dependency is unavailable.",
                "headers": _REQUEST_ID_RESPONSE_HEADER,
                "model": ReadinessResponse,
            },
        },
        summary="Report dependency readiness",
    )
    async def readyz(response: Response) -> ReadinessResponse:
        report = await readiness.inspect()
        if not report.ready:
            response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return ReadinessResponse(
            status="ready" if report.ready else "unready",
            checks=[
                ReadinessCheckResponse(
                    name=check.name,
                    status=check.status.value,
                    detail=check.detail,
                )
                for check in report.checks
            ],
        )

    return router
