"""Cached, single-flight warmup readiness for the VoiceDesign lane."""

import asyncio
import time
from typing import cast

import pytest

from speechnexus_gateway.application.readiness import ReadinessService
from speechnexus_gateway.application.voice_design_readiness import VoiceDesignWarmupProbe
from speechnexus_gateway.domain.voice_design import VoiceDesignSynthesizer


class SlowSynthesizer:
    def __init__(self, *, delay: float = 0.0, failures: int = 0, hang: bool = False) -> None:
        self.delay = delay
        self.failures = failures
        self.hang = hang
        self.calls = 0
        self.started = asyncio.Event()

    async def warmup(self) -> None:
        self.calls += 1
        self.started.set()
        if self.hang:
            await asyncio.Event().wait()
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.calls <= self.failures:
            raise RuntimeError("synthetic warmup rejection")


async def test_a_cold_start_longer_than_the_generic_budget_still_becomes_ready() -> None:
    synthesizer = SlowSynthesizer(delay=0.2)
    probe = VoiceDesignWarmupProbe(cast(VoiceDesignSynthesizer, synthesizer))
    service = ReadinessService([probe], timeout_seconds=0.02)

    started = time.monotonic()
    cold = await service.inspect()
    elapsed = time.monotonic() - started

    assert cold.ready is False
    assert elapsed < 0.2, "readiness must answer with the cached verdict, not block"
    assert synthesizer.calls == 1

    await asyncio.sleep(0.3)
    warm = await service.inspect()

    assert warm.ready is True
    assert synthesizer.calls == 1


async def test_concurrent_checks_share_one_warmup_synthesis() -> None:
    synthesizer = SlowSynthesizer(delay=0.05)
    probe = VoiceDesignWarmupProbe(cast(VoiceDesignSynthesizer, synthesizer))

    await asyncio.gather(*(probe.check() for _ in range(5)), return_exceptions=True)

    assert synthesizer.calls == 1
    await asyncio.sleep(0.1)
    assert probe.ready is True


async def test_a_successful_warmup_is_cached_forever() -> None:
    synthesizer = SlowSynthesizer()
    probe = VoiceDesignWarmupProbe(cast(VoiceDesignSynthesizer, synthesizer))
    with pytest.raises(RuntimeError):
        await probe.check()
    await asyncio.sleep(0.05)

    for _ in range(3):
        await probe.check()

    assert probe.ready is True
    assert synthesizer.calls == 1


async def test_a_failed_warmup_is_retried_only_after_the_interval() -> None:
    synthesizer = SlowSynthesizer(failures=1)
    probe = VoiceDesignWarmupProbe(
        cast(VoiceDesignSynthesizer, synthesizer), retry_interval_seconds=0.0
    )

    with pytest.raises(RuntimeError):
        await probe.check()
    await asyncio.sleep(0.05)
    assert synthesizer.calls == 1

    with pytest.raises(RuntimeError):
        await probe.check()
    await asyncio.sleep(0.05)
    assert synthesizer.calls == 2

    await probe.check()
    assert probe.ready is True


async def test_an_expired_warmup_budget_stays_not_ready() -> None:
    synthesizer = SlowSynthesizer(delay=5)
    probe = VoiceDesignWarmupProbe(
        cast(VoiceDesignSynthesizer, synthesizer),
        timeout_seconds=1.0,
        retry_interval_seconds=600,
    )

    started = time.monotonic()
    with pytest.raises(RuntimeError):
        await probe.check()
    assert time.monotonic() - started < 0.5

    await asyncio.sleep(1.3)
    assert probe.ready is False
    with pytest.raises(RuntimeError):
        await probe.check()
    assert synthesizer.calls == 1, "a spent budget must not be retried immediately"


async def test_close_cancels_an_in_flight_attempt() -> None:
    synthesizer = SlowSynthesizer(hang=True)
    probe = VoiceDesignWarmupProbe(cast(VoiceDesignSynthesizer, synthesizer))
    with pytest.raises(RuntimeError):
        await probe.check()
    await synthesizer.started.wait()

    await probe.aclose()

    assert probe.ready is False
    assert synthesizer.calls == 1
