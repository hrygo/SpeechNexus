"""Bounded readiness checks for the execution gateway."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

logger = logging.getLogger(__name__)


class ProbeStatus(StrEnum):
    OK = "ok"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class ProbeResult:
    name: str
    status: ProbeStatus
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class ReadinessReport:
    checks: tuple[ProbeResult, ...]

    @property
    def ready(self) -> bool:
        return bool(self.checks) and all(check.status is ProbeStatus.OK for check in self.checks)


class ReadinessProbe(Protocol):
    @property
    def name(self) -> str: ...

    async def check(self) -> None: ...


class ReadinessService:
    def __init__(self, probes: Sequence[ReadinessProbe], *, timeout_seconds: float) -> None:
        self._probes = tuple(probes)
        self._timeout_seconds = timeout_seconds

    async def inspect(self) -> ReadinessReport:
        results = await asyncio.gather(*(self._inspect_probe(probe) for probe in self._probes))
        return ReadinessReport(checks=tuple(results))

    async def _inspect_probe(self, probe: ReadinessProbe) -> ProbeResult:
        try:
            async with asyncio.timeout(self._timeout_seconds):
                await probe.check()
        except TimeoutError:
            logger.warning("readiness probe timed out", extra={"probe": probe.name})
            return ProbeResult(name=probe.name, status=ProbeStatus.FAILED, detail="timed out")
        except Exception as error:
            logger.warning(
                "readiness probe failed",
                extra={"probe": probe.name, "exception_type": type(error).__name__},
            )
            return ProbeResult(name=probe.name, status=ProbeStatus.FAILED, detail="unavailable")
        return ProbeResult(name=probe.name, status=ProbeStatus.OK)
