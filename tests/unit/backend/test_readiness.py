"""Readiness aggregation behavior."""

from __future__ import annotations

import asyncio
import socket
import threading
import time

import psycopg
import pytest

from speechnexus_backend.speech.application.readiness import ReadinessService
from speechnexus_backend.speech.infrastructure.readiness import (
    _PERMANENT_DEBT,
    ABANDONED_HOLDING_CAPACITY,
    LEFT_CANCELLING_HOLDING_CAPACITY,
    CancellationDebtProbe,
    StopEvidenceDebtProbe,
    _describe_debt,
    _failure_category,
    unavailable_detail,
)
from tests.fake.readiness import DetailProbe, StaticProbe


class SlowProbe(StaticProbe):
    async def check(self) -> None:
        await asyncio.sleep(0.05)


async def test_timeout_is_reported_without_leaking_exception_text() -> None:
    service = ReadinessService([SlowProbe("slow")], timeout_seconds=0.001)

    report = await service.inspect()

    assert not report.ready
    assert report.checks[0].detail == "timed out"


async def test_a_probe_detail_reaches_the_report_without_failing_readiness() -> None:
    """#852: a stuck reservation must be named, not turned into an outage.

    `/readyz` is the deployment healthcheck, so a probe that observes capacity
    debt reports it in its detail and keeps the aggregate ready.
    """
    service = ReadinessService(
        [DetailProbe("cancellation_debt", detail="cancellation_debt: 1 accepted")],
        timeout_seconds=1,
    )

    report = await service.inspect()

    assert report.ready
    assert report.checks[0].status.value == "ok"
    assert report.checks[0].detail == "cancellation_debt: 1 accepted"


async def test_a_probe_without_a_detail_reports_none() -> None:
    service = ReadinessService([StaticProbe("postgresql")], timeout_seconds=1)

    report = await service.inspect()

    assert report.checks[0].detail is None


def test_debt_detail_is_bounded_and_names_job_and_lease() -> None:
    """The detail is the operator's non-SQL exit, so it stays short and specific."""
    from speechnexus_backend.speech.infrastructure.readiness import _describe_debt

    detail = _describe_debt(
        [
            ("job-a", "lease-1", "reserved", "executing", None, None, None),
            ("job-a", "lease-2", "stopping", None, None, None, None),
        ]
    )

    assert detail is not None
    assert detail.startswith("cancellation_debt: 1 accepted cancellation(s)")
    assert (
        "job-a -> lease-1(lease=reserved, execution=executing, subclass=execution-unknown)"
        in detail
    )
    assert "lease-2(lease=stopping, execution=not_submitted, subclass=execution-unknown)" in detail


def test_debt_detail_bounds_how_many_jobs_it_names() -> None:
    from speechnexus_backend.speech.infrastructure.readiness import _describe_debt

    detail = _describe_debt(
        [
            (f"job-{index}", f"lease-{index}", "reserved", "executing", None, None, None)
            for index in range(9)
        ]
    )

    assert detail is not None
    assert detail.count(" -> ") == 3
    assert detail.endswith("(+6 more)")
    assert len(detail) < 500


def test_a_job_with_only_accounted_leases_is_named_without_a_blocking_lease() -> None:
    from speechnexus_backend.speech.infrastructure.readiness import _describe_debt

    assert _describe_debt([]) is None
    assert _describe_debt([("job-a", None, None, None, None, None, None)]) == (
        "cancellation_debt: 1 accepted cancellation(s) still hold capacity "
        "(jobs: no-held-lease=1): "
        "job-a -> no-held-lease"
    )


def test_a_probe_failure_is_classified_so_a_real_fault_stays_visible() -> None:
    """#852: the debt probe is a diagnostic, so it reports instead of failing.

    The classification is what stops "swallow everything" from hiding a broken
    deployment: ``/readyz`` stays 200, and the detail still says whether the probe
    hit a privilege, relation, connection, timeout or other fault.
    """
    assert _failure_category(psycopg.errors.InsufficientPrivilege()) == "permission"
    assert _failure_category(psycopg.errors.UndefinedTable()) == "relation"
    assert _failure_category(TimeoutError()) == "timeout"
    assert _failure_category(psycopg.OperationalError()) == "connection"
    assert _failure_category(ConnectionRefusedError()) == "connection"
    assert _failure_category(ValueError("synthetic")) == "error"


def test_a_probe_failure_detail_names_the_category_and_never_raises() -> None:
    detail = unavailable_detail(psycopg.errors.InsufficientPrivilege())

    assert detail == "cancellation_debt: unavailable (permission)"


def test_a_degraded_debt_detail_says_which_layer_is_unreadable() -> None:
    """The Job names survive a role that may not read the lease tables."""
    detail = _describe_debt(
        [
            ("job-1", None, None, None, None, None, None),
            ("job-2", None, None, None, None, None, None),
        ],
        missing="permission",
    )

    assert detail is not None
    assert detail.startswith("cancellation_debt: 2 accepted cancellation(s)")
    assert "job-1 -> lease-unread" in detail
    assert detail.endswith("[lease detail unavailable: permission]")


def test_an_accounted_only_job_is_not_reported_as_unreadable() -> None:
    detail = _describe_debt([("job-1", None, None, None, None, None, None)])

    assert detail is not None
    assert "job-1 -> no-held-lease" in detail
    assert "unavailable" not in detail


def test_the_left_cancelling_scope_is_a_job_state_fact_beside_the_ledger_marks() -> None:
    """#852 residual: the fourth scope is counted beside, never inside, the three marks.

    The marks describe the ledger schedule; the new label describes the Job row.
    A left-cancelling Job also has a schedule fact, so it appears once under a mark
    and once under the label -- two different facts about one Job.
    """
    detail = _describe_debt(
        [
            (
                "job-left",
                "lease-left",
                "reserved",
                "executing",
                None,
                None,
                None,
                "never_claimed",
                LEFT_CANCELLING_HOLDING_CAPACITY,
            ),
            ("job-live", "lease-live", "reserved", "executing", None, None, None, "due"),
        ]
    )

    assert detail is not None
    assert detail.startswith(
        "cancellation_debt: 2 accepted cancellation(s) still hold capacity over 2 held lease(s)"
    )
    assert (
        "(held leases: execution-unknown=2; never claimed: 1; due now: 1; "
        "left-cancelling-holding-capacity: 1)" in detail
    )
    # The label is not reported as a fourth ledger mark: the raw state name never
    # leaks into the sentence, only the hyphenated Job-state label does.
    assert "never_claimed" not in detail


def test_the_left_cancelling_label_counts_jobs_not_leases() -> None:
    """One Job with two held leases is one left-cancelling Job, as with the marks."""
    detail = _describe_debt(
        [
            (
                "job-left",
                "lease-a",
                "reserved",
                "executing",
                None,
                None,
                None,
                "exhausted",
                LEFT_CANCELLING_HOLDING_CAPACITY,
            ),
            (
                "job-left",
                "lease-b",
                "reserved",
                "executing",
                None,
                None,
                None,
                "exhausted",
                LEFT_CANCELLING_HOLDING_CAPACITY,
            ),
            ("job-live", "lease-c", "reserved", "executing", None, None, None, "due"),
        ]
    )

    assert detail is not None
    assert "over 3 held lease(s)" in detail
    assert "never claimable (exhausted): 1" in detail
    assert "due now: 1" in detail
    assert "left-cancelling-holding-capacity: 1" in detail
    assert detail.count("left-cancelling-holding-capacity") == 1


def test_the_abandoned_scope_counts_only_jobs_the_bounded_reads_did_not_name() -> None:
    """#1008: the fifth label means "the age-bounded read face stopped naming this".

    The fifth read overlaps the other reads by construction -- it has neither a
    Job-state gate nor an age bound -- so the same lease can arrive twice. The first
    read to name it owns it: the held-lease unit is not inflated, and a Job the
    bounded read still names is never also called abandoned. That is what keeps the
    two labels a statement about *which* read names the Job rather than two ways of
    describing the same row.
    """
    detail = _describe_debt(
        [
            # Still inside the fourth read's window: it owns this lease.
            (
                "job-recent",
                "lease-recent",
                "reserved",
                "unknown_quarantined",
                None,
                None,
                None,
                "exhausted",
                LEFT_CANCELLING_HOLDING_CAPACITY,
            ),
            # The same lease, named again by the unbounded read: same row, same Job.
            (
                "job-recent",
                "lease-recent",
                "reserved",
                "unknown_quarantined",
                None,
                None,
                None,
                "exhausted",
                ABANDONED_HOLDING_CAPACITY,
            ),
            # Outside the window: only the unbounded read names it.
            (
                "job-old",
                "lease-old",
                "reserved",
                "unknown_quarantined",
                None,
                None,
                None,
                "exhausted",
                ABANDONED_HOLDING_CAPACITY,
            ),
        ]
    )

    assert detail is not None
    # Two Jobs, two held leases: the repeated row added neither.
    assert detail.startswith(
        "cancellation_debt: 2 accepted cancellation(s) still hold capacity over 2 held lease(s)"
    )
    assert "lease-recent(lease=reserved" in detail
    assert detail.count("lease-recent(lease=reserved") == 1
    assert "left-cancelling-holding-capacity: 1" in detail
    assert "abandoned-holding-capacity: 1" in detail
    # The mark is counted once per Job, exactly as before the fifth read existed.
    assert "never claimable (exhausted): 2" in detail


def test_the_abandoned_scope_counts_jobs_not_leases() -> None:
    """One Job with two held leases is one abandoned Job, as with every other scope."""
    detail = _describe_debt(
        [
            (
                "job-old",
                "lease-a",
                "reserved",
                "unknown_quarantined",
                None,
                None,
                None,
                "exhausted",
                ABANDONED_HOLDING_CAPACITY,
            ),
            (
                "job-old",
                "lease-b",
                "reserved",
                "unknown_quarantined",
                None,
                None,
                None,
                "exhausted",
                ABANDONED_HOLDING_CAPACITY,
            ),
        ]
    )

    assert detail is not None
    assert "over 2 held lease(s)" in detail
    assert "abandoned-holding-capacity: 1" in detail
    assert detail.count("abandoned-holding-capacity") == 1


def test_the_abandoned_label_is_not_a_ledger_mark() -> None:
    """The fifth scope joins neither the three marks nor the fourth label.

    ``abandoned`` is a statement about the read that found the Job, not about its
    schedule, so it must not be counted as one of the marks: a Job whose ledger is
    merely ``due now`` is still reported there, and the raw state name never leaks.
    """
    detail = _describe_debt(
        [
            (
                "job-old",
                "lease-old",
                "reserved",
                "unknown_quarantined",
                None,
                None,
                None,
                "due",
                ABANDONED_HOLDING_CAPACITY,
            )
        ]
    )

    assert detail is not None
    assert "due now: 1" in detail
    assert "abandoned-holding-capacity: 1" in detail
    assert "left-cancelling-holding-capacity" not in detail
    assert "due" in detail.split("(", 1)[1]


def test_the_permanent_read_takes_no_lookback_window() -> None:
    """#1008: a lookback window here is the defect, so it may not come back.

    The read's bound is migration 0061's partial index -- the ledger's open rows --
    not an age. A ``window_us`` placeholder or an ``accepted_at_us >=`` filter is how
    the 25 h shape would silently disappear again, so neither may exist. The grace
    premise is the one age condition every cancellation read shares, and it is an
    upper bound only: it says the acceptance is old enough to judge, not that it is
    recent enough to see.
    """
    assert "%(window_us)s" not in _PERMANENT_DEBT
    assert "%(now_us)s" in _PERMANENT_DEBT  # the ``due`` branch and the grace premise
    assert "accepted_at_us >=" not in _PERMANENT_DEBT
    assert "accepted_at_us <= now_clock.now_us - %(grace_us)s" in _PERMANENT_DEBT
    # And it reads the ledger's open rows with the index's own predicate.
    assert "(r.settled_at_us IS NULL OR r.last_outcome = 'exhausted')" in _PERMANENT_DEBT


def test_a_completed_execution_is_labelled_as_its_own_subclass() -> None:
    """#852 review P1: the two debts need two procedures, so they never share a label.

    A ``succeeded`` execution on a still ``reserved`` lease can never produce a
    stop, so it is the one class the operator exit closes. Reporting it as the
    generic "still holds capacity" debt would send an operator to wait for a stop
    that cannot arrive, and hiding the label would make the operator exit
    undiscoverable.
    """
    detail = _describe_debt(
        [
            (
                "job-done",
                "lease-done",
                "reserved",
                "succeeded",
                "engine_terminal_response",
                1,
                1,
            ),
            ("job-unknown", "lease-unknown", "reserved", "unknown_quarantined", None, None, None),
        ]
    )

    assert detail is not None
    assert "over 2 held lease(s)" in detail
    assert "(held leases: completed-without-release=1, execution-unknown=1)" in detail
    assert "lease-done(lease=reserved, execution=succeeded, " in detail
    assert "subclass=completed-without-release)" in detail
    assert "subclass=execution-unknown)" in detail
    # The unknown execution is never advertised as the actionable subclass.
    assert (
        "lease-unknown(lease=reserved, execution=unknown_quarantined, subclass=execution-unknown)"
        in detail
    )


def test_only_reserved_completed_leases_are_the_actionable_subclass() -> None:
    """The label follows the fact, not the intent: state decides which exit applies."""
    from speechnexus_backend.speech.infrastructure.readiness import (
        COMPLETED_WITHOUT_RELEASE,
        EXECUTION_UNKNOWN,
        debt_subclass,
    )

    def terminal(lease_state: str | None, execution_state: str | None) -> str:
        return debt_subclass(
            lease_state,
            execution_state,
            evidence_kind="engine_terminal_response",
            execution_stopped=1,
            resources_released=1,
        )

    assert terminal("reserved", "succeeded") == COMPLETED_WITHOUT_RELEASE
    # A quarantined or executing lease is not a completed-but-unreleased one even
    # when the execution ended: the quarantine path owns it.
    assert terminal("quarantined", "succeeded") == EXECUTION_UNKNOWN
    assert terminal("reserved", "executing") == EXECUTION_UNKNOWN
    assert terminal("reserved", None) == EXECUTION_UNKNOWN


def test_a_completed_execution_without_the_terminal_fact_is_not_this_subclass() -> None:
    """#852 review M2: the label may not route #338's shape into a refusing exit.

    A ``succeeded`` execution that never recorded ``engine_terminal_response`` is
    exactly what the operator exit refuses. Labelling it with that exit's name
    would send an operator to a command that can only answer "not releasable", so
    the label demands the same terminal fact the gate does.
    """
    detail = _describe_debt([("job-338", "lease-338", "reserved", "succeeded", None, None, None)])

    assert detail is not None
    assert "subclass=execution-unknown" in detail
    assert "completed-without-release" not in detail

    # An incomplete terminal row is not a terminal fact either.
    for flags in ((0, 1), (1, 0), (0, 0), (2, 1)):
        detail = _describe_debt(
            [
                (
                    "job-338",
                    "lease-338",
                    "reserved",
                    "succeeded",
                    "engine_terminal_response",
                    *flags,
                )
            ]
        )
        assert detail is not None
        assert "subclass=execution-unknown" in detail


def test_the_debt_sentence_counts_jobs_and_leases_in_two_named_units() -> None:
    """#852 review: a multi-stage Job made "3 cancellations" sit next to 4 counts.

    The sentence counts accepted cancellations (Jobs); the parenthetical counts the
    leases it classifies. Naming both is what makes the two numbers comparable.
    """
    terminal = ("engine_terminal_response", 1, 1)
    detail = _describe_debt(
        [
            ("job-a", "lease-a", "reserved", "succeeded", *terminal),
            ("job-a", "lease-b", "reserved", "succeeded", *terminal),
            ("job-b", "lease-c", "reserved", "unknown_quarantined", None, None, None),
            ("job-b", "lease-d", "reserved", "executing", None, None, None),
        ]
    )

    assert detail is not None
    assert detail.startswith(
        "cancellation_debt: 2 accepted cancellation(s) still hold capacity over 4 held lease(s)"
    )
    assert "completed-without-release=2, execution-unknown=2" in detail


class _BlackHole:
    """A TCP endpoint that accepts connections and never answers.

    It is the failure mode a hung read-only DSN produces: the socket connects, the server
    never speaks, and libpq waits. Nothing here is PostgreSQL-specific -- what the test
    needs is a connect that never completes on its own.
    """

    def __init__(self) -> None:
        self._server = socket.socket()
        self._server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server.bind(("127.0.0.1", 0))
        self._server.listen(8)
        self._server.settimeout(0.05)
        self.port: int = self._server.getsockname()[1]
        self._accepted: list[socket.socket] = []
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)

    def __enter__(self) -> _BlackHole:
        self._thread.start()
        return self

    def __exit__(self, *_: object) -> None:
        self._stop.set()
        self._thread.join(timeout=5)
        for connection in self._accepted:
            connection.close()
        self._server.close()

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                connection, _ = self._server.accept()
            except OSError, TimeoutError:
                continue
            # Held open and never read: the client's connect() stays pending.
            self._accepted.append(connection)


@pytest.mark.parametrize(
    "probe_class",
    [
        pytest.param(CancellationDebtProbe, id="cancellation_debt"),
        pytest.param(StopEvidenceDebtProbe, id="stop_evidence_debt"),
    ],
)
async def test_a_hung_probe_reports_a_timeout_and_keeps_readiness_ok(
    probe_class: type[CancellationDebtProbe] | type[StopEvidenceDebtProbe],
) -> None:
    """#917 review M4-a: a hung diagnostic must not take the API out of service.

    Both debt probes promise never to fail ``/readyz``. With the service budget and the
    probe budget equal, the service's outer timeout expired first, the probe's own handler
    never ran, and the check reported ``failed``/``timed out`` -- an outage caused by a
    diagnostic. The probe now spends a fraction of the service budget, so its own timeout
    wins and the documented ``unavailable (timeout)`` shape is what an operator sees.
    """
    with _BlackHole() as hole:
        probe = probe_class(
            f"postgresql://synthetic:synthetic@127.0.0.1:{hole.port}/synthetic",
            schema="speech",
            timeout_seconds=1.0,
        )
        report = await ReadinessService([probe], timeout_seconds=1.0).inspect()

    assert report.ready is True
    assert report.checks[0].status.value == "ok"
    assert report.checks[0].detail == f"{probe.name}: unavailable (timeout)"


async def test_a_hung_probe_does_not_delay_the_aggregate_past_the_budget() -> None:
    """The whole report still answers inside the service budget, not 1.25x of it."""
    with _BlackHole() as hole:
        probe = StopEvidenceDebtProbe(
            f"postgresql://synthetic:synthetic@127.0.0.1:{hole.port}/synthetic",
            schema="speech",
            timeout_seconds=1.0,
        )
        started = time.monotonic()
        report = await ReadinessService([probe], timeout_seconds=1.0).inspect()
        elapsed = time.monotonic() - started

    assert report.ready is True
    assert elapsed < 1.0
