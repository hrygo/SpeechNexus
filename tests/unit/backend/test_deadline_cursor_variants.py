"""Every copied ``_DeadlineCursor`` must refuse to query after its budget expires.

``test_deadline_cursor.py`` pins the eleven ``_Cursor`` copies and explicitly
leaves ``_DeadlineCursor`` out of its scope. Four copies of that class live under
``backend/src`` and repeat the same shape: read the remaining budget, refuse when
it is gone, install a transaction-local ``statement_timeout`` in milliseconds,
then run the query.

Three of the four were already pinned in part by their own functional suites
(``test_production_owners.py``, ``test_runtime_postgres.py`` cover refusing and the
derived millisecond value; ``test_production_leases.py`` covers refusing and sending
nothing). ``quarantine_release_postgres`` had no coverage at all, and no test compared
the four implementations against one shared contract -- a drift in one copy, or a
constant millisecond value, could pass every existing suite.

All four are behaviourally identical apart from the concrete cursor annotation,
so one shared matrix runs against every implementation instead of four
near-identical test files. The discovery guard keeps that table complete: a new
``class _DeadlineCursor`` declaration without an entry here fails the census.
``claims_postgres`` left the table with its port (``#1102`` S3); the backend now
holds four declarations.

The census walks ``backend/src`` only. ``execution-gateway`` holds two more
copies of the same shape (``execution_reader.py``, ``submit_postgres.py``) with
different ``clock()`` return types; they are out of this table's scope and have
no budget assertions of their own (tracked separately).
"""

from __future__ import annotations

import importlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast
from unittest import mock

import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[3] / "backend"
SOURCE_ROOT = BACKEND_ROOT / "src"
CURSOR_DECLARATION = re.compile(r"^\s*class _DeadlineCursor\b", re.MULTILINE)

QUERY = "SELECT payload FROM example WHERE identity = ?"
PARAMETERS = ("row-a",)
SET_TIMEOUT = "SELECT set_config('statement_timeout', ?, true)"


@dataclass(frozen=True, slots=True)
class Implementation:
    """One copied bounded cursor and where its refusal error is defined."""

    module: str
    error: str


# Four declarations exist today; ``quarantine_release_postgres`` is the copy that
# is easiest to miss because the other three own the lease/owner/runtime ports.
IMPLEMENTATIONS = (
    Implementation(
        "speechnexus_backend.speech.infrastructure.leases_postgres",
        "_BudgetExpiredError",
    ),
    Implementation(
        "speechnexus_backend.speech.infrastructure.owners_postgres",
        "_BudgetExpiredError",
    ),
    Implementation(
        "speechnexus_backend.speech.infrastructure.runtime_postgres",
        "_BudgetExpiredError",
    ),
    Implementation(
        "speechnexus_backend.speech.infrastructure.quarantine_release_postgres",
        "_BudgetExpiredError",
    ),
)


class RecordingCursor:
    """Minimal cursor double: records calls, never reaches PostgreSQL."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[object, ...]]] = []
        self.rowcount = 1
        self.description: object | None = None

    def execute(self, query: str, parameters: tuple[object, ...] = ()) -> RecordingCursor:
        self.calls.append((query, parameters))
        return self

    def fetchall(self) -> list[dict[str, object]]:
        return []


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


def build(entry: Implementation, deadline: float) -> tuple[Any, RecordingCursor]:
    cursor_class = module(entry)._DeadlineCursor
    recorder = RecordingCursor()
    return cursor_class(cast(Any, recorder), deadline), recorder


def execute(entry: Implementation, cursor: Any) -> None:
    cursor.execute(QUERY, PARAMETERS)


def timeout_milliseconds(deadline: float, now: float) -> str:
    """The milliseconds the implementation must derive from this one clock read."""

    return str(max(1, int((deadline - now) * 1000)))


def discover_cursor_modules() -> set[str]:
    """Dotted module paths of every ``class _DeadlineCursor`` declaration in backend/src."""

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
        clock.advance(0.2)
        execute(entry, cursor)

    assert len(clock.reads) == 2, f"{entry.module} read the clock {clock.reads!r}"
    assert clock.reads[1] > clock.reads[0], "the fake clock did not advance"

    expected = [timeout_milliseconds(deadline, now) for now in clock.reads]
    assert int(expected[0]) > int(expected[1]), (
        f"{entry.module} sent a non-decreasing timeout for a shrinking budget: {expected!r}"
    )

    observed = [parameters[0] for query, parameters in recorder.calls if query == SET_TIMEOUT]
    assert observed == expected, (
        f"{entry.module} did not derive statement_timeout from the remaining budget: "
        f"observed {observed!r}, expected {expected!r}"
    )
    for value in observed:
        assert isinstance(value, str)


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
    assert isinstance(setup_parameters, tuple) and len(setup_parameters) == 1, (
        f"{entry.module} must pass statement_timeout as a one-element tuple: {setup_parameters!r}"
    )
    assert isinstance(setup_parameters[0], str)
    assert setup_parameters[0] == expected
    assert (query, query_parameters) == (QUERY, PARAMETERS)


def test_table_covers_every_deadline_cursor_declaration_in_the_backend() -> None:
    discovered = discover_cursor_modules()
    tabled = {entry.module for entry in IMPLEMENTATIONS}

    assert len(IMPLEMENTATIONS) == 4
    assert discovered == tabled, (
        "A backend _DeadlineCursor declaration is not classified here.\n"
        f"untested: {sorted(discovered - tabled)}\n"
        f"stale: {sorted(tabled - discovered)}"
    )
