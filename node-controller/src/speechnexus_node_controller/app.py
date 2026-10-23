"""ASGI composition root for the restricted node controller."""

from __future__ import annotations

from fastapi import FastAPI

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


def _build_dependencies(settings: Settings) -> tuple[NodeRuntimeControlService, ReadinessService]:
    """Wire the approved templates, durable intent facts, and the local daemon."""
    api = DockerApi(
        socket_path=str(settings.docker_socket_path),
        timeout_seconds=settings.docker_request_timeout_seconds,
    )
    store = SqliteIntentStore(settings.intent_store_path)
    service = NodeRuntimeControlService(
        node_id=settings.node_id,
        templates=ApprovedTemplateRegistry(
            load_approved_templates(settings.approved_templates_file)
        ),
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
    return service, readiness


def create_app(
    *,
    settings: Settings | None = None,
    service: NodeRuntimeControlService | None = None,
    readiness: ReadinessService | None = None,
) -> FastAPI:
    """Create the controller app, with explicit dependencies for tests."""
    resolved_settings = settings or Settings()
    if service is None and readiness is None:
        service, readiness = _build_dependencies(resolved_settings)
    elif service is None or readiness is None:
        raise ValueError(
            "create_app takes either both a runtime control service and a readiness "
            "service, or neither"
        )

    app = FastAPI(
        title="SpeechNexus internal runtime lifecycle control API",
        version=__version__,
        description=(
            "Internal runtime lifecycle control for one node; approved templates only. "
            "Operational health endpoints are shared with the other deployments."
        ),
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
    return app
