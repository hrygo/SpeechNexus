"""Composition switch and validation for the cancellation reconciliation worker."""

from __future__ import annotations

from typing import Any, cast

import pytest
from pydantic import ValidationError

from speechnexus_backend.job_cancellation_runtime import (
    JobCancellationRuntime,
    JobCancellationSettings,
    UnconfirmedCancellationSettlement,
    _PublicJobsSettlement,
    _PublicJobsStop,
    build_cancellation_runtime,
)
from speechnexus_backend.speech.application.job_cancellation import (
    CancellationCapacity,
    JobCancellationService,
    StopObservation,
)
from speechnexus_backend.speech.application.leases import LeaseIsolation, LeaseIsolationGap
from speechnexus_backend.speech.domain.job_dispatch import DispatchUnavailableError


def _settings(**changes: Any) -> JobCancellationSettings:
    values: dict[str, Any] = {
        "enabled": False,
        "database_url": "postgresql://job:synthetic-password@127.0.0.1:5432/speech",
        "database_schema": "speech_test",
        "gateway_base_url": "http://127.0.0.1:8010",
        "gateway_credential": "synthetic-token-0001",
    }
    return JobCancellationSettings(**dict(values, **changes))


def test_disabled_runtime_does_not_create_a_pool(monkeypatch: pytest.MonkeyPatch) -> None:
    import speechnexus_backend.job_cancellation_runtime as runtime_module

    def fail(*args: object, **kwargs: object) -> object:
        raise AssertionError("disabled reconciliation must not create a pool")

    monkeypatch.setattr(runtime_module, "speech_pool", fail)
    assert build_cancellation_runtime(_settings(enabled=False)) is None


def test_disabled_runtime_cannot_be_constructed_directly() -> None:
    with pytest.raises(ValueError):
        JobCancellationRuntime(_settings(enabled=False))


def test_enabled_runtime_requires_every_operating_value() -> None:
    with pytest.raises(ValidationError):
        _settings(enabled=True, database_url=None)
    with pytest.raises(ValidationError):
        _settings(enabled=True, gateway_credential=None)
    with pytest.raises(ValidationError):
        _settings(enabled=True, interval_seconds=0.1)


def test_enabled_runtime_is_unopened_until_open() -> None:
    runtime = build_cancellation_runtime(_settings(enabled=True))
    assert runtime is not None
    assert runtime._pool is None
    assert runtime._worker is None


class _Job:
    def __init__(self, state: str, version: int = 3) -> None:
        self.state, self.status_version = state, version


class _Jobs:
    def __init__(self, states: list[_Job]) -> None:
        self.states = states
        self.settled: list[str] = []

    def get(self, job_id: str) -> _Job:
        return self.states.pop(0)

    def settle_cancelled(self, job_id: str, *, expected_version: int) -> _Job:
        self.settled.append(job_id)
        return _Job("cancelled", expected_version + 1)


class _Service:
    def __init__(self) -> None:
        self.calls = 0

    async def confirm(self, job: Any, receipt: Any) -> _Job:
        self.calls += 1
        return _Job("cancelled")


class _Capacity:
    def __init__(self, *, released: bool = True, fail: bool = False) -> None:
        self.released, self.fail = released, fail
        self.calls: list[str] = []

    def __call__(self, job_id: str) -> bool:
        self.calls.append(job_id)
        if self.fail:
            raise RuntimeError("lease storage unavailable")
        return self.released


def test_settlement_uses_public_job_version_guard_and_returns_capacity() -> None:
    jobs, capacity = _Jobs([_Job("cancelling")]), _Capacity()
    assert (
        _PublicJobsSettlement(cast(Any, jobs), CancellationCapacity(capacity)).settle_cancelled(
            "job-1"
        )
        is True
    )
    assert jobs.settled == ["job-1"]
    assert capacity.calls == ["job-1"]
    running = _Jobs([_Job("running")])
    assert (
        _PublicJobsSettlement(
            cast(Any, running), CancellationCapacity(_Capacity())
        ).settle_cancelled("job-1")
        is False
    )


def test_an_already_cancelled_job_still_retries_the_release() -> None:
    jobs, capacity = _Jobs([_Job("cancelled", 5)]), _Capacity()

    assert (
        _PublicJobsSettlement(cast(Any, jobs), CancellationCapacity(capacity)).settle_cancelled(
            "job-1"
        )
        is True
    )
    assert jobs.settled == []
    assert capacity.calls == ["job-1"]


def test_a_refused_release_keeps_the_job_scheduled_for_retry() -> None:
    jobs = _Jobs([_Job("cancelling")])
    capacity = _Capacity(released=False)

    assert (
        _PublicJobsSettlement(cast(Any, jobs), CancellationCapacity(capacity)).settle_cancelled(
            "job-1"
        )
        is False
    )
    assert capacity.calls == ["job-1"]


def test_an_unavailable_release_is_not_mistaken_for_settlement() -> None:
    jobs = _Jobs([_Job("cancelling")])
    capacity = _Capacity(fail=True)

    assert (
        _PublicJobsSettlement(cast(Any, jobs), CancellationCapacity(capacity)).settle_cancelled(
            "job-1"
        )
        is False
    )


class _ConfirmJob:
    def __init__(self, state: str = "cancelling") -> None:
        self.job_id = "job-1"
        self.state, self.status_version = state, 3


class _Receipt:
    job_id = "job-1"


class _Stopper:
    def __init__(self, state: str) -> None:
        self.state, self.calls = state, 0

    async def stop(self, job: Any, receipt: Any) -> StopObservation:
        self.calls += 1
        return StopObservation(cast(Any, self.state))


async def test_a_verified_stop_releases_capacity_after_settling() -> None:
    jobs, capacity, stopper = _Jobs([]), _Capacity(), _Stopper("stopped")
    service = JobCancellationService(cast(Any, jobs), stopper, CancellationCapacity(capacity))

    settled = await service.confirm(cast(Any, _ConfirmJob()), cast(Any, _Receipt()))

    assert settled.state == "cancelled"
    assert jobs.settled == ["job-1"]
    assert capacity.calls == ["job-1"]
    assert stopper.calls == 1


async def test_a_refused_release_keeps_the_cancelled_state() -> None:
    jobs, capacity = _Jobs([]), _Capacity(fail=True)
    service = JobCancellationService(
        cast(Any, jobs), _Stopper("stopped"), CancellationCapacity(capacity)
    )

    settled = await service.confirm(cast(Any, _ConfirmJob()), cast(Any, _Receipt()))

    assert settled.state == "cancelled"
    assert capacity.calls == ["job-1"]


async def test_an_unconfirmed_stop_neither_settles_nor_releases() -> None:
    jobs, capacity = _Jobs([]), _Capacity()
    service = JobCancellationService(
        cast(Any, jobs), _Stopper("unknown"), CancellationCapacity(capacity)
    )

    confirmed = await service.confirm(cast(Any, _ConfirmJob()), cast(Any, _Receipt()))

    assert confirmed.state == "cancelling"
    assert jobs.settled == []
    assert capacity.calls == []


async def test_stop_adapter_reuses_job_cancellation_service() -> None:
    jobs = _Jobs([_Job("cancelling")])
    service = _Service()
    stop = _PublicJobsStop(cast(Any, jobs), lambda job_id: cast(Any, object()), cast(Any, service))
    assert await stop("job-1", {}) is True
    assert service.calls == 1


def test_settings_repr_hides_connection_and_credential() -> None:
    settings = _settings(enabled=True)
    text = repr(settings)
    assert "synthetic-password" not in text
    assert "synthetic-token" not in text


class _SettleableJob:
    def __init__(self, state: str, error_code: str | None = None, version: int = 3) -> None:
        self.state, self.error_code, self.status_version = state, error_code, version
        self.job_id = "job-1"


class _SettleableJobs:
    def __init__(self, current: _SettleableJob, *, conflict: bool = False) -> None:
        self.current = current
        self.conflict = conflict
        self.advances: list[tuple[str, str | None]] = []
        self.reads = 0

    def get(self, job_id: str) -> _SettleableJob:
        self.reads += 1
        return self.current

    def advance(self, job_id: str, target: str, *, error_code: str | None = None) -> _SettleableJob:
        if self.conflict:
            from speechnexus_backend.speech.domain.public_jobs import PublicJobConflictError

            raise PublicJobConflictError("version moved")
        self.advances.append((target, error_code))
        self.current = _SettleableJob(target, error_code, self.current.status_version + 1)
        return self.current


class _Isolation:
    """Records every isolation/release attempt and returns a scripted answer."""

    def __init__(
        self, *answers: LeaseIsolation | None, released: bool = True, release_error: bool = False
    ) -> None:
        self.answers = list(answers)
        self.calls: list[tuple[str, str]] = []
        self.releases: list[str] = []
        self.released, self.release_error = released, release_error

    def isolate_sync(self, operation_id: str, *, reason_code: str) -> LeaseIsolation | None:
        self.calls.append((operation_id, reason_code))
        return self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]

    def release_sync(self, operation_id: str) -> bool:
        self.releases.append(operation_id)
        if self.release_error:
            raise DispatchUnavailableError("lease settlement storage is unavailable")
        return self.released


def _isolated(*lease_ids: str) -> LeaseIsolation:
    return LeaseIsolation(operation_id="job-1", quarantined=lease_ids, gaps=())


def _gap(
    *,
    reason: str = "execution_cleanup_pending",
    lease_state: str = "reserved",
    execution_state: str | None = "executing",
    lease_id: str = "lease-1",
) -> LeaseIsolation:
    return LeaseIsolation(
        operation_id="job-1",
        quarantined=(),
        gaps=(
            LeaseIsolationGap(
                lease_id=lease_id,
                lease_state=lease_state,
                cleanup_state="pending",
                execution_state=execution_state,
                execution_cleanup_state="pending",
                reserved_slots=1,
                reason=reason,
            ),
        ),
    )


def test_unconfirmed_settlement_quarantines_every_lease_before_failing_the_job() -> None:
    order: list[str] = []
    jobs = _SettleableJobs(_SettleableJob("cancelling"))
    isolation = _Isolation(_isolated("lease-1", "lease-2"))
    settlement = UnconfirmedCancellationSettlement(
        cast(Any, _OrderedJobs(jobs, order)), cast(Any, _OrderedCapacity(isolation, order))
    )

    judgment = settlement.settle("job-1")

    assert judgment.settled is True and judgment.outcome == "exhausted"
    # Isolation first: capacity must stop looking held before the Job closes.
    assert order == ["isolate", "advance"]
    assert (jobs.current.state, jobs.current.error_code) == ("failed", "cancellation_unconfirmed")


class _OrderedJobs:
    """Public Job port that records the advance after the isolation attempt."""

    def __init__(self, inner: _SettleableJobs, order: list[str]) -> None:
        self._inner, self._order = inner, order

    def get(self, job_id: str) -> _SettleableJob:
        return self._inner.get(job_id)

    def advance(self, job_id: str, target: str, *, error_code: str | None = None) -> _SettleableJob:
        self._order.append("advance")
        return self._inner.advance(job_id, target, error_code=error_code)


class _OrderedCapacity:
    def __init__(self, isolation: _Isolation, order: list[str]) -> None:
        self._isolation, self._order = isolation, order

    def isolate_sync(self, operation_id: str, *, reason_code: str) -> LeaseIsolation | None:
        self._order.append("isolate")
        return self._isolation.isolate_sync(operation_id, reason_code=reason_code)


def test_a_lease_that_cannot_be_isolated_blocks_the_terminal_judgment() -> None:
    """#852 review P1: no `failed/cancellation_unconfirmed` while capacity is held.

    Writing the terminal anyway made `cleanup_state` report `quarantined` for a
    lease that was still `reserved`, and left that lease with no supported exit.
    """
    jobs = _SettleableJobs(_SettleableJob("cancelling"))
    settlement = UnconfirmedCancellationSettlement(cast(Any, jobs), cast(Any, _Isolation(_gap())))

    judgment = settlement.settle("job-1")

    assert judgment.settled is False
    assert judgment.outcome == "stop_unconfirmed"
    assert "lease-1" in judgment.detail and "executing" in judgment.detail
    assert jobs.advances == []
    assert jobs.current.state == "cancelling"


def test_one_isolatable_lease_does_not_excuse_the_others() -> None:
    jobs = _SettleableJobs(_SettleableJob("cancelling"))
    settlement = UnconfirmedCancellationSettlement(
        cast(Any, jobs), cast(Any, _Isolation(_gap(lease_id="lease-2")))
    )

    judgment = settlement.settle("job-1")

    assert judgment.settled is False
    assert jobs.advances == []


def test_unavailable_isolation_storage_is_retryable_not_a_judgment() -> None:
    jobs = _SettleableJobs(_SettleableJob("cancelling"))
    settlement = UnconfirmedCancellationSettlement(cast(Any, jobs), cast(Any, _Isolation(None)))
    assert settlement.settle("job-1").outcome == "unavailable"

    storage_gap = UnconfirmedCancellationSettlement(
        cast(Any, jobs), cast(Any, _Isolation(_gap(reason="lease_storage_unavailable")))
    )
    judgment = storage_gap.settle("job-1")
    assert judgment.settled is False and judgment.outcome == "unavailable"
    assert jobs.advances == []


def test_a_judged_job_only_retries_its_outstanding_isolation() -> None:
    jobs = _SettleableJobs(_SettleableJob("failed", "cancellation_unconfirmed"))
    isolation = _Isolation(_gap(), _isolated("lease-1"))
    settlement = UnconfirmedCancellationSettlement(cast(Any, jobs), cast(Any, isolation))

    assert settlement.settle("job-1").settled is False
    assert jobs.advances == []
    assert settlement.settle("job-1").settled is True
    # The committed judgment is never rewritten, only its isolation finished.
    assert jobs.advances == []


def test_a_cancelled_job_is_judged_by_the_release_not_by_its_state() -> None:
    """#1033: settled requires the capacity to be back, never the state alone."""
    jobs = _SettleableJobs(_SettleableJob("cancelled"))
    capacity = _Isolation(_isolated("lease-1"))
    settlement = UnconfirmedCancellationSettlement(cast(Any, jobs), cast(Any, capacity))

    judgment = settlement.settle("job-1")

    assert (judgment.settled, judgment.outcome) == (True, "exhausted")
    assert jobs.advances == []
    assert capacity.releases == ["job-1"]
    # The cancelled branch never isolates: that would race the release path.
    assert capacity.calls == []


def test_a_refused_release_of_a_cancelled_job_is_not_settled() -> None:
    """The exhausted branch must stay claimable while the lease still holds.

    Recording ``exhausted`` here is what made the capacity permanent: the claim
    predicate excludes that value, so the release would never be retried again.
    """
    jobs = _SettleableJobs(_SettleableJob("cancelled"))
    capacity = _Isolation(_isolated("lease-1"), released=False)
    settlement = UnconfirmedCancellationSettlement(cast(Any, jobs), cast(Any, capacity))

    judgment = settlement.settle("job-1")

    assert (judgment.settled, judgment.outcome) == (False, "stop_unconfirmed")
    assert "refused release" in judgment.detail
    assert capacity.releases == ["job-1"]
    assert jobs.advances == []


def test_an_unreadable_lease_storage_keeps_the_judgment_retryable() -> None:
    jobs = _SettleableJobs(_SettleableJob("cancelled"))
    capacity = _Isolation(_isolated("lease-1"), release_error=True)
    settlement = UnconfirmedCancellationSettlement(cast(Any, jobs), cast(Any, capacity))

    judgment = settlement.settle("job-1")

    assert (judgment.settled, judgment.outcome) == (False, "unavailable")
    assert jobs.advances == []


def test_another_terminal_failure_is_not_overwritten() -> None:
    jobs = _SettleableJobs(_SettleableJob("failed", "internal_error"))
    capacity = _Isolation(_isolated("lease-1"))
    settlement = UnconfirmedCancellationSettlement(cast(Any, jobs), cast(Any, capacity))

    judgment = settlement.settle("job-1")

    assert judgment.settled is True  # Another owner closed the acceptance.
    assert jobs.advances == []
    # Capacity of that Job is still booked as quarantined rather than left
    # reported as held by an execution nobody is judging.
    assert capacity.calls == [("job-1", "cancellation_unconfirmed")]


def test_a_job_that_is_not_cancelling_cannot_be_judged() -> None:
    for state in ("succeeded", "running", "pending"):
        jobs = _SettleableJobs(_SettleableJob(state))
        settlement = UnconfirmedCancellationSettlement(
            cast(Any, jobs), cast(Any, _Isolation(_isolated("lease-1")))
        )
        judgment = settlement.settle("job-1")
        assert judgment.settled is False, state
        assert judgment.outcome == "stop_unconfirmed", state
        assert jobs.advances == [], state


def test_an_unreadable_job_is_retried_rather_than_judged() -> None:
    from speechnexus_backend.speech.domain.public_jobs import PublicJobUnavailableError

    class _Missing:
        def get(self, job_id: str) -> Any:
            raise PublicJobUnavailableError

    settlement = UnconfirmedCancellationSettlement(
        cast(Any, _Missing()), cast(Any, _Isolation(_isolated("lease-1")))
    )

    assert settlement.settle("job-1").outcome == "unavailable"


def test_a_lost_version_guard_is_reported_as_unsettled() -> None:
    jobs = _SettleableJobs(_SettleableJob("cancelling"), conflict=True)
    settlement = UnconfirmedCancellationSettlement(
        cast(Any, jobs), cast(Any, _Isolation(_isolated("lease-1")))
    )

    assert settlement.settle("job-1").settled is False


def test_confirmation_window_is_bounded() -> None:
    for value in (0, 3601):
        with pytest.raises(ValidationError):
            _settings(confirm_timeout_seconds=value)
