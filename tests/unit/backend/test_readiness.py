"""Readiness aggregation behavior."""

from __future__ import annotations

import asyncio

from speechnexus_backend.speech.application.readiness import ReadinessService
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
        [("job-a", "lease-1", "reserved", "executing"), ("job-a", "lease-2", "stopping", None)]
    )

    assert detail is not None
    assert detail.startswith("cancellation_debt: 1 accepted cancellation(s)")
    assert "job-a -> lease-1(lease=reserved, execution=executing)" in detail
    assert "lease-2(lease=stopping, execution=not_submitted)" in detail


def test_debt_detail_bounds_how_many_jobs_it_names() -> None:
    from speechnexus_backend.speech.infrastructure.readiness import _describe_debt

    detail = _describe_debt(
        [(f"job-{index}", f"lease-{index}", "reserved", "executing") for index in range(9)]
    )

    assert detail is not None
    assert detail.count(" -> ") == 3
    assert detail.endswith("(+6 more)")
    assert len(detail) < 400


def test_a_job_with_only_accounted_leases_is_named_without_a_blocking_lease() -> None:
    from speechnexus_backend.speech.infrastructure.readiness import _describe_debt

    assert _describe_debt([]) is None
    assert _describe_debt([("job-a", None, None, None)]) == (
        "cancellation_debt: 1 accepted cancellation(s) still hold capacity: job-a -> no-held-lease"
    )
