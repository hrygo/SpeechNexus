"""The admission transaction's deadline guard in ``public_jobs_postgres``.

``test_deadline_cursor_variants.py`` pins the five ``_DeadlineCursor`` copies and
deliberately leaves ``_DeadlineConnection`` out of its census. That class and the
``_DeadlineRows`` wrapper it hands out are the only implementation of one more
sentence: "control round trips also consume time". ``arm`` installs a
transaction-local ``statement_timeout`` derived from ``deadline - now``, then
re-checks the budget *after* that control round trip; ``fetchall``/``fetchone``
check both before and after reading rows, so a read that finishes after the
deadline is discarded instead of returned.

Nothing in ``tests/unit`` referenced ``_DeadlineConnection``, ``_DeadlineRows``
or ``_remaining`` before this file. Deleting the post-fetch check in
``fetchall``, the trailing check in ``arm``, or the ``max(1, ...)`` floor left
every suite green. Each test below names the one expression it pins, and the
fake clock/connection record exact driver calls so a reordered ``arm`` also
fails rather than passing by accident.

The refusal types differ by path and are pinned as they actually behave:
``_remaining`` raises the module-wide ``PublicJobUnavailableError``, while the
``_DeadlineCursor`` yielded by ``admission_cursor`` raises
``leases_postgres._BudgetExpiredError`` (a bare ``Exception``). Only the Python
side is covered here; whether these checks stop a real ``COMMIT`` is a property
of the driver and the server-side ``statement_timeout``, not of this file.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, cast
from unittest import mock

import pytest

from speechnexus_backend.speech.domain.public_jobs import PublicJobUnavailableError
from speechnexus_backend.speech.infrastructure.leases_postgres import _BudgetExpiredError
from speechnexus_backend.speech.infrastructure.public_jobs_postgres import (
    _DeadlineConnection,
    _DeadlineRows,
    _remaining,
)

QUERY = "SELECT payload FROM example WHERE identity = %s"
PARAMETERS = ("row-a",)
SET_TIMEOUT = "SELECT set_config('statement_timeout', %s, true)"
ROW = {"payload": "row-a"}


class FakeClock:
    """Replacement for ``time.monotonic`` that records every read."""

    def __init__(self, now: float) -> None:
        self.now = now
        self.reads: list[float] = []

    def __call__(self) -> float:
        self.reads.append(self.now)
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class RawRows:
    """Rows double; ``during`` runs while the driver "reads", spending budget."""

    def __init__(
        self,
        rows: list[dict[str, Any]],
        *,
        rowcount: int = 1,
        during: Callable[[], None] | None = None,
    ) -> None:
        self.rows = rows
        self.count = rowcount
        self.during = during
        self.fetches: list[str] = []

    @property
    def rowcount(self) -> int:
        return self.count

    def fetchall(self) -> list[dict[str, Any]]:
        self.fetches.append("fetchall")
        if self.during is not None:
            self.during()
        return list(self.rows)

    def fetchone(self) -> dict[str, Any] | None:
        self.fetches.append("fetchone")
        if self.during is not None:
            self.during()
        return self.rows[0] if self.rows else None


class RecordingCursor:
    """Cursor double handed to ``_DeadlineCursor``; records calls, reads nothing."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[object, ...]]] = []
        self.rows = RawRows([ROW])

    def execute(self, query: str, parameters: tuple[object, ...] = ()) -> RawRows:
        self.calls.append((query, parameters))
        return self.rows

    def __enter__(self) -> RecordingCursor:
        return self

    def __exit__(self, *exception: object) -> None:
        return None


class RecordingConnection:
    """Connection double: records every statement, never reaches PostgreSQL."""

    def __init__(self, after_execute: Callable[[str], None] | None = None) -> None:
        self.calls: list[tuple[str, tuple[object, ...]]] = []
        self.rows = RawRows([ROW])
        self.after_execute = after_execute
        self.cursor_double = RecordingCursor()

    def execute(self, query: str, parameters: tuple[object, ...] = ()) -> RawRows:
        self.calls.append((query, parameters))
        if self.after_execute is not None:
            self.after_execute(query)
        return self.rows

    def cursor(self) -> RecordingCursor:
        return self.cursor_double


def make_connection(
    deadline: float, after_execute: Callable[[str], None] | None = None
) -> tuple[_DeadlineConnection, RecordingConnection]:
    raw = RecordingConnection(after_execute)
    return _DeadlineConnection(cast(Any, raw), deadline), raw


def make_rows(
    deadline: float,
    *,
    during: Callable[[], None] | None = None,
    rowcount: int = 7,
) -> tuple[_DeadlineRows, RawRows]:
    raw = RawRows([ROW], rowcount=rowcount, during=during)
    return _DeadlineRows(cast(Any, raw), deadline), raw


def timeout_milliseconds(deadline: float, now: float) -> str:
    """The milliseconds the implementation must derive from this one clock read."""

    return str(max(1, int((deadline - now) * 1000)))


def clock_patch(clock: FakeClock) -> Any:
    return mock.patch("time.monotonic", clock)


def test_remaining_refuses_at_and_past_the_deadline() -> None:
    """``_remaining`` (line 121-123) returns the budget or raises this module's error."""

    deadline = 100.0
    for now in (deadline + 1.0, deadline, deadline - 0.0):
        clock = FakeClock(now)
        with clock_patch(clock), pytest.raises(PublicJobUnavailableError) as raised:
            _remaining(deadline)
        assert type(raised.value) is PublicJobUnavailableError
    clock = FakeClock(deadline - 2.5)
    with clock_patch(clock):
        assert _remaining(deadline) == pytest.approx(2.5)


def test_arm_derives_the_timeout_and_runs_before_the_statement() -> None:
    """``arm`` (line 153-158) sends the derived timeout first, then the statement."""

    deadline = 100.0
    clock = FakeClock(97.5)
    connection, raw = make_connection(deadline)

    with clock_patch(clock):
        connection.execute(QUERY, PARAMETERS)
        clock.advance(0.25)
        connection.execute(QUERY, PARAMETERS)

    assert raw.calls == [
        (SET_TIMEOUT, ("2500",)),
        (QUERY, PARAMETERS),
        (SET_TIMEOUT, ("2250",)),
        (QUERY, PARAMETERS),
    ]
    sent = [parameters[0] for query, parameters in raw.calls if query == SET_TIMEOUT]
    assert sent == [
        timeout_milliseconds(deadline, 97.5),
        timeout_milliseconds(deadline, 97.75),
    ]
    assert sent[0] != sent[1], "the timeout is a constant, not derived from the clock"


def test_arm_sends_only_the_timeout_and_no_business_statement() -> None:
    """``arm`` (line 153-158) is a control round trip; it never runs user SQL."""

    deadline, now = 100.0, 90.0
    connection, raw = make_connection(deadline)

    with clock_patch(FakeClock(now)):
        connection.arm()

    assert raw.calls == [(SET_TIMEOUT, ("10000",))]


def test_sub_millisecond_budget_is_floored_at_one_millisecond() -> None:
    """``max(1, ...)`` (line 157) keeps a sub-millisecond budget from becoming "0"."""

    deadline = 100.0
    now = deadline - 1e-7
    assert 0 < deadline - now < 0.001
    assert timeout_milliseconds(deadline, now) == "1"
    connection, raw = make_connection(deadline)

    with clock_patch(FakeClock(now)):
        connection.execute(QUERY, PARAMETERS)

    assert raw.calls == [(SET_TIMEOUT, ("1",)), (QUERY, PARAMETERS)]


def test_arm_refuses_when_the_timeout_setup_consumed_the_budget() -> None:
    """``_remaining`` after ``set_config`` (line 161) refuses the next statement.

    This is the only implementation of "control round trips also consume time":
    the budget is still positive when ``arm`` starts, and the control round trip
    itself spends all of it. The business statement must never be sent.
    """

    deadline, now = 100.0, 90.0
    clock = FakeClock(now)

    def spend_the_budget(query: str) -> None:
        if query == SET_TIMEOUT:
            clock.advance(11.0)

    connection, raw = make_connection(deadline, spend_the_budget)

    with clock_patch(clock), pytest.raises(PublicJobUnavailableError):
        connection.execute("SELECT 1")

    assert raw.calls == [(SET_TIMEOUT, ("10000",))], (
        "the business statement was sent after the control round trip exhausted the budget"
    )


def test_execute_refuses_a_statement_that_finished_after_its_deadline() -> None:
    """``_remaining`` after the statement (line 166) discards a late result."""

    deadline, now = 100.0, 90.0
    clock = FakeClock(now)

    def overrun(query: str) -> None:
        if query == QUERY:
            clock.advance(11.0)

    connection, raw = make_connection(deadline, overrun)

    with clock_patch(clock), pytest.raises(PublicJobUnavailableError):
        connection.execute(QUERY, PARAMETERS)

    assert raw.calls == [(SET_TIMEOUT, ("10000",)), (QUERY, PARAMETERS)]


def test_expired_connection_sends_nothing_at_all() -> None:
    """An already-expired deadline stops before ``set_config`` too (line 154)."""

    deadline = 100.0
    connection, raw = make_connection(deadline)

    with clock_patch(FakeClock(deadline + 0.001)), pytest.raises(PublicJobUnavailableError):
        connection.execute(QUERY, PARAMETERS)

    assert raw.calls == []


@pytest.mark.parametrize("method", ["fetchall", "fetchone"])
def test_rows_refuse_before_reading_when_already_expired(method: str) -> None:
    """The first ``_remaining`` (lines 137/143) refuses before touching the driver."""

    deadline = 100.0
    rows, raw = make_rows(deadline)

    with (
        clock_patch(FakeClock(deadline + 0.001)),
        pytest.raises(PublicJobUnavailableError),
    ):
        getattr(rows, method)()

    assert raw.fetches == [], "the driver was asked for rows after the deadline"


@pytest.mark.parametrize("method", ["fetchall", "fetchone"])
def test_rows_refuse_after_reading_when_the_read_consumed_the_budget(method: str) -> None:
    """The second ``_remaining`` (lines 139/145) discards rows read past the deadline.

    The budget is still positive when the read starts and the driver does return
    rows; only the post-read check can see that the read itself spent the budget.
    """

    deadline = 100.0
    clock = FakeClock(deadline - 0.5)

    def spend_the_budget() -> None:
        clock.advance(1.0)

    rows, raw = make_rows(deadline, during=spend_the_budget)

    with clock_patch(clock), pytest.raises(PublicJobUnavailableError):
        getattr(rows, method)()

    assert raw.fetches == [method], "the read did not happen, so this is not the post-read check"


def test_rows_return_the_row_when_the_budget_survives_the_read() -> None:
    """Both checks pass while the clock stays inside the budget (lines 137-146)."""

    deadline = 100.0
    rows, raw = make_rows(deadline)

    with clock_patch(FakeClock(deadline - 0.5)):
        assert rows.fetchall() == [ROW]
        assert rows.fetchone() == ROW

    assert raw.fetches == ["fetchall", "fetchone"]


def test_rowcount_is_guarded_by_the_deadline() -> None:
    """The ``rowcount`` property (line 133) re-checks before reading the attribute."""

    deadline = 100.0
    rows, _ = make_rows(deadline, rowcount=7)

    with clock_patch(FakeClock(deadline - 1.0)):
        assert rows.rowcount == 7

    with clock_patch(FakeClock(deadline + 0.001)), pytest.raises(PublicJobUnavailableError):
        _ = rows.rowcount


def test_admission_cursor_shares_the_connection_deadline() -> None:
    """``admission_cursor`` (line 172) hands out the caller's deadline, not a new one.

    A cursor built from ``time.monotonic() + 999`` would both derive a different
    millisecond value here and survive the expiry below.
    """

    deadline = 100.0
    clock = FakeClock(97.5)
    connection, raw = make_connection(deadline)

    with clock_patch(clock), connection.admission_cursor() as cursor:
        cursor.execute(QUERY, PARAMETERS)
        clock.now = deadline + 0.001
        with pytest.raises(_BudgetExpiredError):
            cursor.execute("SELECT 1")

    assert raw.cursor_double.calls == [(SET_TIMEOUT, ("2500",)), (QUERY, PARAMETERS)], (
        "the cursor did not install its timeout from the connection's deadline"
    )
