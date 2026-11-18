"""Deletion preview and execution over one owner-scoped workspace.

``preview`` writes the immutable plan before returning it. ``execute`` re-validates
that plan, removes the confirmed content and returns the durable ledger record; the
only step outside a database transaction is the asset object purge, which the
ledger's queued row makes resumable under the same ``Idempotency-Key``.
"""

from __future__ import annotations

from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import uuid4

from speechnexus_backend.speech.domain.assets import AssetError, VerifiedObject
from speechnexus_backend.speech.domain.deletion import (
    DeletionIdempotencyConflictError,
    DeletionLedgerUnavailableError,
    DeletionOwnerMismatchError,
    DeletionPlan,
    DeletionPlanChangedError,
    DeletionRecord,
    DeletionRequest,
    DeletionTarget,
    DeletionTargetNotFoundError,
    Dependency,
    deletion_intent_digest,
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


@dataclass(frozen=True, slots=True)
class AcceptedDeletion:
    """One accepted execution plus the object version still awaiting its purge."""

    record: DeletionRecord
    pending_object: VerifiedObject | None


class DeletionExecutionStore(Protocol):
    def accept(
        self,
        request: DeletionRequest,
        *,
        key_digest: str,
        intent_digest: str,
        purge_available: bool,
        workspace_id: str,
    ) -> AcceptedDeletion: ...

    def complete(
        self, deletion_id: str, *, key_digest: str, workspace_id: str
    ) -> DeletionRecord: ...


class DeletionObjectPurge(Protocol):
    """The asset object-store step; object I/O never runs inside a transaction."""

    def delete_version(self, proof: VerifiedObject) -> None: ...


class DeletionPlanner(Protocol):
    def preview(self, target: DeletionTarget, mode: str, idempotency_key: str) -> DeletionPlan: ...

    def execute(self, request: DeletionRequest, idempotency_key: str) -> DeletionRecord: ...


class SpeechDeletions:
    """Owner-scoped preview and execution service."""

    def __init__(
        self,
        store: DeletionPlanStore,
        *,
        workspace_id: str,
        executions: DeletionExecutionStore | None = None,
        purge: DeletionObjectPurge | None = None,
    ) -> None:
        self._store = store
        self._workspace_id = workspace_id
        self._executions = executions
        self._purge = purge

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

    def execute(self, request: DeletionRequest, idempotency_key: str) -> DeletionRecord:
        """Execute the confirmed scope, or resume the row the same key accepted."""
        for target in request.targets:
            if target.owner != "speech":
                # This route owns speech content only; a workbench plan is not a
                # speech resource even when the identifier exists here.
                raise DeletionOwnerMismatchError
        if self._executions is None:
            raise DeletionLedgerUnavailableError("deletion execution is not configured")
        accepted = self._executions.accept(
            request,
            key_digest=idempotency_key_digest(idempotency_key),
            intent_digest=deletion_intent_digest(request),
            purge_available=self._purge is not None,
            workspace_id=self._workspace_id,
        )
        if accepted.pending_object is None:
            return accepted.record
        if self._purge is None:
            # Accepted and durable, but the object identity is not configured: the
            # record stays queued instead of claiming a purge nobody performed.
            return accepted.record
        try:
            self._purge.delete_version(accepted.pending_object)
        except AssetError:
            # The ledger keeps the deletion queued; the same key resumes it. Never
            # report success without a confirmed object purge.
            return accepted.record
        return self._executions.complete(
            accepted.record.id,
            key_digest=idempotency_key_digest(idempotency_key),
            workspace_id=self._workspace_id,
        )


__all__ = [
    "AcceptedDeletion",
    "DeletionExecutionStore",
    "DeletionObjectPurge",
    "DeletionPlanStore",
    "DeletionPlanTransaction",
    "DeletionPlanner",
    "PlanReceipt",
    "PlanSnapshot",
    "SpeechDeletions",
]
