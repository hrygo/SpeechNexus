"""Composition root for the accepted-cancellation reconciliation worker.

This process is intentionally separate from the API app: it owns a short-lived
PostgreSQL pool, reads the reconciliation schedule, confirms the gateway's
durable execution fact, and closes a Job only through the public Job state
machine. It is disabled unless explicitly enabled, and opening the runtime is
the only place that creates a pool or a gateway client.

Two ways out of `cancelling` are assembled here, and they are not
interchangeable: a confirmed stop settles `cancelled` and releases the lease,
while an acceptance whose confirmation window elapses is judged --
`UnconfirmedCancellationSettlement` isolates the lease and closes the Job as
`failed`/`cancellation_unconfirmed`. The judgment exists so that an accepted
cancellation always reaches a definite capacity answer; without it the slot
stayed reserved and reported as held by an execution nobody could vouch for.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, cast

from pydantic import AnyHttpUrl, Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from speechnexus_backend.speech.application.job_cancellation import (
    CancellationCapacity,
    JobCancellationService,
)
from speechnexus_backend.speech.application.job_cancellation_reconciliation import (
    CancellationReconciliationWorker,
    JobCancellationReconciler,
    Judgment,
    ReconciliationBatch,
)
from speechnexus_backend.speech.application.public_jobs import (
    IdempotencyConflictError,
    JobOutcomeUnknownError,
    PublicJobs,
)
from speechnexus_backend.speech.domain.job_dispatch import DispatchUnavailableError
from speechnexus_backend.speech.domain.public_jobs import (
    CANCELLATION_UNCONFIRMED,
    JobCancellationReceipt,
    PublicJobConflictError,
    PublicJobNotFoundError,
    PublicJobUnavailableError,
)
from speechnexus_backend.speech.infrastructure.gateway_cancellation_confirmation import (
    CancellationSettlementPort,
    GatewayCancellationConfirmer,
)
from speechnexus_backend.speech.infrastructure.gateway_job_cancellation import (
    GatewayJobCancellationStopper,
)
from speechnexus_backend.speech.infrastructure.job_cancellation_fence_schema import (
    NOT_SUBMITTED_TABLE,
)
from speechnexus_backend.speech.infrastructure.job_cancellation_reconciliation_postgres import (
    PostgresCancellationReconciliationStore,
)
from speechnexus_backend.speech.infrastructure.job_dispatch_postgres import PostgresLeaseSettler
from speechnexus_backend.speech.infrastructure.postgres_pool import SpeechPool, speech_pool
from speechnexus_backend.speech.infrastructure.public_job_submission_schema import SUBMISSIONS
from speechnexus_backend.speech.infrastructure.public_jobs_postgres import (
    PostgresPublicJobs,
)


@dataclass(frozen=True, slots=True)
class _GatewaySettings:
    """Only the fields ``GatewayExecutionClient.from_settings`` reads."""

    execution_gateway_base_url: AnyHttpUrl
    execution_gateway_component_credential: SecretStr
    execution_gateway_timeout_seconds: float
    execution_gateway_max_retries: int = 0
    execution_gateway_backoff_seconds: float = 0.0
    execution_gateway_max_response_bytes: int = 1_048_576


class CancellationRuntimeUnavailableError(RuntimeError):
    """The reconciliation worker lacks a required deployment value."""


class JobCancellationSettings(BaseSettings):
    """Explicit switch and bounded cadence; all secrets stay out of repr."""

    model_config = SettingsConfigDict(
        env_file=None,
        extra="ignore",
        populate_by_name=True,
        hide_input_in_errors=True,
        frozen=True,
    )

    enabled: bool = Field(
        default=False,
        validation_alias="SPEECHNEXUS_JOB_CANCELLATION_RECONCILIATION_ENABLED",
    )
    interval_seconds: float = Field(
        default=15.0,
        validation_alias="SPEECHNEXUS_JOB_CANCELLATION_RECONCILIATION_INTERVAL_SECONDS",
        ge=1,
        le=300,
        allow_inf_nan=False,
    )
    batch_size: int = Field(
        default=16,
        validation_alias="SPEECHNEXUS_JOB_CANCELLATION_RECONCILIATION_BATCH_SIZE",
        ge=1,
        le=128,
    )
    max_attempts: int = Field(
        default=8,
        validation_alias="SPEECHNEXUS_JOB_CANCELLATION_RECONCILIATION_MAX_ATTEMPTS",
        ge=1,
        le=32,
    )
    min_age_seconds: float = Field(
        default=5.0,
        validation_alias="SPEECHNEXUS_JOB_CANCELLATION_RECONCILIATION_MIN_AGE_SECONDS",
        ge=0,
        le=3600,
        allow_inf_nan=False,
    )
    confirm_timeout_seconds: float = Field(
        default=300.0,
        validation_alias="SPEECHNEXUS_JOB_CANCELLATION_RECONCILIATION_CONFIRM_TIMEOUT_SECONDS",
        gt=0,
        le=3600,
        allow_inf_nan=False,
    )
    lease_seconds: float = Field(
        default=30.0,
        validation_alias="SPEECHNEXUS_JOB_CANCELLATION_RECONCILIATION_LEASE_SECONDS",
        gt=0,
        le=600,
        allow_inf_nan=False,
    )
    transaction_timeout_seconds: float = Field(
        default=3.0,
        validation_alias="SPEECHNEXUS_JOB_CANCELLATION_RECONCILIATION_TRANSACTION_TIMEOUT_SECONDS",
        gt=0,
        le=30,
        allow_inf_nan=False,
    )
    database_url: SecretStr | None = Field(
        default=None, validation_alias="SPEECHNEXUS_DATABASE_URL", repr=False
    )
    database_schema: str | None = Field(
        default=None,
        validation_alias="SPEECHNEXUS_DATABASE_SCHEMA",
        pattern=r"^[a-z][a-z0-9_]{2,62}$",
    )
    gateway_base_url: AnyHttpUrl | None = Field(
        default=None,
        validation_alias="SPEECHNEXUS_EXECUTION_GATEWAY_BASE_URL",
        repr=False,
    )
    gateway_credential: SecretStr | None = Field(
        default=None,
        validation_alias="SPEECHNEXUS_EXECUTION_GATEWAY_COMPONENT_CREDENTIAL",
        repr=False,
    )
    gateway_timeout_seconds: float = Field(
        default=10.0,
        validation_alias="SPEECHNEXUS_EXECUTION_GATEWAY_TIMEOUT_SECONDS",
        gt=0,
        le=60,
        allow_inf_nan=False,
    )

    @model_validator(mode="after")
    def enabled_requires_every_operating_value(self) -> JobCancellationSettings:
        if not self.enabled:
            return self
        missing = [
            name
            for name, value in (
                ("database_url", self.database_url),
                ("database_schema", self.database_schema),
                ("gateway_base_url", self.gateway_base_url),
                ("gateway_credential", self.gateway_credential),
            )
            if value is None
        ]
        if missing:
            raise ValueError("Cancellation reconciliation requires " + ", ".join(sorted(missing)))
        if self.interval_seconds > 300:
            raise ValueError("Cancellation reconciliation interval is unbounded")
        return self


class _PublicJobsSettlement(CancellationSettlementPort):
    """Settle the public Job, then return its admission lease.

    The confirmer only reaches this port after the gateway proved no execution
    (404 plus the dispatcher's not-submitted observation, or a durable
    no-dispatch/verified-stop fact), so the existing ``LeaseService`` release is
    the same settlement the dispatcher uses. Release is idempotent: a Job that is
    already cancelled retries it here, which is how a refused or unknown release
    is retried by the next reconciliation pass. A failed release keeps the lease
    held (the durable marker) and returns False so the ledger schedules a retry
    instead of dropping the only chance to return capacity.

    ``capacity`` is the same :class:`CancellationCapacity` the immediate HTTP
    confirmation holds, so both entry points share one release rule rather than a
    copy of it.
    """

    def __init__(self, jobs: PublicJobs, capacity: CancellationCapacity) -> None:
        self._jobs, self._capacity = jobs, capacity

    def settle_cancelled(self, job_id: str) -> bool:
        try:
            job = self._jobs.get(job_id)
        except PublicJobNotFoundError:
            return False
        if job.state == "cancelled":
            return self._release(job_id)
        if job.state != "cancelling":
            return False
        try:
            current = self._jobs.settle_cancelled(job_id, expected_version=job.status_version)
        except IdempotencyConflictError, JobOutcomeUnknownError, PublicJobUnavailableError:
            return False
        return current.state == "cancelled" and self._release(job_id)

    def _release(self, job_id: str) -> bool:
        # Known window: the port is synchronous, so this blocks the event-loop
        # thread for up to the lease transaction budget (3s) and
        # asyncio.timeout cannot interrupt it. Moving the call to a worker
        # thread is the step to take if cancellation reconciliation ever runs
        # concurrently. The shared capacity object already folds a refused or
        # failed release into False, so the ledger retries instead of masking it.
        return self._capacity.release(job_id)


class UnconfirmedCancellationSettlement:
    """Judge an accepted cancellation that never produced a verified stop.

    Order matters and so does the gate. Every lease of the Job must first move
    into the quarantined bucket; only then is the Job closed as ``failed`` with
    ``error_code=cancellation_unconfirmed``. That is the documented terminal for
    an unconfirmed cancel (see ``docs/architecture/runtime-governance.md``), it
    makes ``cleanup_state`` report ``quarantined``, and it is what stops the
    public Job/cancel/retry surfaces from claiming progress that is never going
    to arrive (#852).

    A lease whose execution never became ``unknown_quarantined``/``quarantined``
    -- still executing, still stopping, or already succeeded -- is refused by the
    domain. That refusal is *not* overridden: the Job keeps ``cancelling``, the
    fence stays claimable, and this method reports the gap. Writing the terminal
    anyway would report ``cleanup_state=quarantined`` for capacity that is still
    held, which is exactly the lie the review rejected, and it would leave the
    lease with no supported way out.

    The same "the capacity answer must be real" rule governs a Job that is
    already ``cancelled`` (#1033). Its stop was proven -- that is how it reached
    ``cancelled`` -- so the only outstanding fact is the admission lease, and
    the release path owns it. Reporting such a Job settled without asking the
    release again turned a persistently refused release into a ledger record
    that said "judged" while the slot stayed reserved: ``claim_due`` excludes an
    exhausted row, so nothing retried the release and the capacity never came
    back. This method now retries the idempotent release itself, which is the
    only retry reachable for a submitted execution whose gateway fact keeps the
    confirmation port away from its settlement step. A refused release keeps the
    fence claimable on the reconciler's bounded backoff; a storage failure stays
    retryable and is never recorded as settled.
    """

    def __init__(
        self,
        jobs: PublicJobs,
        capacity: PostgresLeaseSettler,
        *,
        reason_code: str = CANCELLATION_UNCONFIRMED,
    ) -> None:
        self._jobs, self._capacity, self._reason_code = jobs, capacity, reason_code

    def settle(self, job_id: str) -> Judgment:
        try:
            job = self._jobs.get(job_id)
        except PublicJobNotFoundError, PublicJobUnavailableError:
            return Judgment(settled=False, outcome="unavailable", detail="job unreadable")
        if job.state == "cancelled":
            # The stop is already proven; the release is the outstanding fact.
            # Isolating the lease here would race the release path, so the only
            # honest test of "settled" is asking that same idempotent release.
            return self._capacity_judgment(job.job_id)
        if job.state not in ("cancelling", "failed"):
            return Judgment(
                settled=False,
                outcome="stop_unconfirmed",
                detail=f"job state {job.state} is not an accepted cancellation",
            )
        isolation = self._capacity.isolate_sync(job.job_id, reason_code=self._reason_code)
        if isolation is None:
            return Judgment(
                settled=False, outcome="unavailable", detail="lease storage unavailable"
            )
        if not isolation.complete:
            # Capacity that is still held must never be described as quarantined,
            # so the Job keeps `cancelling` (or its other terminal reason) and the
            # fence stays claimable until every lease has landed.
            return Judgment(
                settled=False,
                outcome=(
                    "unavailable"
                    if any(gap.reason == "lease_storage_unavailable" for gap in isolation.gaps)
                    else "stop_unconfirmed"
                ),
                detail=isolation.detail(),
            )
        if job.state == "failed":
            # Another owner's terminal already closes the acceptance; only the
            # isolation was outstanding and it is now done.
            return Judgment(settled=True, outcome="exhausted", detail=f"already {job.error_code}")
        try:
            current = self._jobs.advance(job.job_id, "failed", error_code=self._reason_code)
        except JobOutcomeUnknownError, PublicJobConflictError, PublicJobUnavailableError:
            return Judgment(settled=False, outcome="unavailable", detail="job advance unavailable")
        if current.state == "failed" and current.error_code == self._reason_code:
            return Judgment(settled=True, outcome="exhausted")
        return Judgment(
            settled=False,
            outcome="unavailable",
            detail=f"job settled as {current.state}/{current.error_code}",
        )

    def _capacity_judgment(self, job_id: str) -> Judgment:
        """Judge an already-cancelled Job by its capacity, not by its state.

        ``release_sync`` is the same idempotent ``LeaseService`` release the
        confirmation port performs, so replaying it after the Job committed
        ``cancelled`` is safe even when both owners run. ``True`` means every
        admission lease of the Job returned; ``False`` means the domain refused
        or could not prove the release, and the answer must stay retryable.

        The refused branch reports ``stop_unconfirmed`` rather than inventing a
        ledger value: the stored outcome vocabulary is bounded and the Job row
        already says ``cancelled``. What matters for the operator is that the
        ledger stays claimable -- an exhausted row is never claimed again -- and
        that the batch counts the pass as blocked instead of settled.
        """
        try:
            released = self._capacity.release_sync(job_id)
        except DispatchUnavailableError:
            return Judgment(
                settled=False, outcome="unavailable", detail="lease storage unavailable"
            )
        if released:
            return Judgment(settled=True, outcome="exhausted", detail="already cancelled")
        return Judgment(
            settled=False,
            outcome="stop_unconfirmed",
            detail="job cancelled; a refused release still holds its capacity",
        )


class _PublicJobsStop:
    """One stop through the existing acceptance service, never a new execution."""

    def __init__(
        self,
        jobs: PublicJobs,
        receipt: Callable[[str], JobCancellationReceipt | None],
        service: JobCancellationService,
    ) -> None:
        self._jobs, self._receipt, self._service = jobs, receipt, service

    async def __call__(self, job_id: str, document: Mapping[str, Any]) -> bool:
        del document  # the service re-reads the authoritative execution identity
        try:
            job = self._jobs.get(job_id)
        except PublicJobNotFoundError:
            return False
        if job.state == "cancelled":
            return True
        if job.state != "cancelling":
            return False
        receipt = self._receipt(job_id)
        if receipt is None:
            return False
        current = await self._service.confirm(job, receipt)
        return current.state == "cancelled"


class JobCancellationRuntime:
    """Own the pool, gateway adapter and periodic worker lifetime."""

    def __init__(self, settings: JobCancellationSettings) -> None:
        if not isinstance(settings, JobCancellationSettings) or not settings.enabled:
            raise ValueError("Cancellation reconciliation is not enabled")
        self.settings = settings
        self._pool: SpeechPool | None = None
        self._reconciler: JobCancellationReconciler | None = None
        self._worker: CancellationReconciliationWorker | None = None

    async def open(self) -> JobCancellationRuntime:
        if self._pool is not None:
            raise ValueError("Cancellation reconciliation is already open")
        settings = self.settings
        database_url, schema = settings.database_url, settings.database_schema
        gateway_url, credential = settings.gateway_base_url, settings.gateway_credential
        if database_url is None or schema is None or gateway_url is None or credential is None:
            raise CancellationRuntimeUnavailableError(
                "Cancellation reconciliation has incomplete deployment settings"
            )
        name = "speech-cancellation-" + uuid.uuid4().hex[:8]
        pool = speech_pool(
            database_url.get_secret_value(),
            schema=schema,
            max_size=2,
            name=name,
        )
        pool.open()
        try:
            with pool.connection(timeout=settings.transaction_timeout_seconds) as connection:
                for table in (
                    "speech_jobs",
                    SUBMISSIONS,
                    "speech_job_cancellations",
                    "speech_job_cancellation_fences",
                    "speech_job_cancellation_reconciliations",
                    NOT_SUBMITTED_TABLE,
                ):
                    # 零行探针:验证表可读即可,方言没有 `LIMIT`,这里也没有分页语义。
                    connection.execute(f"SELECT 1 FROM {table} WHERE 1=0")
            adapter = PostgresPublicJobs(pool, timeout_seconds=settings.transaction_timeout_seconds)
            jobs = PublicJobs(adapter, adapter)
            store = PostgresCancellationReconciliationStore(
                pool, timeout_seconds=settings.transaction_timeout_seconds
            )
            gateway = cast(
                Any,
                _GatewaySettings(
                    execution_gateway_base_url=gateway_url,
                    execution_gateway_component_credential=credential,
                    execution_gateway_timeout_seconds=settings.gateway_timeout_seconds,
                ),
            )
            capacity = PostgresLeaseSettler(
                pool, timeout_seconds=settings.transaction_timeout_seconds
            )
            release = CancellationCapacity(capacity.release_sync)
            service = JobCancellationService(jobs, GatewayJobCancellationStopper(gateway), release)
            confirmer = GatewayCancellationConfirmer(
                gateway,
                _PublicJobsSettlement(jobs, release),
                stop_attempt=_PublicJobsStop(jobs, adapter.cancellation_receipt, service),
            )
            self._reconciler = JobCancellationReconciler(
                store=store,
                confirmer=confirmer,
                unconfirmed=UnconfirmedCancellationSettlement(jobs, capacity),
                max_attempts=settings.max_attempts,
                batch_size=settings.batch_size,
                lease_seconds=settings.lease_seconds,
                min_age_seconds=settings.min_age_seconds,
                confirm_timeout_seconds=settings.confirm_timeout_seconds,
            )
            self._worker = CancellationReconciliationWorker(
                self._reconciler, interval_seconds=settings.interval_seconds
            )
        except BaseException:
            pool.close()
            raise
        self._pool = pool
        return self

    async def close(self) -> None:
        self._worker = None
        self._reconciler = None
        if self._pool is not None:
            await asyncio.to_thread(self._pool.close)
            self._pool = None

    async def run_once(self) -> ReconciliationBatch:
        if self._reconciler is None:
            raise CancellationRuntimeUnavailableError("Cancellation runtime is not open")
        return await self._reconciler.run_once(now_us=int(time.time() * 1_000_000))

    async def run(self, stop: asyncio.Event) -> None:
        if self._worker is None:
            raise CancellationRuntimeUnavailableError("Cancellation runtime is not open")
        await self._worker.run(stop)


def build_cancellation_runtime(
    settings: JobCancellationSettings,
) -> JobCancellationRuntime | None:
    """Do nothing when disabled; otherwise return an unopened runtime."""
    if not settings.enabled:
        return None
    return JobCancellationRuntime(settings)


__all__ = [
    "CancellationRuntimeUnavailableError",
    "JobCancellationRuntime",
    "JobCancellationSettings",
    "UnconfirmedCancellationSettlement",
    "build_cancellation_runtime",
]
