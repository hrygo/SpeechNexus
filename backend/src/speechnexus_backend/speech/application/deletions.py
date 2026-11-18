"""Deletion plan preview; the immutable plan is written before it is returned."""

from __future__ import annotations

from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import uuid4

from speechnexus_backend.speech.domain.deletion import (
    DeletionIdempotencyConflictError,
    DeletionOwnerMismatchError,
    DeletionPlan,
    DeletionPlanChangedError,
    DeletionTarget,
    DeletionTargetNotFoundError,
    Dependency,
    idempotency_key_digest,
    intent_digest,
)


@dataclass(frozen=True, slots=True)
class PlanSnapshot:
    """What the owner read under one lock: current target, dependencies, digests."""

    target: DeletionTarget
    dependencies: tuple[Dependency, ...]
    target_digest: str
    content_snapshot: dict[str, object]


@dataclass(frozen=True, slots=True)
class PlanReceipt:
    intent_digest: str
    plan: DeletionPlan


class DeletionPlanTransaction(Protocol):
    def now(self) -> datetime: ...

    def replay(self, key_digest: str) -> PlanReceipt | None: ...

    def snapshot(self, target: DeletionTarget, mode: str) -> PlanSnapshot | None: ...

    def save(
        self,
        plan: DeletionPlan,
        *,
        key_digest: str,
        intent_digest: str,
        target_digest: str,
        content_snapshot: dict[str, object],
    ) -> DeletionPlan: ...


class DeletionPlanStore(Protocol):
    def transaction(self, workspace_id: str) -> AbstractContextManager[DeletionPlanTransaction]: ...


class DeletionPlanner(Protocol):
    def preview(self, target: DeletionTarget, mode: str, idempotency_key: str) -> DeletionPlan: ...


class SpeechDeletions:
    """Owner-scoped preview service; acceptance and the ledger are not part of it."""

    def __init__(self, store: DeletionPlanStore, *, workspace_id: str) -> None:
        self._store = store
        self._workspace_id = workspace_id

    def preview(self, target: DeletionTarget, mode: str, idempotency_key: str) -> DeletionPlan:
        if target.owner != "speech":
            raise DeletionOwnerMismatchError
        key_digest = idempotency_key_digest(idempotency_key)
        intent = intent_digest(target, mode)
        with self._store.transaction(self._workspace_id) as transaction:
            receipt = transaction.replay(key_digest)
            if receipt is not None:
                # The accepted intent is replayed before any current-state check:
                # the same key returns the stored plan even after the target moved.
                if receipt.intent_digest != intent:
                    raise DeletionIdempotencyConflictError
                return receipt.plan
            snapshot = transaction.snapshot(target, mode)
            if snapshot is None:
                raise DeletionTargetNotFoundError
            if snapshot.target != target:
                raise DeletionPlanChangedError
            plan = DeletionPlan.create(
                plan_id="dp-" + uuid4().hex,
                workspace_id=self._workspace_id,
                target=target,
                mode=mode,
                dependencies=snapshot.dependencies,
                now=transaction.now(),
            )
            return transaction.save(
                plan,
                key_digest=key_digest,
                intent_digest=intent,
                target_digest=snapshot.target_digest,
                content_snapshot=snapshot.content_snapshot,
            )


__all__ = [
    "DeletionPlanStore",
    "DeletionPlanTransaction",
    "DeletionPlanner",
    "PlanReceipt",
    "PlanSnapshot",
    "SpeechDeletions",
]
