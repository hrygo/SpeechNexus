"""Capacity release: one named warning per failure mode, never a rewritten outcome.

``JobDispatcher._release_capacity`` is the only place a dispatch settles the
admission lease. It has three ways not to return capacity -- the lease service
answers that the lease stays held, the lease service raises, or the Job was
never this dispatcher's to release -- and none of them may change the result
that is already committed when they run.

The two warnings are all an operator gets, so these tests pin the message and
the ``job_id`` field per branch instead of accepting "some warning happened":
swapping the two messages, widening ``released is not True``, narrowing
``except Exception`` or moving the release ahead of the terminal write has to
fail here.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from collections.abc import Callable
from dataclasses import replace
from typing import Any, cast

import pytest

from speechnexus_backend.speech.application import job_dispatcher
from speechnexus_backend.speech.application.job_dispatcher import JobDispatcher, LeaseSettlement
from speechnexus_backend.speech.domain.job_dispatch import (
    ClaimedJob,
    DispatchBatch,
    DispatchJob,
    DispatchLimits,
    ExecutionSubmission,
)
from tests.unit.backend.test_job_dispatch import (
    NOW,
    GatewayState,
    Publisher,
    Queue,
    States,
    _gateway,
    _job,
)
from tests.unit.backend.test_job_dispatch_settlement import FUTURE, DeadlinePlanner, _asr_job

LOGGER = job_dispatcher.__name__
DEFERRED = "lease release deferred to reconciliation"
RAISED = "lease release raised"


class _Releaser:
    """A lease service whose single answer the test picks.

    ``released=False`` is the stop-and-cleanup-unproven answer the branch exists
    for. ``cast(bool, None)`` is a non-``bool`` answer no current implementation
    produces: ``is not True`` is the defensive spelling that keeps such an answer
    on the held side instead of reading it as released.
    """

    def __init__(self, *, released: bool = True, error: BaseException | None = None) -> None:
        self.calls: list[str] = []
        #: The Job state as the store exposed it at each release. Recorded rather
        #: than asserted inside ``release``: the dispatcher swallows exceptions
        #: here by design, so an assertion would be logged instead of raised.
        self.state_at_release: list[str] = []
        self._released = released
        self._error = error
        self._states: States | None = None

    def watch(self, states: States) -> None:
        """Record the Job state visible to the lease service when it is called."""
        self._states = states

    async def release(self, operation_id: str) -> bool:
        self.calls.append(operation_id)
        if self._states is not None:
            self.state_at_release.append(self._states.jobs[operation_id].state)
        if self._error is not None:
            raise self._error
        return self._released


class _LeaseStoreOutageError(Exception):
    """A store failure neither the builtin hierarchy nor a ValueError handler names."""


class _ClaimStealingPlanner(DeadlinePlanner):
    """Another claimant bumps the status version while this dispatch is in flight.

    The queue hands out one claim, then the terminal guard refuses it: that is
    the claim-lost path, which must never touch the lease service.
    """

    def __init__(self) -> None:
        super().__init__(FUTURE)
        self.states: States | None = None

    async def plan(self, job: DispatchJob, submit_id: str) -> ExecutionSubmission:
        submission = await super().plan(job, submit_id)
        assert self.states is not None
        current = self.states.jobs[job.job_id]
        self.states.jobs[job.job_id] = replace(current, status_version=current.status_version + 1)
        return submission


def _document(kind: str, payload: dict[str, Any]) -> tuple[str, str]:
    document = json.dumps({"kind": kind, **payload})
    return document, "sha256:" + hashlib.sha256(document.encode("utf-8")).hexdigest()


def _voice_generation_job() -> DispatchJob:
    """A ``voice_design`` Job: dispatched like tts, but not this lane's lease to return."""
    document, digest = _document("voice_design", {})
    return DispatchJob(
        "job-synthetic-voice",
        "workspace-default",
        "voice_design",
        "pending",
        1,
        NOW,
        digest,
        document,
    )


def _voice_validation_job() -> DispatchJob:
    """A ``voice_validation`` Job: the third kind whose terminal returns capacity."""
    document, digest = _document(
        "voice_validation",
        {"input": {"voice_draft_id": "draft-synthetic-0001", "draft_revision": "2"}},
    )
    return DispatchJob(
        "job-synthetic-validation",
        "workspace-default",
        "voice_validation",
        "pending",
        1,
        NOW,
        digest,
        document,
    )


def _dispatcher(
    job: DispatchJob, leases: LeaseSettlement | None, *, planner: Any = None
) -> tuple[JobDispatcher, States]:
    states = States(job)
    dispatcher = JobDispatcher(
        Queue([[ClaimedJob(job, "worker-a", 1, NOW + 60_000_000)]]),
        states,
        DeadlinePlanner(FUTURE) if planner is None else planner,
        _gateway(GatewayState().handler),
        Publisher(),
        limits=DispatchLimits(poll_seconds=0.01, job_timeout_seconds=10),
        now_us=lambda: NOW,
        sleep=lambda _: asyncio.sleep(0),
    )
    if leases is not None:
        dispatcher.settle_capacity_through(leases)
    return dispatcher, states


def _release_warnings(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    """Only this module's records: the release warnings are this dispatcher's contract."""
    return [record for record in caplog.records if record.name == LOGGER]


def _quiet(caplog: pytest.LogCaptureFixture) -> None:
    """Pin the logger at WARNING so a raised level cannot make "no records" vacuous."""
    caplog.set_level(logging.WARNING, logger=LOGGER)


@pytest.mark.parametrize(
    "released",
    [False, cast(bool, None)],
    ids=["service_reports_held", "service_answers_nothing"],
)
async def test_deferred_release_warns_once_and_keeps_the_committed_outcome(
    released: bool, caplog: pytest.LogCaptureFixture
) -> None:
    _quiet(caplog)
    job = _job()
    leases = _Releaser(released=released)
    dispatcher, states = _dispatcher(job, leases)
    batch = await dispatcher.run_once("worker-a")

    assert leases.calls == [job.job_id]
    records = _release_warnings(caplog)
    assert [record.getMessage() for record in records] == [DEFERRED]
    assert records[0].levelno == logging.WARNING
    assert getattr(records[0], "job_id", None) == job.job_id
    # The deferred answer is not a store failure: the other warning must stay absent.
    assert RAISED not in caplog.text
    # The terminal result was committed before release; a held lease never revises it.
    assert batch == DispatchBatch(claimed=1, succeeded=1)
    assert states.jobs[job.job_id].state == "succeeded"


@pytest.mark.parametrize(
    "error",
    [RuntimeError("lease store is down"), _LeaseStoreOutageError("lease store is gone")],
    ids=["runtime_error", "custom_exception"],
)
async def test_release_that_raises_warns_the_raised_variant_and_swallows_it(
    error: BaseException, caplog: pytest.LogCaptureFixture
) -> None:
    _quiet(caplog)
    job = _job()
    leases = _Releaser(error=error)
    dispatcher, states = _dispatcher(job, leases)
    # ``except Exception`` is deliberate: a release failure must not fail the
    # dispatch whose public outcome is already committed.
    batch = await dispatcher.run_once("worker-a")

    assert leases.calls == [job.job_id]
    records = _release_warnings(caplog)
    assert [record.getMessage() for record in records] == [RAISED]
    assert records[0].levelno == logging.WARNING
    assert getattr(records[0], "job_id", None) == job.job_id
    # A raised release is not a deferred one: the two messages are distinct signals.
    assert DEFERRED not in caplog.text
    assert batch == DispatchBatch(claimed=1, succeeded=1)
    assert states.jobs[job.job_id].state == "succeeded"


async def test_missing_lease_service_never_releases_and_stays_quiet(
    caplog: pytest.LogCaptureFixture,
) -> None:
    _quiet(caplog)
    job = _job()
    dispatcher, states = _dispatcher(job, None)
    batch = await dispatcher.run_once("worker-a")

    assert _release_warnings(caplog) == []
    assert batch == DispatchBatch(claimed=1, succeeded=1)
    assert states.jobs[job.job_id].state == "succeeded"


async def test_generation_kind_never_returns_capacity_and_stays_quiet(
    caplog: pytest.LogCaptureFixture,
) -> None:
    _quiet(caplog)
    job = _voice_generation_job()
    leases = _Releaser()
    dispatcher, states = _dispatcher(job, leases)
    batch = await dispatcher.run_once("worker-a")

    assert leases.calls == []
    assert _release_warnings(caplog) == []
    assert batch == DispatchBatch(claimed=1, succeeded=1)
    assert states.jobs[job.job_id].state == "succeeded"


@pytest.mark.parametrize(
    "job_factory",
    [_job, _asr_job, _voice_validation_job],
    ids=["tts", "asr", "voice_validation"],
)
async def test_every_releasable_kind_returns_capacity_and_stays_quiet(
    job_factory: Callable[[], DispatchJob], caplog: pytest.LogCaptureFixture
) -> None:
    _quiet(caplog)
    job = job_factory()
    leases = _Releaser()
    dispatcher, states = _dispatcher(job, leases)
    batch = await dispatcher.run_once("worker-a")

    assert leases.calls == [job.job_id]
    assert _release_warnings(caplog) == []
    assert batch == DispatchBatch(claimed=1, succeeded=1)
    assert states.jobs[job.job_id].state == "succeeded"


async def test_release_runs_only_after_the_terminal_state_is_committed(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The durable terminal write precedes the release; a held lease cannot undo it."""
    _quiet(caplog)
    job = _job()
    leases = _Releaser()
    dispatcher, states = _dispatcher(job, leases)
    leases.watch(states)
    batch = await dispatcher.run_once("worker-a")

    # The lease service was called after the terminal write: a release that ran
    # first would observe "running" here.
    assert leases.state_at_release == ["succeeded"]
    assert leases.calls == [job.job_id]
    assert batch == DispatchBatch(claimed=1, succeeded=1)
    assert states.jobs[job.job_id].state == "succeeded"


async def test_lost_claim_never_releases_and_stays_quiet(
    caplog: pytest.LogCaptureFixture,
) -> None:
    _quiet(caplog)
    job = _job()
    leases = _Releaser()
    planner = _ClaimStealingPlanner()
    dispatcher, states = _dispatcher(job, leases, planner=planner)
    planner.states = states
    batch = await dispatcher.run_once("worker-a")

    # The terminal guard refused this claimant, so no outcome was committed here
    # and this worker has no lease to return.
    assert leases.calls == []
    assert _release_warnings(caplog) == []
    assert batch == DispatchBatch(claimed=1, lease_lost=1)
    assert states.jobs[job.job_id].state == "running"


async def test_no_release_answer_changes_what_the_caller_gets(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Released, held, unanswerable and raised all hand the caller the same result."""
    _quiet(caplog)
    outcomes: list[DispatchBatch] = []
    for leases in (
        _Releaser(),
        _Releaser(released=False),
        _Releaser(released=cast(bool, None)),
        _Releaser(error=RuntimeError("lease store is down")),
    ):
        job = _job()
        dispatcher, states = _dispatcher(job, leases)
        outcomes.append(await dispatcher.run_once("worker-a"))
        assert leases.calls == [job.job_id]
        assert states.jobs[job.job_id].state == "succeeded"

    assert outcomes == [DispatchBatch(claimed=1, succeeded=1)] * 4
