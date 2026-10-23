"""ASGI composition root for the execution gateway."""

from __future__ import annotations

from fastapi import FastAPI

from speechnexus_gateway import __version__
from speechnexus_gateway.application.readiness import (
    ReadinessProbe,
    ReadinessService,
)
from speechnexus_gateway.infrastructure.readiness import PostgresReadinessProbe
from speechnexus_gateway.interfaces.http.health import create_health_router
from speechnexus_gateway.interfaces.http.middleware import RequestIdMiddleware
from speechnexus_gateway.settings import Settings


def _build_readiness(settings: Settings) -> ReadinessService:
    probes: list[ReadinessProbe] = [
        PostgresReadinessProbe(
            settings.control_database_url.get_secret_value(),
            timeout_seconds=settings.readiness_timeout_seconds,
        )
    ]
    return ReadinessService(probes, timeout_seconds=settings.readiness_timeout_seconds)


def create_app(
    *,
    settings: Settings | None = None,
    readiness: ReadinessService | None = None,
) -> FastAPI:
    resolved_settings = settings or Settings()
    resolved_readiness = readiness or _build_readiness(resolved_settings)
    app = FastAPI(
        title="SpeechNexus execution gateway API",
        version=__version__,
        description="Operational endpoints only; engine control routes are not implemented.",
    )
    app.add_middleware(RequestIdMiddleware)
    app.include_router(
        create_health_router(
            service_name=resolved_settings.service_name,
            version=__version__,
            readiness=resolved_readiness,
        )
    )
    app.state.settings = resolved_settings
    return app
