"""Owner-scoped deletion decisions, separate from transport and cleanup effects."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Literal

type DeletionMode = Literal["ordinary", "cascade"]
type ResourceOwner = Literal["speech", "workbench"]
type ResourceKind = Literal[
    "asset",
    "artifact",
    "voice_revision",
    "operation_content",
    "project",
    "saved_asset",
    "transcript",
    "meeting",
    "audiobook",
]
type DependencyRelation = Literal[
    "persistent_reference", "active_execution", "owned_child", "independent_copy"
]
type DependencyDisposition = Literal["blocks", "cascade", "retain"]


class DeletionPlanChangedError(Exception):
    def __init__(self) -> None:
        super().__init__("deletion plan changed")


class DeletionPlanExpiredError(Exception):
    def __init__(self) -> None:
        super().__init__("deletion plan expired")


class DeletionDependencyConflictError(Exception):
    def __init__(self) -> None:
        super().__init__("deletion dependency conflict")


class ConfirmationMismatchError(Exception):
    def __init__(self) -> None:
        super().__init__("deletion confirmation mismatch")


class DeletionOwnerMismatchError(Exception):
    def __init__(self) -> None:
        super().__init__("deletion owner mismatch")


@dataclass(frozen=True, slots=True)
class DeletionTarget:
    owner: ResourceOwner
    kind: ResourceKind
    id: str
    revision: int

    def __post_init__(self) -> None:
        speech = self.kind in {"asset", "artifact", "voice_revision", "operation_content"}
        if (self.owner == "speech") != speech:
            raise DeletionOwnerMismatchError
        if not 1 <= self.revision <= 10**19 - 1:
            raise ValueError("invalid deletion resource revision")

    @property
    def sort_key(self) -> tuple[str, str, str, str]:
        return self.owner, self.kind, self.id, str(self.revision)


@dataclass(frozen=True, slots=True)
class DeletionDependency:
    target: DeletionTarget
    relation: DependencyRelation
    disposition: DependencyDisposition


def dependency_snapshot(
    dependencies: tuple[DeletionDependency, ...],
    *,
    mode: DeletionMode,
) -> tuple[DeletionDependency, ...]:
    """Conservative policy: foreign references block and independent copies survive."""
    seen: set[tuple[DeletionTarget, DependencyRelation]] = set()
    result: list[DeletionDependency] = []
    for item in dependencies:
        identity = item.target, item.relation
        if identity in seen:
            raise ValueError("duplicate deletion dependency relation")
        seen.add(identity)
        if item.relation == "independent_copy":
            disposition: DependencyDisposition = "retain"
        elif mode == "ordinary" or item.target.owner != "workbench":
            disposition = "blocks"
        else:
            disposition = item.disposition
        result.append(replace(item, disposition=disposition))
    return tuple(sorted(result, key=lambda item: (*item.target.sort_key, item.relation)))


@dataclass(frozen=True, slots=True)
class DeletionPlan:
    id: str
    workspace_id: str
    target: DeletionTarget
    mode: DeletionMode
    revision: int
    created_at: datetime
    expires_at: datetime
    dependencies: tuple[DeletionDependency, ...]

    @classmethod
    def create(
        cls,
        *,
        plan_id: str,
        workspace_id: str,
        target: DeletionTarget,
        mode: DeletionMode,
        now: datetime,
        dependencies: tuple[DeletionDependency, ...],
    ) -> DeletionPlan:
        if target.owner != "workbench":
            raise DeletionOwnerMismatchError
        if mode not in {"ordinary", "cascade"} or now.utcoffset() is None:
            raise ValueError("invalid deletion plan mode or time")
        now = now.astimezone(UTC)
        return cls(
            plan_id,
            workspace_id,
            target,
            mode,
            1,
            now,
            now + timedelta(seconds=300),
            dependency_snapshot(dependencies, mode=mode),
        )

    @property
    def eligible(self) -> bool:
        return all(item.disposition != "blocks" for item in self.dependencies)

    @property
    def confirmed_targets(self) -> tuple[DeletionTarget, ...]:
        return tuple(
            sorted(
                {
                    self.target,
                    *(item.target for item in self.dependencies if item.disposition == "cascade"),
                },
                key=lambda target: target.sort_key,
            )
        )

    def confirm(
        self,
        *,
        expected_revision: int,
        current_target: DeletionTarget,
        current_dependencies: tuple[DeletionDependency, ...],
        targets: tuple[DeletionTarget, ...],
        now: datetime,
    ) -> None:
        if expected_revision != self.revision:
            raise DeletionPlanChangedError
        if now.utcoffset() is None or now < self.created_at:
            raise ValueError("deletion time must be aware and monotonic")
        if now >= self.expires_at:
            raise DeletionPlanExpiredError
        if (
            current_target != self.target
            or dependency_snapshot(
                current_dependencies,
                mode=self.mode,
            )
            != self.dependencies
        ):
            raise DeletionPlanChangedError
        if not self.eligible:
            raise DeletionDependencyConflictError
        if len(set(targets)) != len(targets) or set(targets) != set(self.confirmed_targets):
            raise ConfirmationMismatchError


@dataclass(frozen=True, slots=True)
class DeletionResult:
    deleted: tuple[DeletionTarget, ...]
    retained_independent_copies: tuple[DeletionTarget, ...]
    ledger_id: str
    completed_at: datetime


@dataclass(frozen=True, slots=True)
class DeletionRecord:
    """Accepted cleanup intent. It is queued until a worker supplies cleanup evidence."""

    id: str
    plan_id: str
    plan_digest: str
    created_at: datetime
    result: DeletionResult | None = None
