"""The sweep reports what it did, and its one predicate comes from the domain.

These cases pin the properties `#1248` needs from the coordinator-side sweep:

* the selection is one statement, bounded, and takes its terminal state set from
  ``domain.public_jobs.TERMINAL_STATES`` instead of restating it (deleting the
  condition or restating the set turns these red);
* a refusal, an unknown commit or a storage fault on one lease never abandons the
  rest of the pass, and every outcome is reported with a stable code;
* the returned-slot count comes from the released row, so "capacity came back" is
  never inferred from an exit code;
* a pass is bounded (``1..MAX_SWEEP``) and ``--dry-run`` writes nothing.

The database behaviour behind the port -- that the release really rolls the four
projections back on PostgreSQL and InnoDB -- is
``tests/integration/test_lease_reconciliation.py``, not this file.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from io import StringIO
from pathlib import Path
from types import ModuleType
from typing import Any, cast

import pytest

from speechnexus_backend.lease_reconciler import (
    EXIT_ALARM,
    EXIT_CLEAN,
    EXIT_USAGE,
    main,
    pass_line,
    run,
    sweep_line,
)
from speechnexus_backend.speech.application.lease_reconciliation import (
    MAX_SWEEP,
    LeaseCandidate,
    LeaseReconciliation,
    LeaseReconciliationReport,
    LeaseReconciliationTransactions,
    LeaseSweep,
    release_request,
)
from speechnexus_backend.speech.domain.capacity import PhysicalResources
from speechnexus_backend.speech.domain.lease_release import ReleaseLease
from speechnexus_backend.speech.domain.leases import LeaseRecord, LeaseResult, rejected
from speechnexus_backend.speech.domain.public_jobs import TERMINAL_STATES
from speechnexus_backend.speech.infrastructure.lease_reconciliation_sql import (
    CANDIDATE_STATEMENT,
    CANDIDATE_STATES,
    candidate_rows,
)
from speechnexus_portable_sql.contract import ListMarker

NOW_US = 1_795_700_000_000_000


def _candidate(lease_id: str = "lease-batch-stage-main", version: int = 4) -> LeaseCandidate:
    return LeaseCandidate(
        lease_id=lease_id,
        version=version,
        operation_id="job-batch-1",
        attempt_id="attempt-batch-1",
        stage_id="stage-main",
        reserved_slots=1,
        attempt_quota_state="held",
    )


def _released(candidate: LeaseCandidate) -> LeaseRecord:
    return LeaseRecord(
        candidate.lease_id,
        "released",
        "complete",
        candidate.version + 1,
        PhysicalResources(0, 0, 0, 0, 0, 0),
        candidate.reserved_slots,
        budget_released_at_us=NOW_US,
    )


@dataclass
class _Cursor:
    rows: list[dict[str, Any]]
    queries: list[tuple[str, tuple[object, ...]]]
    sizes: list[int]

    def execute(self, query: str, parameters: tuple[object, ...] = ()) -> object:
        self.queries.append((query, parameters))
        return None

    def fetchmany(self, size: int) -> list[dict[str, Any]]:
        self.sizes.append(size)
        return self.rows[:size]


class _Port:
    """A scripted port: one lease per entry, so the loop is observable without SQL."""

    def __init__(
        self,
        candidates: tuple[LeaseCandidate, ...],
        results: dict[str, LeaseResult] | None = None,
        *,
        fault: BaseException | None = None,
    ) -> None:
        self.candidates_read: list[int] = []
        self.released: list[LeaseCandidate] = []
        self._candidates = candidates
        self._results = results or {}
        self._fault = fault

    def candidates(self, limit: int) -> tuple[LeaseCandidate, ...]:
        self.candidates_read.append(limit)
        return self._candidates

    def release(self, candidate: LeaseCandidate) -> LeaseResult:
        self.released.append(candidate)
        if self._fault is not None:
            raise self._fault
        return self._results.get(candidate.lease_id, _committed(candidate))


def _committed(candidate: LeaseCandidate) -> LeaseResult:
    return LeaseResult("committed", candidate.lease_id, _released(candidate))


def test_the_selection_takes_its_terminal_set_from_the_domain() -> None:
    # Deleting the condition, or restating the four states next to the SQL, fails
    # here: the value has to be the domain's own set.
    assert tuple(sorted(TERMINAL_STATES)) == CANDIDATE_STATES
    assert "AND j.state IN (?) " in CANDIDATE_STATEMENT
    assert "AND NOT EXISTS (SELECT 1 FROM submit_intents AS s " in CANDIDATE_STATEMENT
    assert "l.lease_state = 'reserved' AND l.deadline_at_us < ?" in CANDIDATE_STATEMENT
    # The bound is a client-side truncation: LIMIT is outside the portable subset.
    assert " LIMIT " not in CANDIDATE_STATEMENT
    assert "%s" not in CANDIDATE_STATEMENT


def test_candidates_are_read_once_bounded_and_oldest_deadline_first() -> None:
    rows = [
        {
            "lease_id": f"lease-batch-{index}",
            "version": 2,
            "operation_id": "job-batch-1",
            "attempt_id": "attempt-batch-1",
            "stage_id": "stage-main",
            "reserved_slots": 1,
            "attempt_quota_state": "held",
        }
        for index in range(3)
    ]
    cursor = _Cursor(rows, [], [])
    selected = candidate_rows(cast(Any, cursor), now_us=NOW_US, limit=2)
    query, parameters = cursor.queries[0]
    assert query == CANDIDATE_STATEMENT
    assert parameters[0] == NOW_US
    marker = cast(ListMarker, parameters[1])
    assert tuple(marker) == tuple(sorted(TERMINAL_STATES))
    assert cursor.sizes == [2]
    assert len(selected) == 2  # The truncation is the bound, not a page key.
    assert CANDIDATE_STATEMENT.endswith("ORDER BY l.deadline_at_us, l.lease_id")


@pytest.mark.parametrize("limit", [0, -1])
def test_a_selection_needs_a_positive_bound(limit: int) -> None:
    with pytest.raises(ValueError):
        candidate_rows(cast(Any, _Cursor([], [], [])), now_us=NOW_US, limit=limit)


def test_the_release_request_is_the_existing_one() -> None:
    candidate = _candidate()
    request = release_request(candidate)
    assert isinstance(request, ReleaseLease)
    assert (request.lease_id, request.expected_version) == (candidate.lease_id, candidate.version)


def test_a_pass_settles_every_candidate_and_counts_only_released_slots() -> None:
    candidates = (_candidate("lease-a"), _candidate("lease-b"), _candidate("lease-c"))
    port = _Port(
        candidates,
        {
            "lease-a": LeaseResult("committed", "lease-a", _released(candidates[0])),
            "lease-b": LeaseResult("replayed", "lease-b", _released(candidates[1])),
            "lease-c": rejected("lease-c", "version_conflict"),
        },
    )
    report = LeaseReconciliation(port).run_once(limit=8)
    assert port.candidates_read == [8]
    assert [sweep.decision for sweep in report.sweeps] == ["released", "replayed", "refused"]
    assert report.released == 1 and report.replayed == 1 and report.refused == 1
    # A replay returns nothing new; the released row returns its own reservation.
    assert report.returned_slots == 1
    assert report.sweeps[-1].error_code == "version_conflict"


def test_one_fault_does_not_abandon_the_rest_of_the_pass() -> None:
    candidate = _candidate()
    port = _Port((candidate,), fault=RuntimeError("storage"))
    report = LeaseReconciliation(port).run_once(limit=4)
    assert len(port.released) == 1
    assert report.unknown == 1
    assert report.sweeps[0].error_code == "lease_storage_unavailable"
    assert report.returned_slots == 0


def test_a_success_without_the_released_lease_is_not_counted_as_returned() -> None:
    candidate = _candidate()
    unsettled = LeaseRecord(
        candidate.lease_id,
        "reserved",
        "pending",
        candidate.version,
        PhysicalResources(1, 0, 0, 0, 0, 0),
        candidate.reserved_slots,
    )
    port = _Port(
        (candidate,), {candidate.lease_id: LeaseResult("committed", candidate.lease_id, unsettled)}
    )
    report = LeaseReconciliation(port).run_once(limit=2)
    assert report.sweeps[0].decision == "unknown"
    assert report.sweeps[0].error_code == "reconciliation_required"
    assert report.returned_slots == 0


def test_dry_run_names_the_set_and_writes_nothing() -> None:
    port = _Port((_candidate(),))
    report = LeaseReconciliation(port).run_once(limit=8, dry_run=True)
    assert port.released == []
    assert report.planned == 1 and report.released == 0
    assert report.sweeps[0].decision == "planned"
    assert report.returned_slots == 0


@pytest.mark.parametrize("limit", [0, MAX_SWEEP + 1])
def test_a_pass_refuses_a_bound_outside_the_range(limit: int) -> None:
    with pytest.raises(ValueError):
        LeaseReconciliation(_Port(())).run_once(limit=limit)


def test_the_report_lines_carry_the_row_identity_and_the_returned_slots() -> None:
    candidate = _candidate()
    sweep = LeaseSweep(candidate, "released")
    line = sweep_line(sweep)
    assert line["event"] == "lease_reconciliation"
    assert (line["lease_id"], line["operation_id"], line["attempt_id"]) == (
        candidate.lease_id,
        candidate.operation_id,
        candidate.attempt_id,
    )
    assert line["decision"] == "released" and line["reserved_slots"] == 1
    summary = pass_line(LeaseReconciliationReport(8, (sweep,)))
    assert summary["event"] == "lease_reconciliation_pass"
    assert summary["released"] == 1 and summary["returned_slots"] == 1
    assert summary["limit"] == 8 and summary["candidates"] == 1


def test_a_refusal_or_unknown_outcome_makes_the_run_alarm() -> None:
    candidates = (_candidate("lease-a"), _candidate("lease-b"))
    refused = _Port(candidates, {"lease-b": rejected("lease-b", "version_conflict")})
    assert (
        run(release=LeaseReconciliation(refused), limit=2, dry_run=False, out=_Sink()) == EXIT_ALARM
    )
    clean = _Port((_candidate(),))
    assert (
        run(release=LeaseReconciliation(clean), limit=2, dry_run=False, out=_Sink()) == EXIT_CLEAN
    )


class _Sink(StringIO):
    """A sink the pass can write to; it keeps every line the pass emitted."""

    @property
    def text(self) -> str:
        return self.getvalue()


def test_the_cli_refuses_an_unbounded_pass_without_touching_the_database(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("SPEECHNEXUS_QUARANTINE_RELEASE_DATABASE_URL", raising=False)
    out = _Sink()
    assert main(["--limit", "0"], out=out) == EXIT_USAGE
    assert "--limit" in out.text


def test_the_cli_names_the_missing_variable_and_never_the_dsn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("SPEECHNEXUS_QUARANTINE_RELEASE_DATABASE_URL", raising=False)
    out = _Sink()
    assert main([], out=out) == EXIT_USAGE
    assert "SPEECHNEXUS_QUARANTINE_RELEASE_DATABASE_URL" in out.text


def test_a_port_that_returns_too_many_rows_is_a_defect_not_a_bigger_pass() -> None:
    class _Overflowing(_Port):
        def candidates(self, limit: int) -> tuple[LeaseCandidate, ...]:
            return (_candidate("lease-a"), _candidate("lease-b"))

    with pytest.raises(ValueError):
        LeaseReconciliation(_Overflowing(())).run_once(limit=1)


def test_the_sweep_carries_no_second_counter_rollback() -> None:
    """The one release implementation is the only writer of the four projections.

    A sweep that grew its own ``UPDATE`` would be a second definition of the release
    arithmetic -- the exact defect the issue's constraints forbid -- and it would
    drift from the dispatcher's release the first time either side changed. These
    two modules may read and delegate, nothing else. Only non-docstring string
    constants are judged, so prose may name the statement it refuses to write.
    """
    import speechnexus_backend.speech.infrastructure.lease_reconciliation_postgres as adapter
    import speechnexus_backend.speech.infrastructure.lease_reconciliation_sql as selection

    for module in (selection, adapter):
        for literal in _statement_literals(_source_of(module)):
            for statement in ("UPDATE ", "INSERT INTO", "DELETE FROM"):
                assert statement not in literal, f"{module.__name__} writes with {statement!r}"
    # The release is reached through the injected port, never re-implemented here.
    assert "self._releases.release_lease(" in _source_of(adapter)


def _source_of(module: ModuleType) -> str:
    """The module's own text; ``__file__`` is optional in the type system only."""
    assert module.__file__ is not None, module.__name__
    return Path(module.__file__).read_text(encoding="utf-8")


def _statement_literals(source: str) -> list[str]:
    """Every non-docstring string constant (and f-string part) in a module."""
    tree = ast.parse(source)
    docstrings = {
        id(node.body[0].value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef)
        and node.body
        and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
    }
    return [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and id(node) not in docstrings
        and isinstance(node.value, str)
    ]


def test_the_protocol_is_the_two_operations_the_pass_uses() -> None:
    # The adapter must not grow a third entry point: selection and release only.
    assert {name for name in dir(LeaseReconciliationTransactions) if not name.startswith("_")} == {
        "candidates",
        "release",
    }
