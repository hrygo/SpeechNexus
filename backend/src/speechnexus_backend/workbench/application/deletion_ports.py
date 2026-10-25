"""Atomic workbench deletion acceptance; no object I/O inside this boundary."""

from __future__ import annotations

from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Protocol

from speechnexus_backend.workbench.domain.deletion import (
    DeletionDependency,
    DeletionMode,
    DeletionPlan,
    DeletionRecord,
    DeletionTarget,
)

type DeletionFamily = Literal["plan", "create"]


@dataclass(frozen=True, slots=True)
class DeletionReceipt:
    intent_digest: str
    result: DeletionPlan | DeletionRecord


class DeletionSession(Protocol):
    async def now(self) -> datetime: ...

    async def snapshot(
        self,
        workspace_id: str,
        target: DeletionTarget,
        mode: DeletionMode,
    ) -> tuple[DeletionTarget, tuple[DeletionDependency, ...]] | None:
        """Lock target and complete dependency set until commit, including new references."""
        ...

    async def get_plan(self, workspace_id: str, plan_id: str) -> DeletionPlan | None: ...

    async def save_plan(self, plan: DeletionPlan, digest: str) -> None: ...

    async def get_record(
        self,
        workspace_id: str,
        deletion_id: str,
        *,
        current: bool = False,
    ) -> DeletionRecord | None:
        """Replay accepted identity; current queries also load completion evidence."""
        ...

    async def accept(self, plan: DeletionPlan, record: DeletionRecord) -> None:
        """Tombstones, immutable deletion identity and outbox commit together."""
        ...

    async def claim_receipt(
        self,
        workspace_id: str,
        family: DeletionFamily,
        key: str,
    ) -> DeletionReceipt | None: ...

    async def save_receipt(
        self,
        workspace_id: str,
        family: DeletionFamily,
        key: str,
        receipt: DeletionReceipt,
    ) -> None: ...


class DeletionUnitOfWork(Protocol):
    def transaction(self) -> AbstractAsyncContextManager[DeletionSession]: ...
