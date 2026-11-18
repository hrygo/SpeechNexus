"""Immutable, version/digest-pinned speech deletion plans.

A plan is a *preview*: it names the exact target revision and every dependency
the owner can see, and it expires. It authorizes nothing -- acceptance and the
durable ledger belong to the deletion route. The frozen public shape is
``contracts/shared/deletion.schema.json``; this module owns the domain values and
the canonical digest over them, never a hand-written wire copy of that schema.

``plan_digest`` is the SHA-256 of the RFC 8785 UTF-8 canonical JSON of the plan
without its own ``plan_digest`` field (``rfc8785`` is the implementation used by
the independent contract test). The projection holds only fixed ASCII property
names, strings, booleans, arrays and objects -- no numbers and no dynamic names --
so any RFC 8785 implementation agrees on it.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import rfc8785

PLAN_OBJECT = "nexus.deletion_plan"
PLAN_TTL = timedelta(seconds=300)
INITIAL_PLAN_REVISION = 1

OWNERS = ("speech", "workbench")
SPEECH_KINDS = ("asset", "artifact", "voice_revision", "operation_content")
WORKBENCH_KINDS = ("project", "saved_asset", "transcript", "meeting", "audiobook")
MODES = ("ordinary", "cascade")
RELATIONS = (
    "persistent_reference",
    "active_execution",
    "owned_child",
    "independent_copy",
)
DISPOSITIONS = ("blocks", "cascade", "retain")

_IDENTIFIER = re.compile(r"^[a-z0-9][a-z0-9._:-]{1,126}[a-z0-9]$")
_REVISION = re.compile(r"^[1-9][0-9]{0,18}$")


class DeletionError(Exception):
    """Base for refusals whose public code the HTTP layer maps."""

    code = "deletion_request_invalid"


class DeletionOwnerMismatchError(DeletionError):
    code = "deletion_owner_mismatch"


class DeletionTargetUnsupportedError(DeletionError):
    code = "deletion_target_unsupported"


class DeletionTargetNotFoundError(DeletionError):
    code = "resource_not_found"


class DeletionPlanChangedError(DeletionError):
    code = "deletion_plan_changed"


class DeletionIdempotencyConflictError(DeletionError):
    code = "idempotency_conflict"


class DeletionPlanUnavailableError(DeletionError):
    code = "deletion_plans_unavailable"


class DeletionReferenceUnsupportedError(DeletionError):
    """A live dependency this release cannot judge; never silently dropped."""

    code = "deletion_reference_unsupported"


def identifier(value: str) -> str:
    if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
        raise ValueError("Invalid deletion identifier")
    return value


def revision(value: int) -> int:
    if type(value) is not int or not 1 <= value <= 9_999_999_999_999_999_999:
        raise ValueError("Invalid deletion revision")
    return value


@dataclass(frozen=True, slots=True)
class DeletionTarget:
    owner: str
    kind: str
    resource_id: str
    revision: int

    def __post_init__(self) -> None:
        if self.owner not in OWNERS:
            raise ValueError("Invalid deletion owner")
        if self.kind not in (*SPEECH_KINDS, *WORKBENCH_KINDS):
            raise ValueError("Invalid deletion resource kind")
        identifier(self.resource_id)
        revision(self.revision)
        allowed = SPEECH_KINDS if self.owner == "speech" else WORKBENCH_KINDS
        if self.kind not in allowed:
            raise ValueError("Deletion kind does not belong to the owner")

    @property
    def sort_key(self) -> tuple[str, str, str, int]:
        return (self.owner, self.kind, self.resource_id, self.revision)


@dataclass(frozen=True, slots=True)
class Dependency:
    target: DeletionTarget
    relation: str
    disposition: str

    def __post_init__(self) -> None:
        if self.relation not in RELATIONS:
            raise ValueError("Invalid dependency relation")
        if self.disposition not in DISPOSITIONS:
            raise ValueError("Invalid dependency disposition")
        if self.relation == "independent_copy" and self.disposition != "retain":
            raise ValueError("An independent copy is retained, never deleted")
        if self.relation == "active_execution" and self.disposition == "retain":
            raise ValueError("An active execution blocks or cascades")

    @property
    def sort_key(self) -> tuple[str, str, str, int, str]:
        return (*self.target.sort_key, self.relation)


def dependency_snapshot(
    dependencies: tuple[Dependency, ...], *, mode: str
) -> tuple[Dependency, ...]:
    """Sort, de-duplicate and normalize a dependency set for one plan mode.

    ``(owner, kind, id, revision, relation)`` is unique; one relation cannot be
    carried twice with different dispositions. ``ordinary`` mode cannot cascade,
    so an owned child that would cascade is reported as a blocker instead: the
    caller must re-preview with ``cascade``.
    """
    if mode not in MODES:
        raise ValueError("Invalid deletion mode")
    ordered: list[Dependency] = []
    seen: dict[tuple[str, str, str, int, str], str] = {}
    for item in sorted(dependencies, key=lambda entry: entry.sort_key):
        disposition = item.disposition
        if mode == "ordinary" and disposition == "cascade":
            disposition = "blocks"
        key = item.sort_key
        if key in seen:
            if seen[key] != disposition:
                raise ValueError("One relation carries two dispositions")
            continue
        seen[key] = disposition
        ordered.append(Dependency(item.target, item.relation, disposition))
    return tuple(ordered)


def _time(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("Deletion timestamps require an explicit zone")
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def target_payload(target: DeletionTarget) -> dict[str, object]:
    return {
        "owner": target.owner,
        "kind": target.kind,
        "resource": {"id": target.resource_id, "revision": str(target.revision)},
    }


def dependency_payload(dependency: Dependency) -> dict[str, object]:
    return {
        "target": target_payload(dependency.target),
        "relation": dependency.relation,
        "disposition": dependency.disposition,
    }


@dataclass(frozen=True, slots=True)
class DeletionPlan:
    id: str
    workspace_id: str
    target: DeletionTarget
    mode: str
    revision: int
    created_at: datetime
    expires_at: datetime
    dependencies: tuple[Dependency, ...]

    def __post_init__(self) -> None:
        identifier(self.id)
        identifier(self.workspace_id)
        if self.mode not in MODES:
            raise ValueError("Invalid deletion mode")
        revision(self.revision)
        if self.expires_at <= self.created_at:
            raise ValueError("A deletion plan must expire after it was created")
        if self.expires_at - self.created_at > PLAN_TTL:
            raise ValueError("A deletion plan may not outlive its fixed window")
        expected = dependency_snapshot(self.dependencies, mode=self.mode)
        if expected != self.dependencies:
            raise ValueError("Deletion plan dependencies are not in canonical order")

    @property
    def eligible(self) -> bool:
        return not any(item.disposition == "blocks" for item in self.dependencies)

    @classmethod
    def create(
        cls,
        *,
        plan_id: str,
        workspace_id: str,
        target: DeletionTarget,
        mode: str,
        dependencies: tuple[Dependency, ...],
        now: datetime,
    ) -> DeletionPlan:
        snapshot = dependency_snapshot(dependencies, mode=mode)
        return cls(
            id=plan_id,
            workspace_id=workspace_id,
            target=target,
            mode=mode,
            revision=INITIAL_PLAN_REVISION,
            created_at=now,
            expires_at=now + PLAN_TTL,
            dependencies=snapshot,
        )


def unsigned_payload(plan: DeletionPlan) -> dict[str, object]:
    """The plan exactly as it is hashed; ``plan_digest`` is added afterwards."""
    return {
        "id": plan.id,
        "object": PLAN_OBJECT,
        "target": target_payload(plan.target),
        "mode": plan.mode,
        "plan_revision": str(plan.revision),
        "created_at": _time(plan.created_at),
        "expires_at": _time(plan.expires_at),
        "dependencies": [dependency_payload(item) for item in plan.dependencies],
        "eligible": plan.eligible,
    }


def canonical_bytes(value: Any) -> bytes:
    return rfc8785.dumps(value)


def plan_digest(plan: DeletionPlan) -> str:
    return "sha256:" + hashlib.sha256(canonical_bytes(unsigned_payload(plan))).hexdigest()


def plan_payload(plan: DeletionPlan) -> dict[str, object]:
    return {**unsigned_payload(plan), "plan_digest": plan_digest(plan)}


def intent_digest(target: DeletionTarget, mode: str) -> str:
    """Stable identity of the request behind one Idempotency-Key."""
    if mode not in MODES:
        raise ValueError("Invalid deletion mode")
    document: dict[str, object] = {"mode": mode, "target": target_payload(target)}
    return hashlib.sha256(canonical_bytes(document)).hexdigest()


def idempotency_key_digest(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


__all__ = [
    "DeletionError",
    "DeletionIdempotencyConflictError",
    "DeletionOwnerMismatchError",
    "DeletionPlan",
    "DeletionPlanChangedError",
    "DeletionPlanUnavailableError",
    "DeletionReferenceUnsupportedError",
    "DeletionTarget",
    "DeletionTargetNotFoundError",
    "DeletionTargetUnsupportedError",
    "Dependency",
    "canonical_bytes",
    "dependency_snapshot",
    "idempotency_key_digest",
    "intent_digest",
    "plan_digest",
    "plan_payload",
    "unsigned_payload",
]
