"""ASGI composition root for the speech backend."""

from __future__ import annotations

import copy
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exception_handlers import http_exception_handler
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException
from starlette.responses import Response

from speechnexus_backend import __version__
from speechnexus_backend.asset_runtime import AssetRuntime
from speechnexus_backend.settings import Settings
from speechnexus_backend.speech.application.assets import AssetService
from speechnexus_backend.speech.application.readiness import (
    ReadinessProbe,
    ReadinessService,
)
from speechnexus_backend.speech.infrastructure.readiness import (
    ObjectStorageReadinessProbe,
    PostgresReadinessProbe,
)
from speechnexus_backend.speech.interfaces.http.asset_contract import asset_http_contract
from speechnexus_backend.speech.interfaces.http.asset_errors import asset_error
from speechnexus_backend.speech.interfaces.http.asset_ingress import AssetIngress
from speechnexus_backend.speech.interfaces.http.assets import create_asset_router
from speechnexus_backend.speech.interfaces.http.health import create_health_router
from speechnexus_backend.speech.interfaces.http.middleware import RequestIdMiddleware


class _SpeechAPI(FastAPI):
    _asset_contract_added = False

    def openapi(self) -> dict[str, Any]:
        document = super().openapi()
        if not self._asset_contract_added:
            surface = copy.deepcopy(asset_http_contract())
            # Override generated path docs with the complete reviewed schemas,
            # including conditionals. Only these three implemented paths exist
            # in the projection; other speech operations are never advertised.
            document["paths"].update(surface["paths"])
            components = document.setdefault("components", {})
            for category, definitions in surface["components"].items():
                namespace = components.setdefault(category, {})
                for name, definition in definitions.items():
                    if name in namespace and namespace[name] != definition:
                        raise ValueError("Conflicting API contract component")
                    namespace[name] = definition
            self._asset_contract_added = True
        return document


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
    assets: AssetService | None = None,
) -> FastAPI:
    """Create an API instance with explicit dependencies for tests and deployment."""

    resolved_settings = settings or Settings()
    resolved_readiness = readiness or _build_readiness(resolved_settings)
    runtime = (
        AssetRuntime(resolved_settings)
        if assets is None and resolved_settings.object_storage_access_key is not None
        else None
    )
    asset_service = assets or (runtime.service if runtime is not None else None)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if runtime is not None:
            await run_in_threadpool(runtime.open)
        try:
            if runtime is not None:
                # Replay a bounded original/ledger pass before accepting traffic.
                # Every subsequent read independently enforces persisted expiry.
                await run_in_threadpool(
                    runtime.service.maintain, resolved_settings.asset_maintenance_batch_size
                )
            yield
        finally:
            if runtime is not None:
                await run_in_threadpool(runtime.close)

    app = _SpeechAPI(
        title="SpeechNexus operational and asset API",
        version=__version__,
        description="Operational endpoints and bounded input-asset publication.",
        lifespan=lifespan,
    )
    app.add_middleware(
        AssetIngress,
        max_bytes=resolved_settings.asset_max_bytes,
        concurrent_uploads=resolved_settings.asset_upload_concurrency,
        receive_timeout_seconds=resolved_settings.asset_receive_timeout_seconds,
        is_enabled=lambda: asset_service is not None,
        allowed_hosts=resolved_settings.asset_allowed_hosts,
        allowed_origins=resolved_settings.asset_allowed_origins,
    )
    # Request IDs wrap early ingress errors as well as route responses.
    app.add_middleware(RequestIdMiddleware)
    app.include_router(create_asset_router(lambda: asset_service))

    @app.exception_handler(HTTPException)
    async def asset_http_error(request: Request, error: HTTPException) -> Response:
        path = request.scope["path"].removeprefix(request.scope.get("root_path", ""))
        if (
            path.startswith("/v1/nexus/assets/") or path == "/v1/nexus/assets"
        ) and error.status_code in (404, 405):
            code = "asset_not_found" if error.status_code == 404 else "asset_method_not_allowed"
            headers = (
                {"Allow": error.headers["Allow"]}
                if error.headers and "Allow" in error.headers
                else {}
            )
            return asset_error(code, request.state.request_id, headers=headers)
        return await http_exception_handler(request, error)

    app.include_router(
        create_health_router(
            service_name=resolved_settings.service_name,
            version=__version__,
            readiness=resolved_readiness,
        )
    )
    app.state.settings = resolved_settings
    return app
