"""Version-pinned deletion plans and atomic acceptance of owned resources."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from uuid import uuid4

from speechnexus_backend.workbench.application.deletion_ports import (
    DeletionReceipt,
    DeletionUnitOfWork,
)
from speechnexus_backend.workbench.application.versions import (
    IdempotencyConflictError,
    ResourceNotFoundError,
)
from speechnexus_backend.workbench.domain.deletion import (
    ConfirmationMismatchError,
    DeletionMode,
    DeletionOwnerMismatchError,
    DeletionPlan,
    DeletionPlanChangedError,
    DeletionRecord,
    DeletionTarget,
)


def _intent(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


class WorkbenchDeletions:
    def __init__(
        self, unit_of_work: DeletionUnitOfWork, *, digest_plan: Callable[[DeletionPlan], str]
    ) -> None:
        self._unit_of_work = unit_of_work
        self._digest = digest_plan

    async def preview(
        self,
        *,
        workspace_id: str,
        target: DeletionTarget,
        mode: DeletionMode,
        key: str,
    ) -> DeletionPlan:
        if target.owner != "workbench":
            raise DeletionOwnerMismatchError
        digest = _intent((target.sort_key, mode))
        async with self._unit_of_work.transaction() as session:
            receipt = await session.claim_receipt(workspace_id, "plan", key)
            if receipt is not None:
                if receipt.intent_digest != digest:
                    raise IdempotencyConflictError
                assert isinstance(receipt.result, DeletionPlan)
                return receipt.result
            snapshot = await session.snapshot(workspace_id, target, mode)
            if snapshot is None:
                raise ResourceNotFoundError
            if snapshot[0] != target:
                raise DeletionPlanChangedError
            plan = DeletionPlan.create(
                plan_id=uuid4().hex,
                workspace_id=workspace_id,
                target=target,
                mode=mode,
                dependencies=snapshot[1],
                now=await session.now(),
            )
            await session.save_plan(plan, self._digest(plan))
            await session.save_receipt(workspace_id, "plan", key, DeletionReceipt(digest, plan))
        return plan

    async def accept(
        self,
        *,
        workspace_id: str,
        plan_id: str,
        expected_revision: int,
        digest: str,
        targets: tuple[DeletionTarget, ...],
        key: str,
    ) -> DeletionRecord:
        if len(set(targets)) != len(targets):
            raise ConfirmationMismatchError
        intent = _intent(
            (plan_id, expected_revision, digest, sorted(target.sort_key for target in targets))
        )
        async with self._unit_of_work.transaction() as session:
            # Must precede expiry, snapshot checks, and even the plan lookup.
            receipt = await session.claim_receipt(workspace_id, "create", key)
            if receipt is not None:
                if receipt.intent_digest != intent:
                    raise IdempotencyConflictError
                assert isinstance(receipt.result, DeletionRecord)
                return receipt.result
            plan = await session.get_plan(workspace_id, plan_id)
            if plan is None:
                raise ResourceNotFoundError
            if digest != self._digest(plan):
                raise DeletionPlanChangedError
            snapshot = await session.snapshot(workspace_id, plan.target, plan.mode)
            if snapshot is None:
                raise DeletionPlanChangedError
            now = await session.now()
            plan.confirm(
                expected_revision=expected_revision,
                current_target=snapshot[0],
                current_dependencies=snapshot[1],
                targets=targets,
                now=now,
            )
            record = DeletionRecord(uuid4().hex, plan.id, digest, now)
            await session.accept(plan, record)
            await session.save_receipt(workspace_id, "create", key, DeletionReceipt(intent, record))
        return record

    async def get(self, workspace_id: str, deletion_id: str) -> DeletionRecord:
        async with self._unit_of_work.transaction() as session:
            record = await session.get_record(workspace_id, deletion_id, current=True)
            if record is None:
                raise ResourceNotFoundError
            return record
