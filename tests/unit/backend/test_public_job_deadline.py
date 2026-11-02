"""A controllable clock measures the actual adapter's whole transaction."""

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any, cast

import psycopg
import pytest

from speechnexus_backend.speech.infrastructure.postgres_pool import SpeechPool
from speechnexus_backend.speech.infrastructure.public_jobs_postgres import PostgresPublicJobs
from tests.unit.backend.test_public_job_pin_postgres import Pool, Rows, _request


class TimedPool(Pool):
    elapsed = 0.0
    cost = 0.0
    final_read_cost = 0.0
    pool_cost = 0.0
    commit_cost = 0.0
    control_cost = 0.0
    fail_unique = False
    acquisitions = 0

    @contextmanager
    def connection(self, *, timeout: float) -> Iterator[Any]:
        self.acquisitions += 1
        if self.acquisitions == 2:
            assert timeout <= 0.21, "retry reset the original deadline"
        self.elapsed += self.pool_cost
        with super().connection(timeout=timeout) as connection:
            yield connection
        self.elapsed += self.commit_cost

    def execute(self, query: str, parameters: tuple[object, ...] = ()) -> Rows:
        if "speechnexus:public-job-key:" in query:
            self.elapsed += 2.8
        elif "set_config" in query:
            self.elapsed += self.control_cost
        elif "WHERE j.job_id = %s" in query:
            self.elapsed += self.final_read_cost
        else:
            self.elapsed += self.cost
        result = super().execute(query, parameters)
        if self.fail_unique and query.startswith("INSERT"):
            self.fail_unique = False
            raise psycopg.errors.UniqueViolation("synthetic rollback")
        return result


@pytest.mark.parametrize("phase", ["cumulative", "final_read", "pool", "control", "retry"])
def test_expired_transaction_rolls_back_and_cannot_commit(
    phase: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    pool = TimedPool(_request())
    if phase == "cumulative":
        pool.cost = 0.045  # Each statement fits 200ms; the sequence does not.
    elif phase == "final_read":
        pool.final_read_cost = 0.21
    elif phase == "pool":
        pool.pool_cost = 3.01
    elif phase == "control":
        pool.control_cost = 0.11
    else:
        pool.fail_unique = True
    monkeypatch.setattr(
        "speechnexus_backend.speech.infrastructure.public_jobs_postgres.time.monotonic",
        lambda: pool.elapsed,
    )
    result = PostgresPublicJobs(cast(SpeechPool, pool)).create(pool.request)
    assert result.outcome == "unknown"
    assert pool.commits == 0 and pool.rollbacks >= 1
    if phase == "pool":
        assert pool.calls == []
    if phase == "retry":
        assert pool.acquisitions == 2


def test_commit_returning_after_deadline_is_unknown_not_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool = TimedPool(_request())
    pool.commit_cost = 0.21
    monkeypatch.setattr(
        "speechnexus_backend.speech.infrastructure.public_jobs_postgres.time.monotonic",
        lambda: pool.elapsed,
    )
    result = PostgresPublicJobs(cast(SpeechPool, pool)).create(pool.request)
    assert result.outcome == "unknown"
    assert pool.commits == 1 and pool.rollbacks == 0


@pytest.mark.parametrize("error", [psycopg.errors.UniqueViolation, psycopg.OperationalError])
def test_commit_error_never_uses_the_known_rollback_retry_path(error: type[psycopg.Error]) -> None:
    class CommitErrorPool(Pool):
        acquisitions = 0

        @contextmanager
        def connection(self, *, timeout: float) -> Iterator[Any]:
            self.acquisitions += 1
            with super().connection(timeout=timeout) as raw:
                yield raw
            raise error("synthetic unknown commit receipt")

    pool = CommitErrorPool(_request())
    assert PostgresPublicJobs(cast(SpeechPool, pool)).create(pool.request).outcome == "unknown"
    assert pool.acquisitions == pool.commits == 1
