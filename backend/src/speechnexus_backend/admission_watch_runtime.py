"""Composition root for the Job admission watch process.

This process is deliberately separate from the API app: it owns a short-lived
PostgreSQL pool, reads the report through the same service object the HTTP route
uses (``PublicJobs.admission_readiness``), and writes nothing. It never opens an
HTTP client against our own endpoint: the verdict is a projection over the rows
admission reads, so asking the process through loopback would need a credential and
a listening API to answer a question the library already answers.

It is disabled unless explicitly enabled, and ``open`` is the only place that creates
a pool. Disabled means *nothing*: no pool, no connection, no log line -- the same
switch shape as ``job_cancellation_runtime``. Once enabled it fails at open, naming
the missing value, rather than starting a loop that reports a verdict it cannot read.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid

import psycopg
from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from speechnexus_backend.speech.application.admission_watch import (
    AdmissionWatcher,
    AdmissionWatchObservation,
)
from speechnexus_backend.speech.application.public_jobs import DEFAULT_WORKSPACE, PublicJobs
from speechnexus_backend.speech.domain.public_jobs import identifier
from speechnexus_backend.speech.infrastructure.postgres_pool import SpeechPool, speech_pool
from speechnexus_backend.speech.infrastructure.public_jobs_postgres import (
    PostgresPublicJobs,
)

logger = logging.getLogger(__name__)

#: Every relation the report's own transaction reads. Naming them at open turns a
#: missing migration or a missing grant into a startup failure instead of a poll
#: whose verdict is a storage outage on a loop that never ends.
REQUIRED_TABLES = (
    "speech_workspaces",
    "workspace_quotas",
    "speech_publications",
    "speech_publication_heads",
    "speech_binding_revisions",
    "speech_publication_retirements",
    "engine_instances",
    "runtime_instance_heads",
    "runtime_instance_intents",
    "resource_pools",
    "host_nodes",
    "admission_leases",
    "admission_attempts",
)


class AdmissionWatchUnavailableError(RuntimeError):
    """The watch process lacks a required deployment value."""


class AdmissionWatchSettings(BaseSettings):
    """The explicit switch, the cadence and the threshold; secrets stay out of repr."""

    model_config = SettingsConfigDict(
        env_file=None,
        extra="ignore",
        populate_by_name=True,
        hide_input_in_errors=True,
        frozen=True,
    )

    enabled: bool = Field(
        default=False,
        validation_alias="SPEECHNEXUS_ADMISSION_WATCH_ENABLED",
    )
    interval_seconds: float = Field(
        default=60.0,
        validation_alias="SPEECHNEXUS_ADMISSION_WATCH_INTERVAL_SECONDS",
        ge=1,
        le=3600,
        allow_inf_nan=False,
    )
    alert_after: int = Field(
        default=3,
        validation_alias="SPEECHNEXUS_ADMISSION_WATCH_ALERT_AFTER",
        ge=1,
        le=60,
    )
    workspaces: str = Field(
        default=DEFAULT_WORKSPACE,
        validation_alias="SPEECHNEXUS_ADMISSION_WATCH_WORKSPACES",
    )
    # The report reads the rows a create reserves against, and the Job writer role
    # cannot see those counters, so the watch needs the admission identity. It is the
    # same key the API already carries for that role rather than a second credential
    # for the same rows.
    database_url: SecretStr | None = Field(
        default=None,
        validation_alias="SPEECHNEXUS_JOB_ADMISSION_DATABASE_URL",
        repr=False,
    )
    database_schema: str | None = Field(
        default=None,
        validation_alias="SPEECHNEXUS_DATABASE_SCHEMA",
        pattern=r"^[a-z][a-z0-9_]{2,62}$",
    )
    transaction_timeout_seconds: float = Field(
        default=3.0,
        validation_alias="SPEECHNEXUS_ADMISSION_WATCH_TRANSACTION_TIMEOUT_SECONDS",
        gt=0,
        le=30,
        allow_inf_nan=False,
    )

    def workspace_ids(self) -> tuple[str, ...]:
        """The configured workspaces, de-duplicated in the order they were written."""
        parts = [part.strip() for part in self.workspaces.split(",")]
        return tuple(dict.fromkeys(identifier(part) for part in parts if part))

    @model_validator(mode="after")
    def enabled_requires_every_operating_value(self) -> AdmissionWatchSettings:
        if not self.enabled:
            return self
        missing = [
            name
            for name, value in (
                ("SPEECHNEXUS_JOB_ADMISSION_DATABASE_URL", self.database_url),
                ("SPEECHNEXUS_DATABASE_SCHEMA", self.database_schema),
            )
            if value is None
        ]
        if missing:
            raise ValueError("The admission watch requires " + ", ".join(sorted(missing)))
        if not self.workspace_ids():
            raise ValueError("The admission watch requires SPEECHNEXUS_ADMISSION_WATCH_WORKSPACES")
        return self


class AdmissionWatchRuntime:
    """Own the read-only pool and the polling loop's lifetime."""

    def __init__(self, settings: AdmissionWatchSettings) -> None:
        if not isinstance(settings, AdmissionWatchSettings) or not settings.enabled:
            raise ValueError("The admission watch is not enabled")
        self.settings = settings
        self._pool: SpeechPool | None = None
        self._watcher: AdmissionWatcher | None = None

    async def open(self) -> AdmissionWatchRuntime:
        if self._pool is not None:
            raise ValueError("The admission watch is already open")
        settings = self.settings
        database_url, schema = settings.database_url, settings.database_schema
        if database_url is None or schema is None:
            raise AdmissionWatchUnavailableError(
                "The admission watch has incomplete deployment settings"
            )
        pool = speech_pool(
            database_url.get_secret_value(),
            schema=schema,
            max_size=1,
            name="speech-admission-watch-" + uuid.uuid4().hex[:8],
        )
        pool.open()
        try:
            with pool.connection(timeout=settings.transaction_timeout_seconds) as connection:
                for table in REQUIRED_TABLES:
                    connection.execute(f"SELECT 1 FROM {table} LIMIT 0")
        except psycopg.Error as error:
            # A database that never ran the migration profile, or an admission role
            # whose grants are narrower than the report's read, must fail here with
            # the repair instead of polling a verdict that is always a storage outage.
            pool.close()
            raise AdmissionWatchUnavailableError(
                "the admission report's relations are not readable by this identity; "
                "apply the migration profile and grant the admission role's surface"
            ) from error
        try:
            # The report only ever reads through the admission slot, and this process
            # owns a single read-only connection budget, so both names carry the pool.
            adapter = PostgresPublicJobs(
                pool,
                admission_pool=pool,
                require_admission=True,
                timeout_seconds=settings.transaction_timeout_seconds,
            )
            self._watcher = AdmissionWatcher(
                PublicJobs(adapter, adapter),
                workspaces=settings.workspace_ids(),
                interval_seconds=settings.interval_seconds,
                alert_after=settings.alert_after,
            )
        except BaseException:
            pool.close()
            raise
        self._pool = pool
        return self

    async def close(self) -> None:
        self._watcher = None
        if self._pool is not None:
            await asyncio.to_thread(self._pool.close)
            self._pool = None

    async def poll_once(self) -> tuple[AdmissionWatchObservation, ...]:
        watcher = self._watcher
        if watcher is None:
            raise AdmissionWatchUnavailableError("The admission watch is not open")
        return await asyncio.to_thread(watcher.poll)

    async def run(self, stop: asyncio.Event) -> None:
        """Poll until ``stop``; one failed read never ends the watch."""
        watcher = self._watcher
        if watcher is None:
            raise AdmissionWatchUnavailableError("The admission watch is not open")
        while not stop.is_set():
            try:
                await self.poll_once()
            except Exception as error:
                # The verdict is the report's job, so an unreadable report has its own
                # cause. Anything reaching here is a defect in this process; it is
                # logged once per pass and retried at the same cadence rather than
                # ending a watch that a fix would recover.
                logger.warning(
                    "admission watch poll failed",
                    extra={"exception_type": type(error).__name__},
                )
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=watcher.interval_seconds)


def build_admission_watch(settings: AdmissionWatchSettings) -> AdmissionWatchRuntime | None:
    """Do nothing at all when disabled; otherwise return an unopened runtime."""
    if not settings.enabled:
        return None
    return AdmissionWatchRuntime(settings)


__all__ = [
    "REQUIRED_TABLES",
    "AdmissionWatchRuntime",
    "AdmissionWatchSettings",
    "AdmissionWatchUnavailableError",
    "build_admission_watch",
]
