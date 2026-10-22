"""Readiness aggregation behavior."""

from __future__ import annotations

import asyncio

from speechnexus_backend.speech.application.readiness import ReadinessService
from tests.fake.readiness import StaticProbe


class SlowProbe(StaticProbe):
    async def check(self) -> None:
        await asyncio.sleep(0.05)


async def test_timeout_is_reported_without_leaking_exception_text() -> None:
    service = ReadinessService([SlowProbe("slow")], timeout_seconds=0.001)

    report = await service.inspect()

    assert not report.ready
    assert report.checks[0].detail == "timed out"
