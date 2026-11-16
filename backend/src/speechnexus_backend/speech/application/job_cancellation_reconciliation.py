"""Bounded reconciliation for accepted public Job cancellations.

The durable fence is the authoritative acceptance fact. This worker claims due
fences with a lease and a persisted attempt count, asks the cancellation
confirmer whether the Job can now close, and records a bounded backoff. It
never creates an execution, never rewrites a completed Job and never treats a
missing execution or an unverified failure as a stop.

An accepted cancellation is also bounded in time. Once the confirmation window
has passed (or the attempt budget is spent) the worker stops asking the gateway
for a stop it could not prove and *judges* the Job instead: it closes the Job as
a ``failed`` outcome with ``error_code=cancellation_unconfirmed``, which is the
documented terminal state for an unconfirmed cancel. Leaving the Job in
``cancelling`` while the ledger merely recorded ``exhausted`` was the #852
defect: nothing else claims an exhausted row, so the admission lease of that Job
kept its instance slot reserved forever and every public read still reported an
in-progress cancellation.

A restarted worker resumes from the persisted rows: an in-flight claim becomes
due again after its lease, and a settled row is never claimed again.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal, Protocol

logger = logging.getLogger(__name__)

# ``exhausted`` is the store's "never claim again" marker: the acceptance was
# judged. ``terminal_unconfirmed`` stays accepted because earlier builds wrote it
# to the ledger; nothing produces it now.
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


@dataclass(frozen=True, slots=True)
class Judgment:
    """What one attempt to close an unconfirmable acceptance achieved.

    ``settled`` is true only when the Job is durably terminal for this reason.
    A blocked judgment is not a failure to retry blindly: it names the leases
    that still hold capacity, so the caller keeps the fence claimable and the
    operator surfaces can report the same facts (#852 review P1).
    """

    settled: bool
    outcome: ReconciliationOutcome
    detail: str = ""

    def __post_init__(self) -> None:
        if self.settled and self.outcome != "exhausted":
            raise ValueError("A settled judgment records the terminal outcome")
        if not self.settled and self.outcome == "exhausted":
            raise ValueError("An unsettled judgment cannot record exhaustion")


class UnconfirmedCancellation(Protocol):
    """Judge an acceptance whose stop this worker could not confirm.

    Implementations must be idempotent, must never rewrite a Job that another
    owner already terminated, and must report ``settled`` only when every lease
    of the Job has reached a determinate state -- capacity that is still held
    must never be described as quarantined.
    """

    def settle(self, job_id: str) -> Judgment: ...


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
class BlockedJudgment:
    """An acceptance that is past its window but still holds capacity.

    Carried out of ``run_once`` so the caller can log one line per Job naming
    the leases that block closure; the same facts stay readable from storage, so
    a restart does not lose them.
    """

    job_id: str
    detail: str


@dataclass(frozen=True, slots=True)
class ReconciliationBatch:
    claimed: int = 0
    settled: int = 0
    retried: int = 0
    exhausted: int = 0
    unavailable: int = 0
    blocked: tuple[BlockedJudgment, ...] = ()


class JobCancellationReconciler:
    """One bounded pass; composition owns the periodic timer."""

    def __init__(
        self,
        *,
        store: CancellationReconciliationStore,
        confirmer: CancellationConfirmer,
        unconfirmed: UnconfirmedCancellation,
        max_attempts: int = 5,
        batch_size: int = 16,
        lease_seconds: float = 30,
        min_age_seconds: float = 5,
        base_backoff_seconds: float = 1,
        max_backoff_seconds: float = 300,
        budget_seconds: float = 10,
        confirm_timeout_seconds: float = 300,
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
            ("confirmation window", confirm_timeout_seconds, 3_600),
        ):
            if type(value) not in (int, float) or not 0 < value <= ceiling:
                raise ValueError(f"Cancellation reconciliation requires a bounded {name}")
        if base_backoff_seconds > max_backoff_seconds:
            raise ValueError("Cancellation backoff must grow within its ceiling")
        self._store, self._confirmer, self._unconfirmed = store, confirmer, unconfirmed
        self._max_attempts, self._batch_size = max_attempts, batch_size
        self._lease_us = int(lease_seconds * 1_000_000)
        self._min_age_us = int(min_age_seconds * 1_000_000)
        self._base_backoff_us = int(base_backoff_seconds * 1_000_000)
        self._max_backoff_us = int(max_backoff_seconds * 1_000_000)
        self._budget_seconds = float(budget_seconds)
        self._confirm_timeout_us = int(confirm_timeout_seconds * 1_000_000)

    def _backoff_us(self, attempts: int) -> int:
        exponent = min(attempts - 1, 16)
        return min(self._base_backoff_us * (1 << exponent), self._max_backoff_us)

    def _judgeable(self, candidate: CancellationCandidate, now_us: int) -> bool:
        """True once the worker must judge the acceptance instead of retrying.

        Two independent bounds: the configured confirmation window from the
        durable acceptance time, and the attempt budget the ledger enforces.
        Past either one a further gateway question cannot change the answer --
        no bounded retry proves a stop that has not been proven -- so asking
        again would only postpone the terminal state.
        """
        return (
            candidate.attempts > self._max_attempts
            or now_us >= candidate.accepted_at_us + self._confirm_timeout_us
        )

    async def _judge(self, candidate: CancellationCandidate) -> Judgment:
        """Close an unconfirmable acceptance; never drop it on a storage error."""
        try:
            return await asyncio.to_thread(self._unconfirmed.settle, candidate.job_id)
        except Exception:
            return Judgment(settled=False, outcome="unavailable")

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
        blocked: list[BlockedJudgment] = []
        try:
            async with asyncio.timeout(self._budget_seconds):
                for candidate in candidates:
                    outcome = await self._reconcile(candidate, now_us=now_us)
                    if isinstance(outcome, Judgment):
                        if outcome.settled:
                            exhausted += 1
                            continue
                        blocked.append(BlockedJudgment(candidate.job_id, outcome.detail))
                        retried += 1
                        if outcome.outcome == "unavailable":
                            unavailable += 1
                        continue
                    if outcome == "settled":
                        settled += 1
                    else:
                        retried += 1
                        if outcome == "unavailable":
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
            blocked=tuple(blocked),
        )

    async def _reconcile(
        self, candidate: CancellationCandidate, *, now_us: int
    ) -> Judgment | ReconciliationOutcome:
        """Advance one acceptance: judge it when its window is over, else ask.

        A judgment ends the row only when it settled. A blocked judgment falls
        through to the confirmer on purpose: the confirmer is what sends the
        bounded stop attempt, and capacity held by an execution that may still be
        running must keep being asked to stop rather than being described as
        returned (#852 review P1).
        """
        judgment: Judgment | None = None
        if self._judgeable(candidate, now_us):
            judgment = await self._judge(candidate)
            if judgment.settled:
                await self._record_settled_judgment(candidate, now_us=now_us)
                return judgment
        try:
            confirmation = await self._confirmer.confirm(
                candidate.job_id,
                not_submitted=candidate.not_submitted_at_us is not None,
            )
        except Exception:
            confirmation = Confirmation("unavailable")
        if confirmation.outcome == "settled":
            await asyncio.to_thread(self._store.record_settled, candidate.job_id, now_us=now_us)
            return "settled"
        if confirmation.outcome == "terminal_unconfirmed" and judgment is None:
            # The execution finished while the Job was cancelling: no stop will
            # ever be confirmed, so ask for the terminal judgment now instead of
            # waiting out a window that cannot change the answer.
            judgment = await self._judge(candidate)
            if judgment.settled:
                await self._record_settled_judgment(candidate, now_us=now_us)
                return judgment
            await self._record_blocked_judgment(candidate, judgment, now_us=now_us)
            return judgment
        if judgment is not None:
            await self._record_blocked_judgment(candidate, judgment, now_us=now_us)
            return judgment
        await asyncio.to_thread(
            self._store.record_retry,
            candidate.job_id,
            now_us=now_us,
            outcome=confirmation.outcome,
            next_check_at_us=now_us
            + max(self._backoff_us(candidate.attempts), confirmation.retry_after_us or 0),
        )
        return confirmation.outcome

    async def _record_settled_judgment(
        self, candidate: CancellationCandidate, *, now_us: int
    ) -> None:
        """``exhausted`` is the ledger's only "never claim again" marker.

        After this change it means "the acceptance was judged", not "this worker
        gave up"; the reason itself lives on the Job row, and the isolation on
        the leases, where operators read it.
        """
        await asyncio.to_thread(
            self._store.record_exhausted,
            candidate.job_id,
            now_us=now_us,
            outcome="exhausted",
        )

    async def _record_blocked_judgment(
        self, candidate: CancellationCandidate, judgment: Judgment, *, now_us: int
    ) -> None:
        """Keep the fence claimable while capacity that is still held blocks it."""
        await asyncio.to_thread(
            self._store.record_retry,
            candidate.job_id,
            now_us=now_us,
            outcome=judgment.outcome,
            next_check_at_us=now_us + self._backoff_us(candidate.attempts),
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
                batch = await self._reconciler.run_once(now_us=int(time.time() * 1_000_000))
                for blocked in batch.blocked:
                    logger.warning(
                        "cancellation acceptance holds capacity but cannot be isolated",
                        extra={"job_id": blocked.job_id, "blocking": blocked.detail},
                    )
            except Exception as error:
                if self._on_error is None:
                    raise
                self._on_error(error)
            try:
                await asyncio.wait_for(stop.wait(), timeout=self._interval)
            except TimeoutError:
                continue


__all__ = [
    "BlockedJudgment",
    "CancellationCandidate",
    "CancellationConfirmer",
    "CancellationReconciliationStore",
    "CancellationReconciliationWorker",
    "Confirmation",
    "JobCancellationReconciler",
    "ReconciliationBatch",
    "ReconciliationOutcome",
    "UnconfirmedCancellation",
]
