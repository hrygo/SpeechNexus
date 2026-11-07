"""Composition switch and validation for the cancellation reconciliation worker."""

from __future__ import annotations

from typing import Any, cast

import pytest
from pydantic import ValidationError

from speechnexus_backend.job_cancellation_runtime import (
    JobCancellationRuntime,
    JobCancellationSettings,
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
