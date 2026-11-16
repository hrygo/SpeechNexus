"""Readiness aggregation behavior."""

from __future__ import annotations

import asyncio

import psycopg

from speechnexus_backend.speech.application.readiness import ReadinessService
from speechnexus_backend.speech.infrastructure.readiness import (
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
