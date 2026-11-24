"""An admission-cursor budget refusal is an unknown outcome, not an escaping crash.

``PostgresPublicJobs.create`` runs its admission reads through
``_DeadlineConnection.admission_cursor``, which lends out the leases module's
``_DeadlineCursor``. That cursor has its own refusal type,
``leases_postgres._BudgetExpiredError``, while every other bounded call in this
module raises ``PublicJobUnavailableError``. ``create`` used to dispatch only the
latter: a budget that ran out after the admission cursor was handed out escaped
the store as an unhandled exception, even though the surrounding transaction had
rolled back and no Job row was written. Both refusals now answer ``unknown`` /
``outcome_unknown`` (#1021).

The doubles below never touch PostgreSQL or real time. One test lets the real
``_DeadlineCursor`` guard refuse after the timestamp read consumes the remaining
budget; the parametrised test raises each refusal type from the same fake raw
cursor so both dispatch branches are compared under one construction.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest

from speechnexus_backend.speech.domain.public_jobs import (
    JobCreateResult,
    NewPublicJob,
    PublicJobUnavailableError,
)
from speechnexus_backend.speech.infrastructure import public_jobs_postgres
from speechnexus_backend.speech.infrastructure.leases_postgres import _BudgetExpiredError
from speechnexus_backend.speech.infrastructure.postgres_pool import SpeechPool
from speechnexus_backend.speech.infrastructure.public_jobs_postgres import PostgresPublicJobs
from tests.support.keyed_lock_double import answer_keyed_lock, business_inserts
from tests.unit.backend.test_public_job_pin_postgres import Pool, Rows, _request

START = 1_000.0
TIMEOUT = 3.0
NOW_US = 1_700_000_000_000_000
EXPIRED = START + TIMEOUT + 0.5
SET_TIMEOUT = "SELECT set_config('statement_timeout', ?, true)"


def _epoch_us(microseconds: int) -> datetime:
    """替身回答时钟时用的时间点:epoch 起算的微秒,必须是带时区的时间。"""

    return datetime(1970, 1, 1, tzinfo=UTC) + timedelta(microseconds=microseconds)


class Clock:
    """A replaceable ``time.monotonic`` shared by this module and ``leases_postgres``."""

    def __init__(self) -> None:
        self.now = START

    def __call__(self) -> float:
        return self.now


class RefusingCursor:
    """Raw admission cursor that installs the timeout, then refuses the read."""

    rowcount = 1

    def __init__(self, failure: BaseException | None) -> None:
        self.failure = failure
        self.calls: list[tuple[str, tuple[object, ...]]] = []
        self.rows: list[dict[str, object]] = []

    def execute(self, query: str, parameters: tuple[object, ...] = ()) -> RefusingCursor:
        self.calls.append((query, parameters))
        if "set_config" in query:
            return self
        answered = answer_keyed_lock(query, parameters)
        if answered is not None:
            # 绑定行锁先于准入读;它由共享替身回答,不是这次拒绝要模拟的那条语句。
            self.rows = answered
            return self
        assert self.failure is not None, "the real budget guard was expected to refuse first"
        raise self.failure

    def fetchall(self) -> list[dict[str, object]]:
        return self.rows

    def __enter__(self) -> RefusingCursor:
        return self

    def __exit__(self, *exc_info: object) -> None:
        return None

    def close(self) -> None:
        return None


class AdmissionPool(Pool):
    """A fake pool that is also the raw connection and mints the admission cursor.

    The inherited ``connection`` context manager counts commits and rollbacks, so
    the tests can prove the failed admission never committed.
    """

    def __init__(self, request: NewPublicJob) -> None:
        super().__init__(request)
        self.cursors: list[RefusingCursor] = []
        self.cursor_failure: BaseException | None = None

    def execute(self, query: str, parameters: tuple[object, ...] = ()) -> Rows:
        # ``_database_now_us`` runs on the deadline connection before admission;
        # 时钟语句来自登记的 profile,替身按同一列名回答。
        if "clock_timestamp()" in query:
            self.calls.append((query, parameters))
            return Rows([{"now": _epoch_us(NOW_US)}])
        return super().execute(query, parameters)

    def cursor(self) -> RefusingCursor:
        # 与真实驱动一致:``cursor()`` 直接给游标(游标本身支持 ``with``),而不是上下文管理器。
        cursor = RefusingCursor(self.cursor_failure)
        self.cursors.append(cursor)
        return cursor


def _driver(
    monkeypatch: pytest.MonkeyPatch,
    request: NewPublicJob,
    *,
    failure: BaseException | None = None,
    consume_budget_after_now_read: bool = False,
) -> tuple[AdmissionPool, PostgresPublicJobs]:
    clock = Clock()
    pool = AdmissionPool(request)
    pool.cursor_failure = failure
    monkeypatch.setattr("time.monotonic", clock)
    if consume_budget_after_now_read:
        real_now_us = public_jobs_postgres._database_now_us

        def consuming_now_us(connection: Any) -> int:
            value = real_now_us(connection)
            clock.now = EXPIRED
            return value

        monkeypatch.setattr(public_jobs_postgres, "_database_now_us", consuming_now_us)
    jobs = PostgresPublicJobs(
        cast(SpeechPool, pool),
        admission_pool=cast(SpeechPool, pool),
        timeout_seconds=TIMEOUT,
    )
    return pool, jobs


def _assert_rolled_back_unknown(pool: AdmissionPool, result: JobCreateResult) -> None:
    assert (result.outcome, result.error_code) == ("unknown", "outcome_unknown")
    assert pool.cursors, "no admission cursor was ever handed out"
    assert business_inserts(pool.calls) == [], (
        "the admission transaction wrote business rows after its budget ran out"
    )
    assert pool.commits == 0 and pool.rollbacks == 1, (
        "expected one rolled-back transaction and no commit, got "
        f"{pool.rollbacks} rollback(s) and {pool.commits} commit(s)"
    )


def test_the_real_budget_guard_is_answered_as_an_unknown_outcome(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = _request()
    pool, jobs = _driver(monkeypatch, request, consume_budget_after_now_read=True)

    result = jobs.create(request)

    _assert_rolled_back_unknown(pool, result)
    # No exception was scripted: 准入游标自己的预算检查在触库之前就拒绝,
    # 所以它一条语句都没发出去(绑定行锁走的是写者连接上的另一条游标)。
    assert pool.cursors[-1].calls == []


@pytest.mark.parametrize(
    "failure",
    [_BudgetExpiredError(), PublicJobUnavailableError("synthetic budget refusal")],
    ids=["budget_expired", "public_job_unavailable"],
)
def test_both_admission_refusal_types_share_one_unknown_answer(
    failure: BaseException, monkeypatch: pytest.MonkeyPatch
) -> None:
    request = _request()
    pool, jobs = _driver(monkeypatch, request, failure=failure)

    result = jobs.create(request)

    _assert_rolled_back_unknown(pool, result)
    # The refusal happened mid-transaction: the timeout install and the first
    # admission read were already issued before the cursor refused.
    calls = pool.cursors[-1].calls
    assert calls[0][0] == SET_TIMEOUT
    # 拒绝发生在事务中途:超时已经装好,随后有一条非超时语句被拒。
    assert len(calls) >= 2 and calls[-1][0] != SET_TIMEOUT
