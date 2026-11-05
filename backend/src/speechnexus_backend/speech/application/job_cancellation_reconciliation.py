"""Bounded reconciliation for accepted public Job cancellations.

The durable fence is the authoritative acceptance fact. This worker claims due
fences with a lease and a persisted attempt count, asks the cancellation
confirmer whether the Job can now close, and records a bounded backoff. It
never creates an execution, never rewrites a completed Job and never treats a
missing execution or an unverified failure as a stop.

A restarted worker resumes from the persisted rows: an in-flight claim becomes
due again after its lease, and a settled row is never claimed again.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal, Protocol

ReconciliationOutcome = Literal[
    "settled",
    "pending",
    "stop_unconfirmed",
    "terminal_unconfirmed",
    "unavailable",
    "exhausted",
]


@dataclass(frozen=True, slots=True)
class CancellationCandidate:
    job_id: str
    accepted_at_us: int
    attempts: int
    next_check_at_us: int
    not_submitted_at_us: int | None = None


@dataclass(frozen=True, slots=True)
class Confirmation:
    """One bounded confirmation result; only settled closes the Job."""

    outcome: Literal[
        "settled", "pending", "stop_unconfirmed", "terminal_unconfirmed", "unavailable"
    ]
    retry_after_us: int | None = None

    def __post_init__(self) -> None:
        if self.outcome not in (
            "settled",
            "pending",
            "stop_unconfirmed",
            "terminal_unconfirmed",
            "unavailable",
        ):
            raise ValueError("Invalid cancellation confirmation outcome")
        if self.retry_after_us is not None and (
            type(self.retry_after_us) is not int or not 0 <= self.retry_after_us <= 3_600_000_000
        ):
            raise ValueError("Invalid cancellation retry delay")


class CancellationConfirmer(Protocol):
    async def confirm(self, job_id: str, *, not_submitted: bool = False) -> Confirmation: ...


class CancellationReconciliationStore(Protocol):
    def claim_due(
        self, *, now_us: int, limit: int, lease_until_us: int, accepted_before_us: int
    ) -> list[CancellationCandidate]: ...

    def record_settled(self, job_id: str, *, now_us: int) -> None: ...

    def record_retry(
        self,
        job_id: str,
        *,
        now_us: int,
        outcome: ReconciliationOutcome,
        next_check_at_us: int,
    ) -> None: ...

    def record_exhausted(
        self, job_id: str, *, now_us: int, outcome: ReconciliationOutcome
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class ReconciliationBatch:
    claimed: int = 0
    settled: int = 0
    retried: int = 0
    exhausted: int = 0
    unavailable: int = 0


class JobCancellationReconciler:
    """One bounded pass; composition owns the periodic timer."""

    def __init__(
        self,
        *,
        store: CancellationReconciliationStore,
        confirmer: CancellationConfirmer,
        max_attempts: int = 5,
        batch_size: int = 16,
        lease_seconds: float = 30,
        min_age_seconds: float = 5,
        base_backoff_seconds: float = 1,
        max_backoff_seconds: float = 300,
        budget_seconds: float = 10,
    ) -> None:
        if not 1 <= max_attempts <= 32:
            raise ValueError("Cancellation reconciliation requires bounded attempts")
        if not 1 <= batch_size <= 128:
            raise ValueError("Cancellation reconciliation requires a bounded batch")
        if type(min_age_seconds) not in (int, float) or not 0 <= min_age_seconds <= 3_600:
            raise ValueError("Cancellation reconciliation requires a bounded minimum age")
        for name, value, ceiling in (
            ("lease", lease_seconds, 600),
            ("base backoff", base_backoff_seconds, 300),
            ("max backoff", max_backoff_seconds, 3_600),
            ("budget", budget_seconds, 60),
        ):
            if type(value) not in (int, float) or not 0 < value <= ceiling:
                raise ValueError(f"Cancellation reconciliation requires a bounded {name}")
        if base_backoff_seconds > max_backoff_seconds:
            raise ValueError("Cancellation backoff must grow within its ceiling")
        self._store, self._confirmer = store, confirmer
        self._max_attempts, self._batch_size = max_attempts, batch_size
        self._lease_us = int(lease_seconds * 1_000_000)
        self._min_age_us = int(min_age_seconds * 1_000_000)
        self._base_backoff_us = int(base_backoff_seconds * 1_000_000)
        self._max_backoff_us = int(max_backoff_seconds * 1_000_000)
        self._budget_seconds = float(budget_seconds)

    def _backoff_us(self, attempts: int) -> int:
        exponent = min(attempts - 1, 16)
        return min(self._base_backoff_us * (1 << exponent), self._max_backoff_us)

    async def run_once(self, *, now_us: int) -> ReconciliationBatch:
        now_us = int(now_us)
        candidates = await asyncio.to_thread(
            self._store.claim_due,
            now_us=now_us,
            limit=self._batch_size,
            lease_until_us=now_us + self._lease_us,
            accepted_before_us=max(0, now_us - self._min_age_us),
        )
        settled = retried = exhausted = unavailable = 0
        try:
            async with asyncio.timeout(self._budget_seconds):
                for candidate in candidates:
                    if candidate.attempts > self._max_attempts:
                        await asyncio.to_thread(
                            self._store.record_exhausted,
                            candidate.job_id,
                            now_us=now_us,
                            outcome="exhausted",
                        )
                        exhausted += 1
                        continue
                    try:
                        confirmation = await self._confirmer.confirm(
                            candidate.job_id,
                            not_submitted=candidate.not_submitted_at_us is not None,
                        )
                    except Exception:
                        confirmation = Confirmation("unavailable")
                    if confirmation.outcome == "settled":
                        await asyncio.to_thread(
                            self._store.record_settled, candidate.job_id, now_us=now_us
                        )
                        settled += 1
                    elif confirmation.outcome == "terminal_unconfirmed":
                        await asyncio.to_thread(
                            self._store.record_exhausted,
                            candidate.job_id,
                            now_us=now_us,
                            outcome="terminal_unconfirmed",
                        )
                        exhausted += 1
                    else:
                        delay = max(
                            self._backoff_us(candidate.attempts),
                            confirmation.retry_after_us or 0,
                        )
                        await asyncio.to_thread(
                            self._store.record_retry,
                            candidate.job_id,
                            now_us=now_us,
                            outcome=confirmation.outcome,
                            next_check_at_us=now_us + delay,
                        )
                        retried += 1
                        if confirmation.outcome == "unavailable":
                            unavailable += 1
        except TimeoutError:
            # Claimed rows stay leased until next_check_at_us; a later pass resumes.
            pass
        return ReconciliationBatch(
            claimed=len(candidates),
            settled=settled,
            retried=retried,
            exhausted=exhausted,
            unavailable=unavailable,
        )


class CancellationReconciliationWorker:
    """Periodic composition shell around one bounded reconciler pass."""

    def __init__(
        self,
        reconciler: JobCancellationReconciler,
        *,
        interval_seconds: float = 5,
        on_error: Callable[[BaseException], None] | None = None,
    ) -> None:
        if type(interval_seconds) not in (int, float) or not 0 < interval_seconds <= 60:
            raise ValueError("Cancellation worker requires a bounded interval")
        self._reconciler, self._interval, self._on_error = (
            reconciler,
            float(interval_seconds),
            on_error,
        )

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                await self._reconciler.run_once(now_us=int(time.time() * 1_000_000))
            except Exception as error:
                if self._on_error is None:
                    raise
                self._on_error(error)
            try:
                await asyncio.wait_for(stop.wait(), timeout=self._interval)
            except TimeoutError:
                continue


__all__ = [
    "CancellationCandidate",
    "CancellationConfirmer",
    "CancellationReconciliationStore",
    "CancellationReconciliationWorker",
    "Confirmation",
    "JobCancellationReconciler",
    "ReconciliationBatch",
    "ReconciliationOutcome",
]
