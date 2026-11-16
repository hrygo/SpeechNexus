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
    assert _PublicJobsSettlement(cast(Any, jobs), capacity).settle_cancelled("job-1") is True
    assert jobs.settled == ["job-1"]
    assert capacity.calls == ["job-1"]
    running = _Jobs([_Job("running")])
    assert _PublicJobsSettlement(cast(Any, running), _Capacity()).settle_cancelled("job-1") is False


def test_an_already_cancelled_job_still_retries_the_release() -> None:
    jobs, capacity = _Jobs([_Job("cancelled", 5)]), _Capacity()

    assert _PublicJobsSettlement(cast(Any, jobs), capacity).settle_cancelled("job-1") is True
    assert jobs.settled == []
    assert capacity.calls == ["job-1"]


def test_a_refused_release_keeps_the_job_scheduled_for_retry() -> None:
    jobs = _Jobs([_Job("cancelling")])
    capacity = _Capacity(released=False)

    assert _PublicJobsSettlement(cast(Any, jobs), capacity).settle_cancelled("job-1") is False
    assert capacity.calls == ["job-1"]


def test_an_unavailable_release_is_not_mistaken_for_settlement() -> None:
    jobs = _Jobs([_Job("cancelling")])
    capacity = _Capacity(fail=True)

    assert _PublicJobsSettlement(cast(Any, jobs), capacity).settle_cancelled("job-1") is False


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


class _QuarantiningCapacity:
    def __init__(self, *, quarantined: bool = True, fail: bool = False) -> None:
        self.quarantined, self.fail = quarantined, fail
        self.calls: list[tuple[str, str]] = []

    def quarantine_sync(self, operation_id: str, *, reason_code: str) -> bool:
        self.calls.append((operation_id, reason_code))
        if self.fail:
            raise RuntimeError("lease storage unavailable")
        return self.quarantined


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
    def __init__(self, order: list[str]) -> None:
        self._order = order

    def quarantine_sync(self, operation_id: str, *, reason_code: str) -> bool:
        self._order.append("quarantine")
        return True


def test_unconfirmed_settlement_quarantines_the_lease_before_failing_the_job() -> None:
    order: list[str] = []
    jobs = _SettleableJobs(_SettleableJob("cancelling"))
    settlement = UnconfirmedCancellationSettlement(
        cast(Any, _OrderedJobs(jobs, order)), cast(Any, _OrderedCapacity(order))
    )

    assert settlement.settle("job-1") is True
    # Isolation first: the slot must stop looking held before the Job is closed.
    assert order == ["quarantine", "advance"]
    assert (jobs.current.state, jobs.current.error_code) == ("failed", "cancellation_unconfirmed")


def test_unconfirmed_settlement_judges_the_job_even_when_isolation_is_refused() -> None:
    jobs = _SettleableJobs(_SettleableJob("cancelling"))
    settlement = UnconfirmedCancellationSettlement(
        cast(Any, jobs), cast(Any, _QuarantiningCapacity(quarantined=False))
    )

    assert settlement.settle("job-1") is True
    assert jobs.advances == [("failed", "cancellation_unconfirmed")]


def test_unconfirmed_settlement_tolerates_unavailable_lease_storage() -> None:
    jobs = _SettleableJobs(_SettleableJob("cancelling"))
    settlement = UnconfirmedCancellationSettlement(
        cast(Any, jobs), cast(Any, _QuarantiningCapacity(fail=True))
    )

    assert settlement.settle("job-1") is True
    assert jobs.advances == [("failed", "cancellation_unconfirmed")]


def test_unconfirmed_settlement_never_rewrites_another_terminal_fact() -> None:
    for state, error_code in (
        ("cancelled", None),
        ("succeeded", None),
        ("failed", "internal_error"),
        ("running", None),
    ):
        jobs = _SettleableJobs(_SettleableJob(state, error_code))
        settlement = UnconfirmedCancellationSettlement(
            cast(Any, jobs), cast(Any, _QuarantiningCapacity())
        )
        assert settlement.settle("job-1") is (state == "cancelled"), state
        assert jobs.advances == [], state


def test_unconfirmed_settlement_is_idempotent_for_its_own_failure() -> None:
    jobs = _SettleableJobs(_SettleableJob("failed", "cancellation_unconfirmed"))
    capacity = _QuarantiningCapacity()
    settlement = UnconfirmedCancellationSettlement(cast(Any, jobs), cast(Any, capacity))

    assert settlement.settle("job-1") is True
    # The already-committed judgment is not replayed, but the isolation is retried.
    assert jobs.advances == []
    assert capacity.calls == [("job-1", "cancellation_unconfirmed")]


def test_unconfirmed_settlement_reports_a_lost_version_guard_as_unsettled() -> None:
    jobs = _SettleableJobs(_SettleableJob("cancelling"), conflict=True)
    settlement = UnconfirmedCancellationSettlement(
        cast(Any, jobs), cast(Any, _QuarantiningCapacity())
    )

    assert settlement.settle("job-1") is False


def test_confirmation_window_is_bounded() -> None:
    for value in (0, 3601):
        with pytest.raises(ValidationError):
            _settings(confirm_timeout_seconds=value)
