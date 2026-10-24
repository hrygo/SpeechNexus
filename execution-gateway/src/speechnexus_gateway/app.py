"""ASGI composition root for the execution gateway."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from starlette.concurrency import run_in_threadpool

from speechnexus_gateway import __version__
from speechnexus_gateway.application.abort_execution import ExecutionAbortService
from speechnexus_gateway.application.barrier_control import GatewayBarrierService
from speechnexus_gateway.application.ingress import GatewayIngress
from speechnexus_gateway.application.instance_drain import InstanceDrainService
from speechnexus_gateway.application.query_execution import ExecutionQueryService
from speechnexus_gateway.application.readiness import (
    ReadinessProbe,
    ReadinessService,
)
from speechnexus_gateway.application.submit_execution import SubmitExecutionService
from speechnexus_gateway.infrastructure.postgres_pool import GatewayPool, gateway_pool
from speechnexus_gateway.infrastructure.process_ownership import ProcessIngressOwnership
from speechnexus_gateway.infrastructure.readiness import PostgresReadinessProbe
from speechnexus_gateway.infrastructure.submit_postgres import PostgresSubmissionLedger
from speechnexus_gateway.interfaces.http.barrier_control import create_barrier_router
from speechnexus_gateway.interfaces.http.control_errors import install_control_error_handlers
from speechnexus_gateway.interfaces.http.control_security import ComponentCredential
from speechnexus_gateway.interfaces.http.execution_query import create_execution_query_router
from speechnexus_gateway.interfaces.http.execution_submit import (
    create_execution_submit_router,
)
from speechnexus_gateway.interfaces.http.health import create_health_router
from speechnexus_gateway.interfaces.http.instance_drain import create_instance_drain_router
from speechnexus_gateway.interfaces.http.middleware import RequestIdMiddleware
from speechnexus_gateway.settings import Settings


class _ControlReadinessProbe:
    name = "gateway_ingress"

    def __init__(self, app: FastAPI) -> None:
        self._app = app

    async def check(self) -> None:
        service = self._app.state.barrier_control
        if not isinstance(service, GatewayBarrierService) or not isinstance(
            self._app.state.execution_query, ExecutionQueryService
        ):
            raise RuntimeError("gateway control is not running")
        await run_in_threadpool(service.check_ownership)


def _build_readiness(settings: Settings, app: FastAPI) -> ReadinessService:
    probes: list[ReadinessProbe] = [
        PostgresReadinessProbe(
            settings.control_database_url.get_secret_value(),
            timeout_seconds=settings.readiness_timeout_seconds,
        )
    ]
    if settings.execution_control_enabled:
        probes.append(_ControlReadinessProbe(app))
    return ReadinessService(probes, timeout_seconds=settings.readiness_timeout_seconds)


def _build_control(
    settings: Settings,
) -> tuple[
    GatewayBarrierService,
    ExecutionQueryService,
    InstanceDrainService,
    ProcessIngressOwnership,
    GatewayPool,
]:
    directory = settings.ingress_lock_directory
    if directory is None:
        raise ValueError("execution control requires a host lock directory")
    ownership = ProcessIngressOwnership(
        directory, node_id=settings.node_id, gateway_id=settings.gateway_id
    )
    pool = None
    try:
        pool = gateway_pool(
            settings.control_database_url.get_secret_value(), schema=settings.control_schema
        )
        pool.open()
        ingress = GatewayIngress(ownership, timeout_seconds=settings.control_timeout_seconds)
        ledger = PostgresSubmissionLedger(pool, timeout_seconds=settings.control_timeout_seconds)
        return (
            GatewayBarrierService(ingress=ingress, ledger=ledger),
            ExecutionQueryService(ingress=ingress, ledger=ledger),
            InstanceDrainService(ingress=ingress, ledger=ledger),
            ownership,
            pool,
        )
    except BaseException:
        ownership.close()
        if pool is not None:
            pool.close()
        raise


def create_app(
    *,
    settings: Settings | None = None,
    readiness: ReadinessService | None = None,
    barrier_control: GatewayBarrierService | None = None,
    execution_query: ExecutionQueryService | None = None,
    instance_drain: InstanceDrainService | None = None,
    submit_execution: SubmitExecutionService | None = None,
    abort_execution: ExecutionAbortService | None = None,
) -> FastAPI:
    resolved_settings = settings or Settings()
    if barrier_control is not None and not resolved_settings.execution_control_enabled:
        raise ValueError("injected execution control still requires explicit control configuration")
    if (barrier_control is None) != (execution_query is None):
        raise ValueError("inject both barrier and query services, or let the lifespan build both")
    if (submit_execution is None) != (abort_execution is None):
        raise ValueError("inject both engine submit and abort services, or neither")
    if submit_execution is not None and not resolved_settings.execution_control_enabled:
        raise ValueError("injected execution control still requires explicit control configuration")
    if instance_drain is not None and not resolved_settings.execution_control_enabled:
        raise ValueError("injected execution control still requires explicit control configuration")

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if not resolved_settings.execution_control_enabled or barrier_control is not None:
            yield
            return
        service, queries, drain, ownership, pool = await run_in_threadpool(
            _build_control, resolved_settings
        )
        app.state.barrier_control = service
        app.state.execution_query = queries
        app.state.instance_drain = drain
        try:
            yield
        finally:
            app.state.barrier_control = None
            app.state.execution_query = None
            app.state.instance_drain = None
            try:
                await run_in_threadpool(ownership.close)
            finally:
                await run_in_threadpool(pool.close)

    app = FastAPI(
        title="SpeechNexus execution gateway API",
        version=__version__,
        description=(
            "Operational endpoints, opt-in authenticated ingress barriers and recorded status. "
            "Engine submission and abort need a configured engine adapter: without one they "
            "answer with a retryable unavailability error and record nothing. An instance "
            "drain always reports the recorded in-flight work and only claims a safe stop "
            "from an empty inventory."
        ),
        lifespan=lifespan,
    )
    app.state.barrier_control = barrier_control
    app.state.execution_query = execution_query
    app.state.instance_drain = instance_drain
    app.state.submit_execution = submit_execution
    app.state.abort_execution = abort_execution
    resolved_readiness = readiness or _build_readiness(resolved_settings, app)
    app.add_middleware(RequestIdMiddleware)
    app.include_router(
        create_health_router(
            service_name=resolved_settings.service_name,
            version=__version__,
            readiness=resolved_readiness,
        )
    )
    if resolved_settings.component_token is not None:
        credential = ComponentCredential(resolved_settings.component_token)
        install_control_error_handlers(app)
        app.include_router(create_barrier_router(credential=credential))
        app.include_router(create_execution_query_router(credential=credential))
        app.include_router(create_execution_submit_router(credential=credential))
        app.include_router(create_instance_drain_router(credential=credential))
    app.state.settings = resolved_settings
    return app
