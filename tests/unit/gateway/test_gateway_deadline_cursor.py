"""The two gateway ``_DeadlineCursor`` copies obey the same budget contract.

``tests/unit/backend/test_deadline_cursor_variants.py`` pins the five copies under
``backend/src`` with a discovery census rooted there. The execution gateway holds two
more copies of the same shape, and that census cannot see them:

* ``execution_reader._DeadlineCursor`` exposes no ``clock()`` at all -- the reader only
  runs bounded selects, so it never needs the database clock;
* ``submit_postgres._DeadlineCursor.clock()`` returns a ``datetime`` decoded by
  ``submit_sql.from_unix_micros``, where the five backend copies return an int.

Neither copy had a budget assertion of its own: deleting the ``remaining <= 0`` guard or
replacing ``remaining * 1000`` with a constant left every pre-existing gateway unit test
green. This file runs the same shared matrix against the two gateway copies, resolving the two
interface differences above instead of flattening them into the backend shape, and keeps
its own census rooted at ``execution-gateway/src`` so a new unregistered copy fails here.
"""

from __future__ import annotations

import importlib
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from unittest import mock

import pytest

GATEWAY_ROOT = Path(__file__).resolve().parents[3] / "execution-gateway"
SOURCE_ROOT = GATEWAY_ROOT / "src"
CURSOR_DECLARATION = re.compile(r"^\s*class _DeadlineCursor\b", re.MULTILINE)

QUERY = "SELECT payload FROM example WHERE identity = %s"
PARAMETERS = ("row-a",)
SET_TIMEOUT = "SELECT set_config('statement_timeout', %s, true)"
CLOCK_QUERY = "SELECT CAST(EXTRACT(EPOCH FROM clock_timestamp()) * 1000000 AS BIGINT) AS now_us"


@dataclass(frozen=True, slots=True)
class Implementation:
    """One copied bounded cursor, its refusal error, and how it reads the database clock."""

    module: str
    error: str
    #: ``None`` means the copy has no ``clock()`` method at all.
    clock_returns: type | None


IMPLEMENTATIONS = (
    Implementation(
        "speechnexus_gateway.infrastructure.execution_reader",
        "_BudgetExpiredError",
        None,
    ),
    Implementation(
        "speechnexus_gateway.infrastructure.submit_postgres",
        "_BudgetExpiredError",
        datetime,
    ),
)

CLOCK_IMPLEMENTATIONS = tuple(entry for entry in IMPLEMENTATIONS if entry.clock_returns is not None)


class RecordingCursor:
    """Minimal cursor double: records calls, never reaches PostgreSQL."""

    def __init__(self, rows: list[dict[str, Any]] | None = None) -> None:
        self.calls: list[tuple[str, tuple[object, ...]]] = []
        self.rows: list[dict[str, Any]] = [] if rows is None else rows
        self.rowcount = 1
        self.description: object | None = None

    def execute(self, query: str, parameters: tuple[object, ...] = ()) -> RecordingCursor:
        self.calls.append((query, parameters))
        return self

    def fetchall(self) -> list[dict[str, Any]]:
        return self.rows


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


def cursor_id(entry: Implementation) -> str:
    return entry.module.rsplit(".", 1)[-1]


def module(entry: Implementation) -> Any:
    return importlib.import_module(entry.module)


def error_type(entry: Implementation) -> Any:
    return getattr(module(entry), entry.error)


def build(
    entry: Implementation,
    deadline: float,
    rows: list[dict[str, Any]] | None = None,
) -> tuple[Any, RecordingCursor]:
    cursor_class = module(entry)._DeadlineCursor
    recorder = RecordingCursor(rows)
    return cursor_class(cast(Any, recorder), deadline), recorder


def execute(entry: Implementation, cursor: Any) -> None:
    cursor.execute(QUERY, PARAMETERS)


def timeout_milliseconds(deadline: float, now: float) -> str:
    """The milliseconds the implementation must derive from this one clock read."""

    return str(max(1, int((deadline - now) * 1000)))


def discover_cursor_modules() -> set[str]:
    """Dotted module paths of every ``class _DeadlineCursor`` declaration in gateway/src."""

    discovered: set[str] = set()
    for path in SOURCE_ROOT.rglob("*.py"):
        if not CURSOR_DECLARATION.search(path.read_text(encoding="utf-8")):
            continue
        parts = list(path.relative_to(SOURCE_ROOT).with_suffix("").parts)
        if parts[-1] == "__init__":
            parts.pop()
        discovered.add(".".join(parts))
    return discovered


def attempt(entry: Implementation, cursor: Any, clock: FakeClock) -> BaseException | None:
    """Run one query under the fake clock, returning whatever the cursor raised."""

    raised: BaseException | None = None
    with mock.patch("time.monotonic", clock):
        try:
            execute(entry, cursor)
        except BaseException as error:
            raised = error
    return raised


@pytest.mark.parametrize("entry", IMPLEMENTATIONS, ids=cursor_id)
def test_expired_deadline_refuses_before_any_query(entry: Implementation) -> None:
    expected = error_type(entry)
    now = 100.0
    cursor, recorder = build(entry, deadline=now - 1)

    raised = attempt(entry, cursor, FakeClock(now))

    assert len(recorder.calls) == 0, (
        f"{entry.module} sent work to the database after its deadline: {recorder.calls!r}"
    )
    assert type(raised) is expected, (
        f"{entry.module} raised {type(raised)!r} instead of {entry.error}"
    )


@pytest.mark.parametrize("entry", IMPLEMENTATIONS, ids=cursor_id)
def test_zero_remaining_deadline_is_already_expired(entry: Implementation) -> None:
    expected = error_type(entry)
    deadline = now = 100.0
    cursor, recorder = build(entry, deadline=deadline)

    raised = attempt(entry, cursor, FakeClock(now))

    assert len(recorder.calls) == 0, (
        f"{entry.module} let an exactly-expired deadline reach the database: {recorder.calls!r}"
    )
    assert type(raised) is expected, (
        f"{entry.module} raised {type(raised)!r} instead of {entry.error}"
    )


@pytest.mark.parametrize("entry", IMPLEMENTATIONS, ids=cursor_id)
def test_timeout_milliseconds_follow_the_shrinking_budget(entry: Implementation) -> None:
    deadline = 200.0
    clock = FakeClock(deadline - 3.0)
    cursor, recorder = build(entry, deadline=deadline)

    with mock.patch("time.monotonic", clock):
        execute(entry, cursor)
        clock.advance(0.25)
        execute(entry, cursor)

    assert len(clock.reads) == 2, f"{entry.module} read the clock {clock.reads!r}"
    assert clock.reads[1] > clock.reads[0], "the fake clock did not advance"

    expected = [timeout_milliseconds(deadline, now) for now in clock.reads]
    assert expected == ["3000", "2750"], (
        f"the fake budget did not shrink by an exact quarter second: {expected!r}"
    )

    # Every business statement is preceded by its own transaction-local timeout.
    assert recorder.calls == [
        (SET_TIMEOUT, ("3000",)),
        (QUERY, PARAMETERS),
        (SET_TIMEOUT, ("2750",)),
        (QUERY, PARAMETERS),
    ], (
        f"{entry.module} did not derive statement_timeout from the remaining budget: "
        f"{recorder.calls!r}"
    )


@pytest.mark.parametrize("entry", IMPLEMENTATIONS, ids=cursor_id)
def test_sub_millisecond_remaining_deadline_never_sends_zero_milliseconds(
    entry: Implementation,
) -> None:
    deadline = 100.0
    now = deadline - 1e-7
    assert 0 < deadline - now < 0.001
    expected = timeout_milliseconds(deadline, now)
    assert expected == "1"

    cursor, recorder = build(entry, deadline=deadline)
    clock = FakeClock(now)
    with mock.patch("time.monotonic", clock):
        execute(entry, cursor)

    assert recorder.calls == [(SET_TIMEOUT, (expected,)), (QUERY, PARAMETERS)], (
        f"{entry.module} did not floor a sub-millisecond budget at 1ms: {recorder.calls!r}"
    )


@pytest.mark.parametrize("entry", IMPLEMENTATIONS, ids=cursor_id)
def test_each_query_is_preceded_by_a_transaction_local_timeout(entry: Implementation) -> None:
    deadline, now = 100.0, 97.5
    expected = timeout_milliseconds(deadline, now)
    assert expected == "2500"
    cursor, recorder = build(entry, deadline=deadline)
    clock = FakeClock(now)

    with mock.patch("time.monotonic", clock):
        execute(entry, cursor)

    assert len(recorder.calls) == 2, (
        f"{entry.module} did not send exactly one timeout plus one query: {recorder.calls!r}"
    )
    setup, setup_parameters = recorder.calls[0]
    query, query_parameters = recorder.calls[1]
    assert setup == SET_TIMEOUT, f"{entry.module} changed the transaction-local set_config call"
    assert ", true)" in setup, f"{entry.module} made statement_timeout session-wide"
    assert isinstance(setup_parameters, tuple) and len(setup_parameters) == 1, (
        f"{entry.module} must pass statement_timeout as a one-element tuple: {setup_parameters!r}"
    )
    assert isinstance(setup_parameters[0], str)
    assert setup_parameters[0] == expected
    assert (query, query_parameters) == (QUERY, PARAMETERS)


@pytest.mark.parametrize("entry", CLOCK_IMPLEMENTATIONS, ids=cursor_id)
def test_database_clock_keeps_the_timestamp_type_its_callers_expect(
    entry: Implementation,
) -> None:
    """``submit_postgres`` hands out an aware ``datetime``, not the backend's int micros."""

    assert entry.clock_returns is datetime
    deadline, now = 200.0, 190.5
    micros = 1_700_000_000_000_000
    cursor, recorder = build(entry, deadline=deadline, rows=[{"now_us": micros}])

    with mock.patch("time.monotonic", FakeClock(now)):
        value = cursor.clock()

    assert type(value) is datetime, f"{entry.module}.clock() returned {type(value)!r}"
    assert value.tzinfo is UTC
    assert value == datetime(2023, 11, 14, 22, 13, 20, tzinfo=UTC)
    assert recorder.calls == [(SET_TIMEOUT, ("9500",)), (CLOCK_QUERY, ())], (
        f"{entry.module}.clock() did not read the database clock inside the budget: "
        f"{recorder.calls!r}"
    )


@pytest.mark.parametrize("entry", CLOCK_IMPLEMENTATIONS, ids=cursor_id)
def test_database_clock_refuses_after_the_budget_expires(entry: Implementation) -> None:
    expected = error_type(entry)
    deadline, now = 100.0, 100.5
    cursor, recorder = build(entry, deadline=deadline, rows=[{"now_us": 0}])

    with mock.patch("time.monotonic", FakeClock(now)), pytest.raises(expected):
        cursor.clock()

    assert recorder.calls == [], (
        f"{entry.module}.clock() reached the database after its deadline: {recorder.calls!r}"
    )


@pytest.mark.parametrize("entry", IMPLEMENTATIONS, ids=cursor_id)
def test_each_copy_exposes_a_clock_exactly_when_the_table_says_so(
    entry: Implementation,
) -> None:
    """The table records whether a copy has ``clock()``; nothing else checks it.

    Without this, adding ``clock()`` to ``execution_reader`` -- a copy whose callers
    never expect one -- stays green, and the ``clock_returns`` column becomes prose.
    """
    cls = module(entry)._DeadlineCursor
    assert hasattr(cls, "clock") is (entry.clock_returns is not None), (
        f"{entry.module}._DeadlineCursor clock() presence does not match the table"
    )


def test_table_covers_every_deadline_cursor_declaration_in_the_gateway() -> None:
    discovered = discover_cursor_modules()
    tabled = {entry.module for entry in IMPLEMENTATIONS}

    assert len(IMPLEMENTATIONS) == 2
    assert discovered == tabled, (
        "A gateway _DeadlineCursor declaration is not classified here.\n"
        f"untested: {sorted(discovered - tabled)}\n"
        f"stale: {sorted(tabled - discovered)}"
    )
