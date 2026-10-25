"""ASGI composition root for the execution gateway."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI
from starlette.concurrency import run_in_threadpool

from speechnexus_gateway import __version__
from speechnexus_gateway.application.abort_execution import ExecutionAbortService
from speechnexus_gateway.application.barrier_control import GatewayBarrierService
from speechnexus_gateway.application.command_dispatch import OriginalCommandDispatch
from speechnexus_gateway.application.ingress import GatewayIngress
from speechnexus_gateway.application.instance_drain import InstanceDrainService
from speechnexus_gateway.application.query_execution import ExecutionQueryService
from speechnexus_gateway.application.readiness import (
    ReadinessProbe,
    ReadinessService,
)
from speechnexus_gateway.application.recover_executions import ExecutionRecoveryService
from speechnexus_gateway.application.submit_execution import SubmitExecutionService
from speechnexus_gateway.domain.file_execution import FileExecutionLedger, FileExecutionRuntime
from speechnexus_gateway.infrastructure.postgres_pool import GatewayPool, gateway_pool
from speechnexus_gateway.infrastructure.process_ownership import ProcessIngressOwnership
from speechnexus_gateway.infrastructure.readiness import PostgresReadinessProbe
from speechnexus_gateway.infrastructure.recovery_configuration import load_recovery_configuration
from speechnexus_gateway.infrastructure.recovery_worker import RecoveryWorker
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
        if self._app.state.settings.recovery_config_file is not None:
            worker = self._app.state.recovery_worker
            if not isinstance(worker, RecoveryWorker) or not worker.running:
                raise RuntimeError("gateway recovery is not running")
        if self._app.state.file_execution_required:
            runtime = self._app.state.file_execution_runtime
            if runtime is None or not runtime.running:
                raise RuntimeError("file execution is not running")
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
    RecoveryWorker | None,
    GatewayIngress,
    PostgresSubmissionLedger,
]:
    configuration = load_recovery_configuration(settings)
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
        queries = ExecutionQueryService(ingress=ingress, ledger=ledger)
        worker = (
            None
            if configuration is None
            else RecoveryWorker(
                ExecutionRecoveryService(
                    ingress=ingress,
                    ledger=ledger,
                    submissions=queries,
                    terminations=configuration.source,
                ),
                interval_seconds=configuration.interval_seconds,
                batch_size=configuration.batch_size,
                pass_budget_seconds=configuration.pass_budget_seconds,
            )
        )
        return (
            GatewayBarrierService(ingress=ingress, ledger=ledger),
            queries,
            InstanceDrainService(ingress=ingress, ledger=ledger),
            ownership,
            pool,
            worker,
            ingress,
            ledger,
        )
    except BaseException:
        try:
            if pool is not None:
                pool.close()
        finally:
            ownership.close()
        raise


def _close_control(
    worker: RecoveryWorker | None,
    pool: GatewayPool,
    ownership: ProcessIngressOwnership,
    file_execution: FileExecutionRuntime | None = None,
) -> None:
    # One synchronous cleanup job retains ordering even if its async waiter is cancelled.
    # A failed join must retain resources/ownership, never leave a writer using closed state.
    if file_execution is not None:
        file_execution.close()
    if worker is not None:
        worker.close()
    try:
        pool.close()
    finally:
        ownership.close()


async def _join_lifecycle_call[T](call: Callable[[], T]) -> tuple[T, bool]:
    """Acquire the thread's result before propagating cancellation to its owner."""
    pending = asyncio.create_task(run_in_threadpool(call))
    cancelled = False
    while not pending.done():
        try:
            await asyncio.shield(pending)
        except asyncio.CancelledError:
            cancelled = True
        except Exception:
            break  # Retrieve the original failure from the completed task.
    return pending.result(), cancelled


async def _finish_control(
    worker: RecoveryWorker | None,
    pool: GatewayPool,
    ownership: ProcessIngressOwnership,
    file_execution: FileExecutionRuntime | None = None,
) -> None:
    if worker is not None:
        worker.request_stop()
    _, cancelled = await _join_lifecycle_call(
        lambda: _close_control(worker, pool, ownership, file_execution)
    )
    if cancelled:
        raise asyncio.CancelledError


def create_app(
    *,
    settings: Settings | None = None,
    readiness: ReadinessService | None = None,
    barrier_control: GatewayBarrierService | None = None,
    execution_query: ExecutionQueryService | None = None,
    instance_drain: InstanceDrainService | None = None,
    submit_execution: SubmitExecutionService | None = None,
    abort_execution: ExecutionAbortService | None = None,
    file_execution_factory: Callable[[GatewayIngress, FileExecutionLedger], FileExecutionRuntime]
    | None = None,
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
    if barrier_control is not None and resolved_settings.recovery_config_file is not None:
        raise ValueError("configured recovery requires the default control lifespan")
    if file_execution_factory is not None and (
        not resolved_settings.execution_control_enabled
        or any(
            service is not None
            for service in (
                barrier_control,
                execution_query,
                instance_drain,
                submit_execution,
                abort_execution,
            )
        )
    ):
        raise ValueError("file execution requires the default owned control lifespan")

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if not resolved_settings.execution_control_enabled or barrier_control is not None:
            yield
            return
        control, cancelled = await _join_lifecycle_call(lambda: _build_control(resolved_settings))
        service, queries, drain, ownership, pool, worker, ingress, ledger = control
        app.state.barrier_control = service
        app.state.execution_query = queries
        app.state.instance_drain = drain
        app.state.recovery_worker = worker
        file_execution: FileExecutionRuntime | None = None
        try:
            if cancelled:
                raise asyncio.CancelledError
            if file_execution_factory is not None:
                runtime, cancelled = await _join_lifecycle_call(
                    lambda: file_execution_factory(ingress, ledger)
                )
                file_execution = runtime
                app.state.file_execution_runtime = runtime
                if cancelled:
                    raise asyncio.CancelledError
                _, cancelled = await _join_lifecycle_call(runtime.open)
                if cancelled:
                    raise asyncio.CancelledError
                app.state.submit_execution = SubmitExecutionService(
                    ledger=ledger,
                    ingress=ingress,
                    dispatch=OriginalCommandDispatch(
                        commands=ledger,
                        engine=runtime,
                        max_document_bytes=runtime.max_command_bytes,
                    ),
                    observation=runtime,
                    committed_execution=runtime,
                )
                app.state.abort_execution = ExecutionAbortService(
                    ingress=ingress, ledger=ledger, abort=runtime
                )
            if worker is not None:
                worker.start()
            yield
        finally:
            try:
                await _finish_control(worker, pool, ownership, file_execution)
            finally:
                app.state.barrier_control = None
                app.state.execution_query = None
                app.state.instance_drain = None
                app.state.recovery_worker = None
                app.state.file_execution_runtime = None
                app.state.submit_execution = None
                app.state.abort_execution = None

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
    app.state.recovery_worker = None
    app.state.file_execution_required = file_execution_factory is not None
    app.state.file_execution_runtime = None
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
