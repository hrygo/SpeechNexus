"""ASGI composition root for the restricted node controller."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from starlette.concurrency import run_in_threadpool

from speechnexus_node_controller import __version__
from speechnexus_node_controller.application.readiness import ReadinessService
from speechnexus_node_controller.application.runtime_control import (
    NodeRuntimeControlService,
)
from speechnexus_node_controller.domain.templates import ApprovedTemplateRegistry
from speechnexus_node_controller.infrastructure.docker_api import DockerApi
from speechnexus_node_controller.infrastructure.docker_runtime import (
    DockerNodeRuntime,
    DockerRuntimeSettings,
)
from speechnexus_node_controller.infrastructure.local_observer import LocalObservationLoop
from speechnexus_node_controller.infrastructure.readiness import (
    DockerDaemonReadinessProbe,
    IntentStoreReadinessProbe,
)
from speechnexus_node_controller.infrastructure.sqlite_intent_store import (
    SqliteIntentStore,
)
from speechnexus_node_controller.infrastructure.template_config import (
    load_approved_templates,
)
from speechnexus_node_controller.interfaces.http.errors import install_error_handlers
from speechnexus_node_controller.interfaces.http.health import create_health_router
from speechnexus_node_controller.interfaces.http.middleware import RequestIdMiddleware
from speechnexus_node_controller.interfaces.http.runtime_control import (
    create_runtime_control_router,
)
from speechnexus_node_controller.interfaces.http.security import ComponentCredential
from speechnexus_node_controller.settings import Settings


def _build_dependencies(
    settings: Settings,
) -> tuple[NodeRuntimeControlService, ReadinessService, SqliteIntentStore, DockerApi]:
    """Wire the approved templates, durable intent facts, and the local daemon."""
    templates = ApprovedTemplateRegistry(load_approved_templates(settings.approved_templates_file))
    api = DockerApi(
        socket_path=str(settings.docker_socket_path),
        timeout_seconds=settings.docker_request_timeout_seconds,
    )
    try:
        store = SqliteIntentStore(settings.intent_store_path)
    except BaseException:
        api.close()
        raise
    service = NodeRuntimeControlService(
        node_id=settings.node_id,
        templates=templates,
        store=store,
        runtime=DockerNodeRuntime(
            api=api,
            node_id=settings.node_id,
            settings=DockerRuntimeSettings(
                stop_timeout_seconds=settings.stop_timeout_seconds,
                exit_wait_seconds=settings.exit_wait_seconds,
            ),
        ),
    )
    readiness = ReadinessService(
        [DockerDaemonReadinessProbe(api), IntentStoreReadinessProbe(store)],
        timeout_seconds=settings.readiness_timeout_seconds,
    )
    return service, readiness, store, api


def create_app(
    *,
    settings: Settings | None = None,
    service: NodeRuntimeControlService | None = None,
    readiness: ReadinessService | None = None,
) -> FastAPI:
    """Create the controller app, with explicit dependencies for tests."""
    resolved_settings = settings or Settings()
    owned_store: SqliteIntentStore | None = None
    owned_api: DockerApi | None = None
    if service is None and readiness is None:
        service, readiness, owned_store, owned_api = _build_dependencies(resolved_settings)
    elif service is None or readiness is None:
        raise ValueError(
            "create_app takes either both a runtime control service and a readiness "
            "service, or neither"
        )

    observer = (
        LocalObservationLoop(
            service,
            interval_seconds=resolved_settings.observation_interval_seconds,
            batch_size=resolved_settings.observation_batch_size,
        )
        if resolved_settings.local_observer_enabled
        else None
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        try:
            if observer is not None:
                observer.start()
            yield
        finally:
            try:
                if observer is not None:
                    await run_in_threadpool(observer.close)
            finally:
                try:
                    if owned_store is not None:
                        await run_in_threadpool(owned_store.close)
                finally:
                    if owned_api is not None:
                        await run_in_threadpool(owned_api.close)

    app = FastAPI(
        title="SpeechNexus internal runtime lifecycle control API",
        version=__version__,
        description=(
            "Internal runtime lifecycle control for one node; approved templates only. "
            "Operational health endpoints are shared with the other deployments."
        ),
        lifespan=lifespan,
    )
    app.add_middleware(RequestIdMiddleware)
    install_error_handlers(app)
    app.include_router(
        create_health_router(
            service_name=resolved_settings.service_name,
            version=__version__,
            readiness=readiness,
        )
    )
    app.include_router(
        create_runtime_control_router(
            service=service,
            credential=ComponentCredential(resolved_settings.component_token),
        )
    )
    app.state.settings = resolved_settings
    app.state.runtime_control = service
    app.state.local_observer = observer
    return app
