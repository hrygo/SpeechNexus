"""Observe the Job admission readiness verdict and log only what changed.

``GET /v1/nexus/jobs/admission-readiness`` answers one instant, and it answers it
only when somebody asks (#845, #863). A deployment whose readiness receipt elapsed,
whose pinned instance lost its pool or whose workspace quota is full therefore
refuses every create with a ``503`` while ``/readyz``, the container healthcheck and
``docker ps`` all stay green: nothing was asking. This module is the asking, not a
second opinion -- it calls the same service object the route calls
(``PublicJobs.admission_readiness``), which in turn calls the predicate the create
path calls.

Two rules bound what this module writes:

* **only a change is a log line.** One line when the watched workspace moves between
  ``admitting`` and ``degraded``, or between two causes, and nothing while the
  verdict repeats. A poller that printed every sample would bury the transition that
  matters in a line every interval (#1041);
* **only a deployment-level refusal is worth waking somebody for.** A refused create
  caused by the workspace's own administrative state -- no catalog row, disabled,
  no quota row -- is answered before admission is consulted and is that workspace's
  problem. It still produces a change line, but it never raises the alert.

The alert is a latch, not a per-poll message: it fires once when the consecutive
counter crosses its threshold, stays quiet while the refusal persists, and clears
when the streak ends. The threshold and the cadence belong to the deployment, so
they are constructor arguments here and settings in ``admission_watch_runtime``.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from speechnexus_backend.speech.domain.job_admission_readiness import (
    AdmissionReadiness,
    AdmissionReadinessCause,
    AdmissionReadinessStatus,
    storage_unavailable_readiness,
)
from speechnexus_backend.speech.domain.public_jobs import PublicJobUnavailableError, identifier

logger = logging.getLogger(__name__)

EVENT_STARTED = "admission_watch_started"
EVENT_CHANGED = "admission_watch_changed"
EVENT_ALERTED = "admission_watch_alerted"
EVENT_CLEARED = "admission_watch_cleared"

WORKSPACE_SCOPED_CAUSES: frozenset[AdmissionReadinessCause] = frozenset(
    {
        AdmissionReadinessCause.WORKSPACE_NOT_FOUND,
        AdmissionReadinessCause.WORKSPACE_DISABLED,
        AdmissionReadinessCause.WORKSPACE_NOT_CONFIGURED,
    }
)
"""Refusals that follow one workspace instead of the deployment.

All three are decided before admission is consulted (``_readiness_workspace_state``
/ ``_quota``), and each names an administrative state of that workspace: a missing
catalog row, a workspace that is not ``active``, a missing quota row. Paging the
deployment for them would train whoever is on call to ignore this signal, so they
are classified here rather than inferred from a prefix at the call site.
"""

DEPLOYMENT_CAUSES: frozenset[AdmissionReadinessCause] = frozenset(
    set(AdmissionReadinessCause) - WORKSPACE_SCOPED_CAUSES
)
"""Every refusal the deployment itself answers: ``engine_*``, ``capacity_exhausted``,
``storage_unavailable``.

Derived as the complement so a cause added to the report vocabulary alerts until
somebody classifies it here; the safe direction for a waking signal is to be loud by
default, and ``tests/unit/backend/test_admission_watch.py`` pins both halves of the
partition against the enum.
"""


class AdmissionReadinessService(Protocol):
    """The one read the watch needs; ``PublicJobs`` implements it."""

    def admission_readiness(self, workspace_id: str) -> AdmissionReadiness: ...


@dataclass(frozen=True, slots=True)
class AdmissionWatchObservation:
    """What one poll of one workspace produced."""

    workspace_id: str
    report: AdmissionReadiness
    changed: bool
    consecutive_degraded: int
    alerted: bool


@dataclass(slots=True)
class _WatchedWorkspace:
    """The verdict last reported for one workspace plus its refusal streak."""

    status: AdmissionReadinessStatus
    cause: AdmissionReadinessCause | None
    consecutive_degraded: int = 0
    first_degraded_at_us: int | None = None
    alerted: bool = False


def _verdict(
    status: AdmissionReadinessStatus, cause: AdmissionReadinessCause | None
) -> dict[str, str | None]:
    """The ``from``/``to`` shape: one status and its cause, never one without the other."""
    return {"status": status.value, "cause": None if cause is None else cause.value}


def _deployment_refusal(report: AdmissionReadiness) -> bool:
    return report.status is AdmissionReadinessStatus.DEGRADED and report.cause in DEPLOYMENT_CAUSES


def _next_state(
    previous: _WatchedWorkspace | None, report: AdmissionReadiness, *, alert_after: int
) -> _WatchedWorkspace:
    """Advance one workspace's streak and alert latch from one verdict."""
    if not _deployment_refusal(report):
        # Either a create would be admitted, or the refusal is the workspace's own
        # administrative state: neither is a deployment-level streak, so the
        # counter starts over and an outstanding alert is considered cleared.
        return _WatchedWorkspace(report.status, report.cause)
    streak = (0 if previous is None else previous.consecutive_degraded) + 1
    first = None
    if previous is not None:
        first = previous.first_degraded_at_us
    return _WatchedWorkspace(
        report.status,
        report.cause,
        consecutive_degraded=streak,
        first_degraded_at_us=report.now_us if first is None else first,
        alerted=(previous is not None and previous.alerted) or streak >= alert_after,
    )


class AdmissionWatcher:
    """Poll one readiness service per configured workspace and log the transitions.

    The class is synchronous because the service it calls is: the report runs one
    read-only transaction on a blocking driver. ``admission_watch_runtime`` calls
    :meth:`poll` in a worker thread so an awaitable shutdown still interrupts the
    sleep between polls, not a query in flight.
    """

    def __init__(
        self,
        service: AdmissionReadinessService,
        *,
        workspaces: Sequence[str],
        interval_seconds: float,
        alert_after: int,
    ) -> None:
        ordered = tuple(dict.fromkeys(identifier(workspace_id) for workspace_id in workspaces))
        if not ordered:
            raise ValueError("Admission watch requires at least one workspace")
        if type(interval_seconds) not in (int, float) or not 0 < interval_seconds <= 3600:
            raise ValueError("Admission watch requires a bounded interval")
        if type(alert_after) is not int or not 1 <= alert_after <= 60:
            raise ValueError("Admission watch requires a bounded alert threshold")
        self._service = service
        self._workspaces = ordered
        self._interval_seconds = float(interval_seconds)
        self._alert_after = alert_after
        self._states: dict[str, _WatchedWorkspace] = {}

    @property
    def workspaces(self) -> tuple[str, ...]:
        return self._workspaces

    @property
    def interval_seconds(self) -> float:
        return self._interval_seconds

    @property
    def alert_after(self) -> int:
        return self._alert_after

    def poll(self) -> tuple[AdmissionWatchObservation, ...]:
        """Read every configured workspace once, in configuration order."""
        return tuple(self._poll(workspace_id) for workspace_id in self._workspaces)

    def _poll(self, workspace_id: str) -> AdmissionWatchObservation:
        report = self._read(workspace_id)
        previous = self._states.get(workspace_id)
        state = _next_state(previous, report, alert_after=self._alert_after)
        self._states[workspace_id] = state
        changed = previous is not None and (previous.status, previous.cause) != (
            state.status,
            state.cause,
        )
        if previous is None:
            self._log_started(workspace_id, report)
        elif changed:
            self._log_changed(workspace_id, report, previous)
        alerted = state.alerted and not (previous is not None and previous.alerted)
        cleared = not state.alerted and previous is not None and previous.alerted
        if alerted:
            # One line per crossing, not one per poll: the refusal is already being
            # reported by the interval and the streak length is not news.
            logger.warning(
                "admission readiness refuses creates for this deployment",
                extra={
                    "event": EVENT_ALERTED,
                    "workspace_id": workspace_id,
                    "cause": None if state.cause is None else state.cause.value,
                    "consecutive_degraded": state.consecutive_degraded,
                    "alert_after": self._alert_after,
                    "interval_seconds": self._interval_seconds,
                    "since_us": state.first_degraded_at_us,
                    "detail": report.detail,
                },
            )
        elif cleared:
            logger.info(
                "admission readiness alert cleared",
                extra={
                    "event": EVENT_CLEARED,
                    "workspace_id": workspace_id,
                    "status": state.status.value,
                    "cause": None if state.cause is None else state.cause.value,
                    "alert_after": self._alert_after,
                },
            )
        return AdmissionWatchObservation(
            workspace_id, report, changed, state.consecutive_degraded, state.alerted
        )

    def _read(self, workspace_id: str) -> AdmissionReadiness:
        """One verdict, or the declared storage-outage cause when the read fails.

        ``PublicJobs.admission_readiness`` raises ``PublicJobUnavailableError`` when
        the store cannot answer, and a poller that stayed silent through that would
        reproduce the defect this module exists to remove: a deployment whose store
        is unreachable also refuses every create. The synthetic verdict is the
        domain's own ``storage_unavailable``, not a third state invented here -- and
        it deliberately shares the vocabulary, the change line and the alert streak
        with a store that answers that cause itself.
        """
        try:
            return self._service.admission_readiness(workspace_id)
        except PublicJobUnavailableError:
            return storage_unavailable_readiness(
                workspace_id=workspace_id, now_us=time.time_ns() // 1_000
            )

    def _log_started(self, workspace_id: str, report: AdmissionReadiness) -> None:
        logger.info(
            "admission watch started",
            extra={
                "event": EVENT_STARTED,
                "workspace_id": workspace_id,
                "status": report.status.value,
                "cause": None if report.cause is None else report.cause.value,
                "interval_seconds": self._interval_seconds,
                "alert_after": self._alert_after,
            },
        )

    def _log_changed(
        self, workspace_id: str, report: AdmissionReadiness, previous: _WatchedWorkspace
    ) -> None:
        logger.info(
            "admission readiness changed",
            extra={
                "event": EVENT_CHANGED,
                "workspace_id": workspace_id,
                "from": _verdict(previous.status, previous.cause),
                "to": _verdict(report.status, report.cause),
                "detail": report.detail,
            },
        )


__all__ = [
    "DEPLOYMENT_CAUSES",
    "EVENT_ALERTED",
    "EVENT_CHANGED",
    "EVENT_CLEARED",
    "EVENT_STARTED",
    "WORKSPACE_SCOPED_CAUSES",
    "AdmissionReadinessService",
    "AdmissionWatchObservation",
    "AdmissionWatcher",
]
