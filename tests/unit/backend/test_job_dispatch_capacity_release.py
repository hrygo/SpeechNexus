"""Capacity release: one named warning per failure mode, never a rewritten outcome.

``JobDispatcher._release_capacity`` is the only place a dispatch settles the
admission lease. It has three ways not to return capacity -- the lease service
answers that the lease stays held, the lease service raises, or the Job was
never this dispatcher's to release -- and none of them may change the terminal
result that is already committed when they run.

The two warnings are all an operator gets, so these tests pin the message and
the ``job_id`` field per branch instead of accepting "some warning happened":
swapping the two messages, widening ``released is not True`` or narrowing
``except Exception`` has to fail here.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from typing import cast

import pytest

from speechnexus_backend.speech.application import job_dispatcher
from speechnexus_backend.speech.application.job_dispatcher import JobDispatcher, LeaseSettlement
from speechnexus_backend.speech.domain.job_dispatch import (
    ClaimedJob,
    DispatchBatch,
    DispatchJob,
    DispatchLimits,
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
from tests.unit.backend.test_job_dispatch_settlement import FUTURE, DeadlinePlanner

LOGGER = job_dispatcher.__name__
DEFERRED = "lease release deferred to reconciliation"
RAISED = "lease release raised"


class _Releaser:
    """A lease service whose single answer the test picks.

    ``released=False`` is the stop-and-cleanup-unproven answer the branch exists
    for; ``cast(bool, None)`` is the same branch seen by a caller that received
    no answer at all.
    """

    def __init__(self, *, released: bool = True, error: BaseException | None = None) -> None:
        self.calls: list[str] = []
        self._released = released
        self._error = error

    async def release(self, operation_id: str) -> bool:
        self.calls.append(operation_id)
        if self._error is not None:
            raise self._error
        return self._released


class _LeaseStoreOutageError(Exception):
    """A store failure neither the builtin hierarchy nor a ValueError handler names."""


def _voice_generation_job() -> DispatchJob:
    """A ``voice_design`` Job: dispatched like tts, but not this lane's lease to return."""
    document = json.dumps({"kind": "voice_design"})
    digest = "sha256:" + hashlib.sha256(document.encode("utf-8")).hexdigest()
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


def _dispatcher(job: DispatchJob, leases: LeaseSettlement | None) -> tuple[JobDispatcher, States]:
    states = States(job)
    dispatcher = JobDispatcher(
        Queue([[ClaimedJob(job, "worker-a", 1, NOW + 60_000_000)]]),
        states,
        DeadlinePlanner(FUTURE),
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


@pytest.mark.parametrize(
    "released",
    [False, cast(bool, None)],
    ids=["service_reports_held", "service_answers_nothing"],
)
async def test_deferred_release_warns_once_and_keeps_the_committed_outcome(
    released: bool, caplog: pytest.LogCaptureFixture
) -> None:
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
    job = _job()
    dispatcher, states = _dispatcher(job, None)
    batch = await dispatcher.run_once("worker-a")

    assert _release_warnings(caplog) == []
    assert batch == DispatchBatch(claimed=1, succeeded=1)
    assert states.jobs[job.job_id].state == "succeeded"


async def test_generation_kind_never_returns_capacity_and_stays_quiet(
    caplog: pytest.LogCaptureFixture,
) -> None:
    job = _voice_generation_job()
    leases = _Releaser()
    dispatcher, states = _dispatcher(job, leases)
    batch = await dispatcher.run_once("worker-a")

    assert leases.calls == []
    assert _release_warnings(caplog) == []
    assert batch == DispatchBatch(claimed=1, succeeded=1)
    assert states.jobs[job.job_id].state == "succeeded"


async def test_successful_release_is_silent(caplog: pytest.LogCaptureFixture) -> None:
    job = _job()
    leases = _Releaser()
    dispatcher, states = _dispatcher(job, leases)
    batch = await dispatcher.run_once("worker-a")

    assert leases.calls == [job.job_id]
    assert _release_warnings(caplog) == []
    assert batch == DispatchBatch(claimed=1, succeeded=1)
    assert states.jobs[job.job_id].state == "succeeded"


async def test_no_release_answer_changes_what_the_caller_gets(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Released, held, unanswerable and raised all hand the caller the same result."""
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
