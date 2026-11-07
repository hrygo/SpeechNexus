"""Cached, single-flight engine warmup readiness shared by resident speech lanes.

An HTTP 200 from ``/health`` only proves that something listens; the verified
vLLM-Omni process answers health long before image capture finished and its
first real request is much slower. A lane therefore reports ready only after one
real bounded synthesis returned a valid answer.

The synthesis runs in the background, so a readiness request always answers with
the last settled verdict instead of blocking for a cold start. Concurrent
readiness checks share one attempt, a successful attempt is cached forever, and
a failed one is not retried before the configured interval. The generic
readiness timeout therefore never bounds the warmup itself.
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from collections.abc import Callable
from contextlib import suppress
from typing import Protocol

logger = logging.getLogger(__name__)

DEFAULT_WARMUP_TIMEOUT_SECONDS = 330.0
DEFAULT_RETRY_INTERVAL_SECONDS = 30.0
MAX_WARMUP_TIMEOUT_SECONDS = 1800.0


class WarmupSynthesizer(Protocol):
    """One lane-specific real synthesis; the probe owns caching and retry only."""

    async def warmup(self) -> None: ...


class CachedWarmupProbe:
    """One cached warmup synthesis per gateway process; a health answer never passes."""

    name = "engine_warmup"

    def __init__(
        self,
        synthesizer: WarmupSynthesizer,
        *,
        timeout_seconds: float = DEFAULT_WARMUP_TIMEOUT_SECONDS,
        retry_interval_seconds: float = DEFAULT_RETRY_INTERVAL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if (
            type(timeout_seconds) not in (int, float)
            or not math.isfinite(timeout_seconds)
            or not 1.0 <= timeout_seconds <= MAX_WARMUP_TIMEOUT_SECONDS
        ):
            raise ValueError("invalid engine warmup budget")
        if (
            type(retry_interval_seconds) not in (int, float)
            or not math.isfinite(retry_interval_seconds)
            or not 0.0 <= retry_interval_seconds <= 600.0
        ):
            raise ValueError("invalid engine warmup retry interval")
        self._synthesizer = synthesizer
        self._timeout_seconds = float(timeout_seconds)
        self._retry_interval_seconds = float(retry_interval_seconds)
        self._clock = clock
        self._task: asyncio.Task[None] | None = None
        self._ready = False
        self._attempt_started_at: float | None = None
        self._last_failure: BaseException | None = None

    @property
    def ready(self) -> bool:
        # The verdict is cached for this process only. A restarted gateway process
        # (including a restarted engine container) starts a fresh warmup attempt.
        return self._ready

    def warm(self) -> None:
        """Start the cached attempt from the application loop during startup."""
        if self._ready:
            return
        self._start_if_due()

    async def check(self) -> None:
        """Answer from the cached verdict; a started task cannot have finished yet."""
        if self.ready:
            return
        self._start_if_due()
        failure = self._last_failure
        detail = "no attempt" if failure is None else type(failure).__name__
        raise RuntimeError(f"{self.name} warmup has not completed ({detail})")

    async def aclose(self) -> None:
        """Cancel an in-flight attempt; a completed attempt keeps its verdict."""
        task = self._task
        self._task = None
        if task is not None and not task.done():
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

    def _start_if_due(self) -> None:
        task = self._task
        if task is not None and not task.done():
            return
        if task is not None:
            if not task.cancelled():
                failure = task.exception()
                if failure is not None:
                    self._last_failure = failure
            self._task = None
        now = self._clock()
        if (
            self._attempt_started_at is not None
            and now - self._attempt_started_at < self._retry_interval_seconds
        ):
            return
        self._attempt_started_at = now
        self._task = asyncio.create_task(self._warm())

    async def _warm(self) -> None:
        try:
            async with asyncio.timeout(self._timeout_seconds):
                await self._synthesizer.warmup()
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self._last_failure = error
            logger.warning(
                "engine warmup failed",
                extra={"probe": self.name, "exception_type": type(error).__name__},
            )
        else:
            self._ready = True
