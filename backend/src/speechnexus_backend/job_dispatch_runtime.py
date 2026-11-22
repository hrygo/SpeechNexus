"""Composition and owned lifetime for the public Job dispatch loop.

This is deliberately outside ``create_app``: dispatch is a long-lived worker,
not a request-scoped component. The production planner is still a separate
assembly step; the result publisher is built here and its absence is an explicit
startup failure instead of a fake implementation or a silent no-op.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal, Self, cast
from urllib.parse import urlsplit

from pydantic import AnyHttpUrl, Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from speechnexus_backend.speech.application.job_dispatcher import (
    DispatchPlanner,
    JobDispatcher,
    ResultPublisher,
)
from speechnexus_backend.speech.application.results import (
    MAX_RESULT_BYTES,
    ResultArtifactPublisher,
)
from speechnexus_backend.speech.domain.artifacts import ArtifactError
from speechnexus_backend.speech.domain.gateway_origin import require_safe_gateway_origin
from speechnexus_backend.speech.domain.job_dispatch import (
    DISPATCHABLE_JOB_KIND_UNION,
    DISPATCHABLE_JOB_KINDS,
    DispatchLimits,
    identifier,
)
from speechnexus_backend.speech.infrastructure.artifacts_minio import MinioArtifactPublisher
from speechnexus_backend.speech.infrastructure.artifacts_postgres import PostgresArtifactWriter
from speechnexus_backend.speech.infrastructure.execution_gateway_adapter import (
    ExecutionGatewayAdapter,
)
from speechnexus_backend.speech.infrastructure.gateway_execution_client import (
    GatewayExecutionClient,
    GatewayResultObjectError,
    MinioExecutionResultObjects,
)
from speechnexus_backend.speech.infrastructure.job_dispatch_postgres import (
    PostgresDispatchQueue,
    PostgresDispatchStateStore,
    PostgresLeaseSettler,
)
from speechnexus_backend.speech.infrastructure.postgres_pool import SpeechPool, speech_pool
from speechnexus_backend.speech.infrastructure.stored_command_planner import (
    StoredCommandPlanner,
)
from speechnexus_backend.speech.infrastructure.voice_validation_run_postgres import (
    PostgresValidationRunFacts,
)
from speechnexus_portable_sql.vendor import conninfo_to_dict


class DispatchDependenciesUnavailableError(RuntimeError):
    """The planner/result publisher required by the dispatcher are not assembled."""


class JobDispatchSettings(BaseSettings):
    """Every operating value is explicit; a missing one prevents startup."""

    model_config = SettingsConfigDict(
        env_file=None,
        extra="ignore",
        populate_by_name=True,
        hide_input_in_errors=True,
        frozen=True,
    )

    process_role: Literal["job-worker"] = Field(validation_alias="SPEECHNEXUS_PROCESS_ROLE")
    node_id: str = Field(validation_alias="SPEECHNEXUS_NODE_ID")
    instance_id: str = Field(validation_alias="SPEECHNEXUS_INSTANCE_ID")
    database_url: SecretStr = Field(validation_alias="SPEECHNEXUS_DATABASE_URL")
    database_schema: str = Field(
        validation_alias="SPEECHNEXUS_DATABASE_SCHEMA",
        pattern=r"^[a-z][a-z0-9_]{2,62}$",
    )

    gateway_base_url: AnyHttpUrl = Field(
        validation_alias="SPEECHNEXUS_EXECUTION_GATEWAY_BASE_URL", repr=False
    )
    allow_plaintext_origin: str = Field(
        default="", validation_alias="SPEECHNEXUS_EXECUTION_GATEWAY_ALLOW_PLAINTEXT_ORIGIN"
    )
    gateway_credential: SecretStr = Field(
        validation_alias="SPEECHNEXUS_EXECUTION_GATEWAY_COMPONENT_CREDENTIAL",
        min_length=16,
        repr=False,
    )
    gateway_timeout_seconds: float = Field(
        validation_alias="SPEECHNEXUS_EXECUTION_GATEWAY_TIMEOUT_SECONDS",
        gt=0,
        le=60,
        allow_inf_nan=False,
    )
    batch_size: int = Field(validation_alias="SPEECHNEXUS_JOB_DISPATCH_BATCH_SIZE", ge=1, le=32)
    lease_ms: int = Field(validation_alias="SPEECHNEXUS_JOB_DISPATCH_LEASE_MS", ge=1_000, le=60_000)
    poll_seconds: float = Field(
        validation_alias="SPEECHNEXUS_JOB_DISPATCH_POLL_SECONDS",
        ge=0.01,
        le=30,
        allow_inf_nan=False,
    )
    job_timeout_seconds: float = Field(
        validation_alias="SPEECHNEXUS_JOB_DISPATCH_JOB_TIMEOUT_SECONDS",
        ge=0.01,
        le=3_600,
        allow_inf_nan=False,
    )
    max_attempts: int = Field(validation_alias="SPEECHNEXUS_JOB_DISPATCH_MAX_ATTEMPTS", ge=1, le=20)
    interval_seconds: float = Field(
        validation_alias="SPEECHNEXUS_JOB_DISPATCH_INTERVAL_SECONDS",
        ge=0.1,
        le=300,
        allow_inf_nan=False,
    )
    storage_timeout_seconds: float = Field(
        validation_alias="SPEECHNEXUS_JOB_DISPATCH_STORAGE_TIMEOUT_SECONDS",
        gt=0,
        le=30,
        allow_inf_nan=False,
    )
    # Retention window recorded on every Artifact this worker publishes. It is a
    # product default, not a machine-identity value: PRD F-ARTIFACT (docs/prd.md)
    # and the retention table in docs/architecture/runtime-governance.md §2.3 both
    # put a generated audio target at 30 days. A deployment may narrow or widen it
    # without touching the publication branch, and the value a row was published
    # with never changes afterwards.
    result_retention_seconds: int = Field(
        validation_alias="SPEECHNEXUS_ARTIFACT_RETENTION_SECONDS",
        default=30 * 24 * 60 * 60,
        ge=3_600,
        le=10 * 365 * 24 * 60 * 60,
    )
    dispatch_kinds: str = Field(
        validation_alias="SPEECHNEXUS_JOB_DISPATCH_KINDS",
        default=",".join(DISPATCHABLE_JOB_KINDS),
        min_length=1,
        max_length=256,
    )
    object_storage_endpoint: AnyHttpUrl = Field(
        validation_alias="SPEECHNEXUS_OBJECT_STORAGE_ENDPOINT", repr=False
    )
    artifacts_bucket: str = Field(
        validation_alias="SPEECHNEXUS_OBJECT_STORAGE_BUCKET",
        min_length=3,
        max_length=63,
        pattern=r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$",
    )
    execution_result_bucket: str = Field(
        validation_alias="SPEECHNEXUS_EXECUTION_RESULT_BUCKET",
        min_length=3,
        max_length=63,
        pattern=r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$",
    )
    object_storage_access_key: SecretStr = Field(
        validation_alias="SPEECHNEXUS_OBJECT_STORAGE_ACCESS_KEY", min_length=1, repr=False
    )
    object_storage_secret_key: SecretStr = Field(
        validation_alias="SPEECHNEXUS_OBJECT_STORAGE_SECRET_KEY", min_length=1, repr=False
    )

    @property
    def worker_id(self) -> str:
        return self.node_id + ":" + self.instance_id

    @property
    def kinds(self) -> tuple[str, ...]:
        selected = tuple(part.strip() for part in self.dispatch_kinds.split(",") if part.strip())
        if not selected or len(set(selected)) != len(selected):
            raise ValueError("Dispatch lane Job kinds must be a non-empty unique list")
        if any(kind not in DISPATCHABLE_JOB_KIND_UNION for kind in selected):
            raise ValueError("Dispatch lane Job kind is outside the dispatchable union")
        return selected

    @property
    def limits(self) -> DispatchLimits:
        return DispatchLimits(
            batch_size=self.batch_size,
            lease_ms=self.lease_ms,
            poll_seconds=self.poll_seconds,
            job_timeout_seconds=self.job_timeout_seconds,
            max_attempts=self.max_attempts,
        )

    @model_validator(mode="after")
    def explicit_bounded_configuration(self) -> Self:
        for value in (self.node_id, self.instance_id, self.worker_id):
            identifier(value)
        try:
            info = conninfo_to_dict(self.database_url.get_secret_value())
        except Exception:
            raise ValueError("Dispatch requires an explicit PostgreSQL connection") from None
        if not all(info.get(key) for key in ("user", "dbname")) or not (
            info.get("host") or info.get("hostaddr")
        ):
            raise ValueError("Dispatch PostgreSQL host, database and principal must be explicit")
        require_safe_gateway_origin(
            str(self.gateway_base_url),
            allow_plaintext_origin=self.allow_plaintext_origin,
            label="Dispatch gateway",
        )
        token = self.gateway_credential.get_secret_value()
        if len(token) > 4096 or any(not 33 <= ord(char) <= 126 for char in token):
            raise ValueError("Dispatch gateway credential must be visible ASCII")
        objects = urlsplit(str(self.object_storage_endpoint))
        if (
            not objects.hostname
            or objects.username
            or objects.password
            or objects.path not in ("", "/")
            or objects.query
            or objects.fragment
        ):
            raise ValueError(
                "Artifact object storage requires an origin without credentials or path"
            )
        if self.poll_seconds > self.job_timeout_seconds:
            raise ValueError("Dispatch polling cannot exceed the Job timeout")
        _ = self.kinds
        return self


class _StagingResultReader:
    """Adapt the verified gateway object reader to the publication port."""

    def __init__(self, objects: MinioExecutionResultObjects) -> None:
        self._objects = objects

    def read(self, reference: Mapping[str, Any], storage_version: str) -> bytes:
        try:
            return self._objects.read(cast(Any, dict(reference)), storage_version)
        except GatewayResultObjectError:
            raise ArtifactError("artifact_staging_unavailable") from None


def build_result_publisher(
    settings: JobDispatchSettings, pool: SpeechPool
) -> ResultArtifactPublisher:
    """Build the real publisher; object clients are closed together on failure."""
    endpoint = str(settings.object_storage_endpoint)
    access = settings.object_storage_access_key.get_secret_value()
    secret = settings.object_storage_secret_key.get_secret_value()
    artifacts = MinioArtifactPublisher(endpoint, settings.artifacts_bucket, access, secret)
    try:
        staging = MinioExecutionResultObjects(
            endpoint=endpoint,
            bucket=settings.execution_result_bucket,
            access_key=access,
            secret_key=secret,
            max_bytes=MAX_RESULT_BYTES,
        )
    except BaseException:
        artifacts.close()
        raise

    def close() -> None:
        artifacts.close()
        staging.close()

    publisher = ResultArtifactPublisher(
        PostgresArtifactWriter(pool, timeout_seconds=settings.storage_timeout_seconds),
        artifacts,
        _StagingResultReader(staging),
        retention_us=settings.result_retention_seconds * 1_000_000,
        validation=PostgresValidationRunFacts(
            pool, timeout_seconds=settings.storage_timeout_seconds
        ),
        report_reader=artifacts,
        close=close,
    )
    try:
        publisher.check_ready()
    except BaseException:
        close()
        raise
    return publisher


@dataclass(frozen=True)
class DispatchDependencies:
    """The assembled planner, publisher and the one pool they share."""

    planner: DispatchPlanner
    results: ResultPublisher
    pool: SpeechPool

    def close(self) -> None:
        try:
            close = getattr(self.results, "close", None)
            if callable(close):
                close()
        finally:
            self.pool.close()


def build_dispatch_dependencies(settings: JobDispatchSettings) -> DispatchDependencies:
    """Assemble the production planner and publisher, or refuse to start.

    A worker that claimed Jobs without a real planner or publisher would either
    abandon them or lose their results. Any broken prerequisite therefore raises
    ``DispatchDependenciesUnavailableError`` before the worker opens its database
    role, so no lease is ever claimed.
    """
    pool: SpeechPool | None = None
    try:
        pool = speech_pool(
            settings.database_url.get_secret_value(), schema=settings.database_schema
        )
        pool.open()
        planner = StoredCommandPlanner(pool, timeout_seconds=settings.storage_timeout_seconds)
        results = build_result_publisher(settings, pool)
    except Exception:
        if pool is not None:
            pool.close()
        raise DispatchDependenciesUnavailableError(
            "Job dispatch planner and result publisher are unavailable"
        ) from None
    return DispatchDependencies(planner, results, pool)


# Startup probes are module-level so the read-only operations preflight can compare
# its copies against this single declaration instead of drifting (see
# ``tests/unit/test_async_job_prereqs_check.py``). The statements run unqualified
# inside the pool's ``search_path``; a preflight running psql must set the same
# search_path. Order is the startup order.
DISPATCH_ROLE_GUARD_SQL = (
    "SELECT r.rolsuper OR pg_catalog.has_schema_privilege(CAST(%(role)s AS "
    "name),%(schema)s,'CREATE') "
    "OR pg_catalog.pg_has_role(CAST(%(role)s AS name),d.datdba,'MEMBER') OR EXISTS ("
    "SELECT 1 FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n "
    "ON n.oid=c.relnamespace WHERE n.nspname=%(schema)s "
    "AND pg_catalog.pg_has_role(CAST(%(role)s AS name),c.relowner,'MEMBER')) AS forbidden "
    "FROM pg_catalog.pg_roles r CROSS JOIN pg_catalog.pg_database d "
    "WHERE r.rolname=%(role)s AND d.datname=CURRENT_DATABASE()"
)
# 零行探针:只验证语句可解析、这些列对该角色可读(列级授权),不取任何行。
# 受限方言没有 `LIMIT`,而这里也没有分页语义(keyset + fetchmany 规则因此不适用),
# 所以用一个恒假谓词;规划器在计划阶段就剪掉扫描。
_ZERO_ROWS = " WHERE 1=0"
DISPATCH_STARTUP_PROBES: tuple[str, ...] = (
    "SELECT job_id FROM speech_jobs" + _ZERO_ROWS,
    "SELECT job_id, event_seq, state, occurred_at_us, kind, progress_unit, "
    "progress_completed, progress_total FROM speech_job_events" + _ZERO_ROWS,
    "SELECT job_id FROM coordination_jobs" + _ZERO_ROWS,
    "SELECT operation_id, request_document FROM admission_operations" + _ZERO_ROWS,
    "SELECT attempt_id FROM admission_attempts" + _ZERO_ROWS,
    "SELECT request_document FROM admission_requests" + _ZERO_ROWS,
    "SELECT lease_id, lease_state, cleanup_state, deadline_at_us FROM admission_leases"
    + _ZERO_ROWS,
    "SELECT artifact_id FROM speech_artifacts" + _ZERO_ROWS,
)
DISPATCH_VOICE_VALIDATION_PROBES: tuple[str, ...] = (
    "SELECT draft_id, revision FROM speech_voice_draft_revisions" + _ZERO_ROWS,
    "SELECT workspace_id, asset_id FROM speech_assets" + _ZERO_ROWS,
)


class JobDispatchRuntime:
    """Own the worker pool, gateway client and dispatcher lifetime."""

    def __init__(
        self,
        settings: JobDispatchSettings,
        dependencies: DispatchDependencies,
        *,
        gateway: ExecutionGatewayAdapter | None = None,
    ) -> None:
        if not isinstance(settings, JobDispatchSettings):
            raise ValueError("Dispatch requires explicit settings")
        if dependencies is None:
            raise DispatchDependenciesUnavailableError(
                "Job dispatch planner and result publisher are required"
            )
        self.settings = settings
        self._dependencies = dependencies
        self._pool = dependencies.pool
        self._gateway = gateway
        self._owns_gateway = gateway is None
        self._loaded = False
        self.dispatcher: JobDispatcher | None = None

    async def open(self) -> Self:
        self._pool.open()
        try:
            await asyncio.to_thread(self._check_role)
            if self._gateway is None:
                self._gateway = ExecutionGatewayAdapter(
                    GatewayExecutionClient(
                        base_url=str(self.settings.gateway_base_url),
                        credential=self.settings.gateway_credential,
                        timeout_seconds=self.settings.gateway_timeout_seconds,
                        max_retries=0,
                        backoff_seconds=0,
                    )
                )
            queue = PostgresDispatchQueue(
                self._pool,
                kinds=self.settings.kinds,
                require_admission=True,
                timeout_seconds=self.settings.storage_timeout_seconds,
            )
            if queue.require_admission is not True:
                # The production lane must never fall back to anchor-only claims.
                raise DispatchDependenciesUnavailableError(
                    "Job dispatch requires the admission claim filter"
                )
            dispatcher = JobDispatcher(
                queue,
                PostgresDispatchStateStore(
                    self._pool, timeout_seconds=self.settings.storage_timeout_seconds
                ),
                self._dependencies.planner,
                self._gateway,
                self._dependencies.results,
                limits=self.settings.limits,
            )
            dispatcher.settle_capacity_through(
                PostgresLeaseSettler(
                    self._pool, timeout_seconds=self.settings.storage_timeout_seconds
                )
            )
            self.dispatcher = dispatcher
            self._loaded = True
            return self
        except BaseException:
            await self.close()
            raise

    async def close(self) -> None:
        try:
            if self._gateway is not None and self._owns_gateway:
                await self._gateway.aclose()
        finally:
            self._gateway = None
            self._loaded = False
            self.dispatcher = None
            await asyncio.to_thread(self._dependencies.close)

    def _check_role(self) -> None:
        # Startup owner/DDL guard; deployment still provisions the dedicated DML
        # role and its column grants separately.
        with self._pool.connection(timeout=self.settings.storage_timeout_seconds) as connection:
            connection.execute(
                "SELECT set_config('statement_timeout', ?, true)",
                (str(max(1, int(self.settings.storage_timeout_seconds * 1000))),),
            )
            identities = connection.execute(
                "SELECT SESSION_USER AS login, CURRENT_USER AS effective"
            ).fetchone()
            if identities is None:
                raise ValueError("Dispatch requires a non-owner role without DDL")
            for role in dict.fromkeys((identities["login"], identities["effective"])):
                row = connection.execute(
                    DISPATCH_ROLE_GUARD_SQL,
                    {"role": role, "schema": self.settings.database_schema},
                ).fetchone()
                if row is None or row["forbidden"]:
                    raise ValueError("Dispatch requires a non-owner role without DDL")
            for statement in DISPATCH_STARTUP_PROBES:
                connection.execute(statement)
            # The validation lane reads two facts outside the dispatcher's base
            # grants. Probe them only when this process claims that kind so a
            # missing ``voice_validation_worker_grants`` refuses startup here,
            # before any dispatcher exists and before any lease can be claimed.
            if "voice_validation" in self.settings.kinds:
                for statement in DISPATCH_VOICE_VALIDATION_PROBES:
                    connection.execute(statement)


__all__ = [
    "DISPATCH_ROLE_GUARD_SQL",
    "DISPATCH_STARTUP_PROBES",
    "DISPATCH_VOICE_VALIDATION_PROBES",
    "DispatchDependencies",
    "DispatchDependenciesUnavailableError",
    "JobDispatchRuntime",
    "JobDispatchSettings",
    "build_dispatch_dependencies",
    "build_result_publisher",
]
