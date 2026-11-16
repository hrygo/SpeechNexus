"""Liveness and dependency readiness endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Response, status

from speechnexus_backend.speech.application.readiness import ReadinessService
from speechnexus_backend.speech.interfaces.http.schemas import (
    HealthResponse,
    ReadinessCheckResponse,
    ReadinessResponse,
)
from speechnexus_backend.speech.interfaces.http.version_identity import (
    create_version_identity_router,
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
    # The composition root already hands this factory the API version, and it is
    # the one operations surface app.py mounts unconditionally, so the version
    # identity route is attached here instead of taking a second wiring path
    # through app.py (#853). Its contract lives in contracts/speech-openapi.yaml.
    router.include_router(create_version_identity_router(version=version))

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
                "description": "At least one required dependency is unavailable.",
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
