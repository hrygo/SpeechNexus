"""Readiness warmup caching keeps a settled failure reason instead of erasing it."""

import asyncio

import pytest

from speechnexus_gateway.application.engine_warmup import CachedWarmupProbe


class FailingWarmup:
    def __init__(self) -> None:
        self.calls = 0

    async def warmup(self) -> None:
        self.calls += 1
        raise RuntimeError("synthetic cold-start failure")


async def _settle(probe: CachedWarmupProbe) -> None:
    task = probe._task
    assert task is not None
    await asyncio.wait({task}, timeout=1.0)


async def test_a_settled_failure_keeps_its_reason_and_does_not_retry_early() -> None:
    warmup = FailingWarmup()
    probe = CachedWarmupProbe(warmup, retry_interval_seconds=60.0)

    probe.warm()
    await _settle(probe)
    assert not probe.ready

    with pytest.raises(RuntimeError) as raised:
        await probe.check()
    assert "RuntimeError" in str(raised.value)
    assert "no attempt" not in str(raised.value)
    # The retry interval still applies: a failed attempt is not restarted by a
    # later readiness read or health probe.
    for _ in range(2):
        with pytest.raises(RuntimeError):
            await probe.check()
    assert warmup.calls == 1

    await probe.aclose()
