"""Settle generating render units from the public speech Job outcome.

The reconciler never invents a result: an unreadable Job receipt leaves the unit
generating, and a settled attempt is recorded exactly once through the store's
generation guard.
"""

from __future__ import annotations

from contextlib import suppress
from dataclasses import dataclass
from typing import Literal, Protocol

from speechnexus_backend.workbench.application.render_copies import RenderResultCopyPort
from speechnexus_backend.workbench.application.versions import ResourceNotFoundError
from speechnexus_backend.workbench.domain.render_units import (
    RenderAttemptConflictError,
    RenderSource,
    RenderUnit,
)

type RenderJobState = Literal["pending", "succeeded", "failed", "expired", "cancelled"]


class RenderSettlementStore(Protocol):
    async def generating_units(
        self, *, workspace_id: str, limit: int
    ) -> tuple[RenderUnit, ...]: ...

    async def complete_unit(
        self,
        *,
        unit_id: str,
        workspace_id: str,
        generation_id: str,
        source: RenderSource,
    ) -> RenderUnit: ...

    async def fail_unit(
        self, *, unit_id: str, workspace_id: str, generation_id: str
    ) -> RenderUnit: ...


class RenderJobReadError(Exception):
    """The Job receipt is unreadable or unavailable; the attempt stays unresolved."""


@dataclass(frozen=True, slots=True)
class RenderJobOutcome:
    """The bounded facts a render settlement needs from one speech Job."""

    state: RenderJobState
    artifact_id: str | None = None
    receipt_id: str | None = None


class RenderJobReader(Protocol):
    async def read_job(self, *, job_id: str) -> RenderJobOutcome: ...


@dataclass(frozen=True, slots=True)
class SettledAttempt:
    unit_id: str
    state: str


class RenderReconciler:
    """Apply terminal Job outcomes to the units that dispatched them."""

    def __init__(
        self,
        units: RenderSettlementStore,
        jobs: RenderJobReader,
        copies: RenderResultCopyPort | None = None,
    ) -> None:
        self._units = units
        self._jobs = jobs
        self._copies = copies

    async def settle(self, *, workspace_id: str, limit: int = 20) -> tuple[SettledAttempt, ...]:
        settled: list[SettledAttempt] = []
        for unit in await self._units.generating_units(workspace_id=workspace_id, limit=limit):
            generation = unit.generation
            if generation is None:
                continue
            try:
                outcome = await self._jobs.read_job(job_id=generation.job_id)
            except RenderJobReadError:
                continue
            try:
                if outcome.state == "succeeded":
                    if outcome.artifact_id is None or outcome.receipt_id is None:
                        continue
                    source = RenderSource(
                        unit.id,
                        generation.job_id,
                        outcome.artifact_id,
                        outcome.receipt_id,
                        unit.dependencies.sha256,
                    )
                    await self._units.complete_unit(
                        unit_id=unit.id,
                        workspace_id=workspace_id,
                        generation_id=generation.id,
                        source=source,
                    )
                    if self._copies is not None:
                        # The unit is already ready; a copy failure is reported by the
                        # page's explicit save instead of un-settling the unit.
                        with suppress(Exception):
                            await self._copies.save_copy(
                                workspace_id=workspace_id, unit=unit, source=source
                            )
                elif outcome.state in {"failed", "expired", "cancelled"}:
                    await self._units.fail_unit(
                        unit_id=unit.id,
                        workspace_id=workspace_id,
                        generation_id=generation.id,
                    )
                else:
                    continue
            except RenderAttemptConflictError, ResourceNotFoundError:
                # Another pass already settled this attempt, or the unit disappeared.
                continue
            settled.append(SettledAttempt(unit.id, outcome.state))
        return tuple(settled)
