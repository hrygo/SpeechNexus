"""ASGI composition root for the restricted node controller."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI
from starlette.concurrency import run_in_threadpool

from speechnexus_node_controller import __version__
from speechnexus_node_controller.application.readiness import (
    ReadinessProbe,
    ReadinessService,
)
from speechnexus_node_controller.application.runtime_control import (
    NodeRuntimeControlService,
)
from speechnexus_node_controller.domain.ports import NodeRuntimePort
from speechnexus_node_controller.domain.templates import (
    ApprovedTemplateRegistry,
    EngineAuthKind,
)
from speechnexus_node_controller.infrastructure.docker_api import DockerApi
from speechnexus_node_controller.infrastructure.docker_runtime import (
    DockerNodeRuntime,
    DockerRuntimeSettings,
)
from speechnexus_node_controller.infrastructure.engine_credentials import EngineCredentials
from speechnexus_node_controller.infrastructure.kubernetes_api import (
    KubernetesApi,
    KubernetesApiSettings,
)
from speechnexus_node_controller.infrastructure.kubernetes_runtime import (
    KubernetesNodeRuntime,
    KubernetesRuntimeSettings,
)
from speechnexus_node_controller.infrastructure.local_observer import LocalObservationLoop
from speechnexus_node_controller.infrastructure.readiness import (
    DockerDaemonReadinessProbe,
    IntentStoreReadinessProbe,
    KubernetesApiReadinessProbe,
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
from speechnexus_node_controller.settings import KUBERNETES_RUNTIME, Settings

# The driver is chosen once, here. Neither branch falls back to the other: a
# deployment that names ``k8s`` and cannot reach the cluster must refuse, not
# start Docker containers while reporting a cluster-backed node.
RuntimeApi = DockerApi | KubernetesApi
RuntimeFactory = Callable[[SqliteIntentStore], NodeRuntimePort]


def _build_dependencies(
    settings: Settings,
) -> tuple[NodeRuntimeControlService, ReadinessService, SqliteIntentStore, RuntimeApi]:
    """Wire the approved templates, durable intent facts, and the selected driver."""
    approved = load_approved_templates(settings.approved_templates_file)
    templates = ApprovedTemplateRegistry(approved)
    credentials = (
        EngineCredentials(settings.engine_auth_root_key.get_secret_value())
        if settings.engine_auth_root_key is not None
        else None
    )
    if credentials is None and any(
        template.engine_auth is not EngineAuthKind.NONE for template in approved
    ):
        raise ValueError(
            "approved native engine templates require an engine authentication root key"
        )
    if credentials is not None and all(
        template.engine_auth is EngineAuthKind.NONE for template in approved
    ):
        # Nothing would ever read the key, so accepting it would report an
        # authenticated engine fleet that does not exist (#660).
        raise ValueError(
            "SPEECHNEXUS_CONTROLLER_ENGINE_AUTH_ROOT_KEY is configured but no approved "
            "template declares engine authentication; declare an authenticated engine "
            "template or unset the key"
        )
    api, probes, build_runtime = _runtime_driver(settings, credentials)
    try:
        store = SqliteIntentStore(settings.intent_store_path)
    except BaseException:
        api.close()
        raise
    service = NodeRuntimeControlService(
        node_id=settings.node_id,
        templates=templates,
        store=store,
        stop_checkpoints=store,
        runtime=build_runtime(store),
    )
    readiness = ReadinessService(
        [*probes, IntentStoreReadinessProbe(store)],
        timeout_seconds=settings.readiness_timeout_seconds,
    )
    return service, readiness, store, api


def _runtime_driver(
    settings: Settings,
    credentials: EngineCredentials | None,
) -> tuple[RuntimeApi, tuple[ReadinessProbe, ...], RuntimeFactory]:
    """Construct the selected driver's client, its readiness probes and its adapter.

    The client is built before the intent store so an unusable deployment (a
    missing ServiceAccount mount, an absent daemon socket) fails before any
    durable state is opened.
    """
    if settings.runtime == KUBERNETES_RUNTIME:
        # The settings validator requires both values for this driver; asserting
        # keeps the narrowing in one place instead of re-validating here.
        namespace = settings.k8s_namespace
        service_account_name = settings.k8s_service_account_name
        assert namespace is not None
        assert service_account_name is not None
        api = KubernetesApi(
            settings=KubernetesApiSettings(
                namespace=namespace,
                service_account_name=service_account_name,
                request_timeout_seconds=settings.k8s_request_timeout_seconds,
            )
        )

        def build_kubernetes(store: SqliteIntentStore) -> NodeRuntimePort:
            # Stop checkpoints arrive with the stop slice; the adapter takes the
            # same durable store then.
            return KubernetesNodeRuntime(
                api=api,
                node_id=settings.node_id,
                engine_credentials=credentials,
                settings=KubernetesRuntimeSettings(
                    namespace=namespace,
                    pod_ready_timeout_seconds=settings.k8s_pod_ready_timeout_seconds,
                    poll_interval_seconds=settings.k8s_pod_poll_interval_seconds,
                    gpu_memory_utilization=settings.engine_gpu_memory_utilization,
                ),
            )

        return api, (KubernetesApiReadinessProbe(api),), build_kubernetes

    docker_api = DockerApi(
        socket_path=str(settings.docker_socket_path),
        timeout_seconds=settings.docker_request_timeout_seconds,
    )

    def build_docker(store: SqliteIntentStore) -> NodeRuntimePort:
        return DockerNodeRuntime(
            api=docker_api,
            node_id=settings.node_id,
            stop_checkpoints=store,
            engine_credentials=credentials,
            settings=DockerRuntimeSettings(
                stop_timeout_seconds=settings.stop_timeout_seconds,
                exit_wait_seconds=settings.exit_wait_seconds,
                gpu_memory_mib=settings.engine_gpu_memory_mib,
                gpu_memory_utilization=settings.engine_gpu_memory_utilization,
                libvgpu_path=(
                    str(settings.engine_libvgpu_path)
                    if settings.engine_libvgpu_path is not None
                    else None
                ),
            ),
        )

    return docker_api, (DockerDaemonReadinessProbe(docker_api),), build_docker


def create_app(
    *,
    settings: Settings | None = None,
    service: NodeRuntimeControlService | None = None,
    readiness: ReadinessService | None = None,
) -> FastAPI:
    """Create the controller app, with explicit dependencies for tests."""
    resolved_settings = settings or Settings()
    owned_store: SqliteIntentStore | None = None
    owned_api: RuntimeApi | None = None
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
