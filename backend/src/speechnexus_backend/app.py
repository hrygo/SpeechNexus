"""ASGI composition root for the speech backend."""

from __future__ import annotations

from fastapi import FastAPI

from speechnexus_backend import __version__
from speechnexus_backend.settings import Settings
from speechnexus_backend.speech.application.readiness import (
    ReadinessProbe,
    ReadinessService,
)
from speechnexus_backend.speech.infrastructure.readiness import (
    ObjectStorageReadinessProbe,
    PostgresReadinessProbe,
)
from speechnexus_backend.speech.interfaces.http.health import create_health_router
from speechnexus_backend.speech.interfaces.http.middleware import RequestIdMiddleware


def _build_readiness(settings: Settings) -> ReadinessService:
    probes: list[ReadinessProbe] = [
        PostgresReadinessProbe(
            settings.database_url.get_secret_value(),
            timeout_seconds=settings.readiness_timeout_seconds,
        ),
        ObjectStorageReadinessProbe(
            settings.object_storage_healthcheck_url,
            timeout_seconds=settings.readiness_timeout_seconds,
        ),
    ]
    return ReadinessService(probes, timeout_seconds=settings.readiness_timeout_seconds)


def create_app(
    *,
    settings: Settings | None = None,
    readiness: ReadinessService | None = None,
) -> FastAPI:
    """Create an API instance with explicit dependencies for tests and deployment."""

    resolved_settings = settings or Settings()
    resolved_readiness = readiness or _build_readiness(resolved_settings)
    app = FastAPI(
        title="SpeechNexus operational API",
        version=__version__,
        description="Operational endpoints only; speech contracts remain pending P0.",
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
