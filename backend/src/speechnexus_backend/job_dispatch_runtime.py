"""Composition and owned lifetime for the public Job dispatch loop.

This is deliberately outside ``create_app``: dispatch is a long-lived worker,
not a request-scoped component. The production planner is still a separate
assembly step; the result publisher is built here and its absence is an explicit
startup failure instead of a fake implementation or a silent no-op.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any, Literal, Self, cast
from urllib.parse import urlsplit

from psycopg.conninfo import conninfo_to_dict
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
from speechnexus_backend.speech.domain.job_dispatch import DispatchLimits, identifier
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
)
from speechnexus_backend.speech.infrastructure.postgres_pool import SpeechPool, speech_pool


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
        parsed = urlsplit(str(self.gateway_base_url))
        if (
            not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.path not in ("", "/")
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("Dispatch gateway requires an origin without credentials or path")
        if parsed.scheme == "http" and parsed.hostname not in ("127.0.0.1", "::1", "localhost"):
            raise ValueError("Dispatch gateway plain HTTP is allowed only for loopback")
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
        close=close,
    )
    try:
        publisher.check_ready()
    except BaseException:
        close()
        raise
    return publisher


def build_dispatch_dependencies(
    settings: JobDispatchSettings,
) -> tuple[DispatchPlanner, ResultPublisher]:
    """Return the production planner/publisher, or fail closed.

    Result publication is assembled by ``build_result_publisher``. The
    production planner remains a separate card; starting a worker without it
    would claim Jobs and abandon them, so startup is still refused.
    """

    del settings
    raise DispatchDependenciesUnavailableError("Job dispatch planner is not assembled")


class JobDispatchRuntime:
    """Own the worker pool, gateway client and dispatcher lifetime."""

    def __init__(
        self,
        settings: JobDispatchSettings,
        planner: DispatchPlanner,
        results: ResultPublisher,
        *,
        gateway: ExecutionGatewayAdapter | None = None,
    ) -> None:
        if not isinstance(settings, JobDispatchSettings):
            raise ValueError("Dispatch requires explicit settings")
        if planner is None or results is None:
            raise DispatchDependenciesUnavailableError(
                "Job dispatch planner and result publisher are required"
            )
        self.settings = settings
        self._planner, self._results = planner, results
        self._pool: SpeechPool = speech_pool(
            settings.database_url.get_secret_value(), schema=settings.database_schema
        )
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
            self.dispatcher = JobDispatcher(
                PostgresDispatchQueue(
                    self._pool, timeout_seconds=self.settings.storage_timeout_seconds
                ),
                PostgresDispatchStateStore(
                    self._pool, timeout_seconds=self.settings.storage_timeout_seconds
                ),
                self._planner,
                self._gateway,
                self._results,
                limits=self.settings.limits,
            )
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
            await asyncio.to_thread(self._pool.close)

    def _check_role(self) -> None:
        # Startup owner/DDL guard; deployment still provisions the dedicated DML
        # role and its column grants separately.
        with self._pool.connection(timeout=self.settings.storage_timeout_seconds) as connection:
            connection.execute(
                "SELECT set_config('statement_timeout', %s, true)",
                (str(max(1, int(self.settings.storage_timeout_seconds * 1000))),),
            )
            identities = connection.execute(
                "SELECT SESSION_USER AS login, CURRENT_USER AS effective"
            ).fetchone()
            if identities is None:
                raise ValueError("Dispatch requires a non-owner role without DDL")
            for role in dict.fromkeys((identities["login"], identities["effective"])):
                row = connection.execute(
                    "SELECT r.rolsuper OR pg_catalog.has_schema_privilege(%s::name,%s,'CREATE') "
                    "OR pg_catalog.pg_has_role(%s::name,d.datdba,'MEMBER') OR EXISTS ("
                    "SELECT 1 FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n "
                    "ON n.oid=c.relnamespace WHERE n.nspname=%s "
                    "AND pg_catalog.pg_has_role(%s::name,c.relowner,'MEMBER')) AS forbidden "
                    "FROM pg_catalog.pg_roles r CROSS JOIN pg_catalog.pg_database d "
                    "WHERE r.rolname=%s AND d.datname=CURRENT_DATABASE()",
                    (
                        role,
                        self.settings.database_schema,
                        role,
                        self.settings.database_schema,
                        role,
                        role,
                    ),
                ).fetchone()
                if row is None or row["forbidden"]:
                    raise ValueError("Dispatch requires a non-owner role without DDL")
            for statement in (
                "SELECT job_id FROM speech_jobs LIMIT 0",
                "SELECT job_id, event_seq, state, occurred_at_us FROM speech_job_events LIMIT 0",
                "SELECT job_id FROM coordination_jobs LIMIT 0",
                "SELECT operation_id, request_document FROM admission_operations LIMIT 0",
                "SELECT artifact_id FROM speech_artifacts LIMIT 0",
            ):
                connection.execute(statement)


__all__ = [
    "DispatchDependenciesUnavailableError",
    "JobDispatchRuntime",
    "JobDispatchSettings",
    "build_dispatch_dependencies",
    "build_result_publisher",
]
