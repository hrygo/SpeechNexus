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
DELETION_OBJECT = "nexus.deletion"
PLAN_TTL = timedelta(seconds=300)
INITIAL_PLAN_REVISION = 1
# The public lifecycle of one clean-up resource. Execution in this slice produces
# ``queued`` (content still awaiting object purge or resume) and ``succeeded``.
DELETION_STATUSES = ("queued", "running", "blocked", "succeeded", "failed")
CLEANUP_STATES = ("pending", "quarantined", "complete")

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


class DeletionConfirmationMismatchError(DeletionError):
    """``confirm_targets`` is not exactly the plan's confirmable scope."""

    code = "deletion_confirmation_mismatch"


class DeletionPlanExpiredError(DeletionError):
    code = "deletion_plan_expired"


class DeletionDependencyConflictError(DeletionError):
    """A ``blocks`` dependency forbids execution; re-preview after resolving it."""

    code = "deletion_dependency_conflict"


class DeletionLedgerUnavailableError(DeletionError):
    """Deletion execution storage is absent or unhealthy; retry the same key."""

    code = "deletion_unavailable"


class DeletionLedgerConfigurationError(DeletionError):
    """The route's own execution grants are missing; the rest of the API still serves."""

    code = "deletion_not_configured"


class DeletionObjectStorageUnavailableError(DeletionError):
    """Asset content cannot be confirmed deleted without the object-store identity."""

    code = "deletion_object_storage_unconfigured"


class DeletionReferenceUnsupportedError(DeletionError):
    """A live dependency this release cannot judge; never silently dropped."""

    code = "deletion_reference_unsupported"


class DeletionPlanConfigurationError(DeletionError):
    """The route's own grants are missing; the rest of the API still serves."""

    code = "deletion_plans_not_configured"


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


_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")


def digest(value: str) -> str:
    if not isinstance(value, str) or _DIGEST.fullmatch(value) is None:
        raise ValueError("Invalid deletion digest")
    return value


@dataclass(frozen=True, slots=True)
class DeletionRequest:
    """One confirmation of an issued plan.

    ``targets`` is the client's ``confirm_targets`` as sent. Order carries no
    meaning: the confirmation is compared as a set, so a reordered list names the
    same scope. A duplicate entry is not a set member twice and is rejected before
    this value is built (the contract declares ``uniqueItems``).
    """

    plan_id: str
    expected_plan_revision: int
    plan_digest: str
    targets: tuple[DeletionTarget, ...]

    def __post_init__(self) -> None:
        identifier(self.plan_id)
        revision(self.expected_plan_revision)
        digest(self.plan_digest)
        if not self.targets:
            raise ValueError("A deletion confirmation names at least one target")
        if len(set(self.targets)) != len(self.targets):
            raise ValueError("A deletion confirmation names distinct targets")


def confirmed_targets(plan: DeletionPlan) -> tuple[DeletionTarget, ...]:
    """Exactly the plan's confirmable scope, sorted by identity.

    A ``blocks`` dependency is *not* confirmable: the caller resolves it and
    re-previews. An ``independent_copy / retain`` dependency is never deleted by
    this owner. Only ``cascade`` dependencies join the target as deletable scope,
    so the confirmation cannot widen or narrow what the plan authorized.
    """
    scope = {plan.target}
    scope.update(item.target for item in plan.dependencies if item.disposition == "cascade")
    return tuple(sorted(scope, key=lambda entry: entry.sort_key))


def retained_independent_copies(plan: DeletionPlan) -> tuple[DeletionTarget, ...]:
    """Copies this owner knows it must not delete, reported instead of removed."""
    retained = {item.target for item in plan.dependencies if item.disposition == "retain"}
    return tuple(sorted(retained, key=lambda entry: entry.sort_key))


def deletion_intent_digest(request: DeletionRequest) -> str:
    """Stable identity of the request behind one Idempotency-Key."""
    document = {
        "plan_id": request.plan_id,
        "expected_plan_revision": str(request.expected_plan_revision),
        "plan_digest": request.plan_digest,
        "confirm_targets": [
            target_payload(target) for target in sorted(request.targets, key=lambda t: t.sort_key)
        ],
    }
    return hashlib.sha256(canonical_bytes(document)).hexdigest()


def confirm(plan: DeletionPlan, request: DeletionRequest, *, now: datetime) -> None:
    """Refuse any confirmation that is not exactly this plan's live scope.

    Checked in the order the contract distinguishes: revision and digest identify
    *this* plan, expiry is the plan's own window, blockers are a dependency
    conflict, and only then is the target set compared. A stale plan therefore
    reports why it is stale instead of hiding it behind a scope mismatch.
    """
    if request.expected_plan_revision != plan.revision:
        raise DeletionPlanChangedError
    if request.plan_digest != plan_digest(plan):
        raise DeletionPlanChangedError
    if now >= plan.expires_at:
        raise DeletionPlanExpiredError
    if not plan.eligible:
        raise DeletionDependencyConflictError
    if set(request.targets) != set(confirmed_targets(plan)):
        raise DeletionConfirmationMismatchError


@dataclass(frozen=True, slots=True)
class DeletionResult:
    deleted: tuple[DeletionTarget, ...]
    retained_independent_copies: tuple[DeletionTarget, ...]
    ledger_id: str
    completed_at: datetime

    def __post_init__(self) -> None:
        identifier(self.ledger_id)
        if not self.deleted:
            raise ValueError("A deletion result names at least one deleted target")
        if self.completed_at.tzinfo is None:
            raise ValueError("A deletion result requires an explicit zone")
        if set(self.deleted) & set(self.retained_independent_copies):
            raise ValueError("A retained copy is never reported as deleted")


@dataclass(frozen=True, slots=True)
class DeletionRecord:
    """Durable projection of one accepted deletion.

    ``result`` is present exactly when the content removal is confirmed; an
    interrupted execution keeps the same record with ``queued / pending`` and is
    completed by replaying the original key.
    """

    id: str
    plan_id: str
    plan_digest: str
    status: str
    cleanup_state: str
    created_at: datetime
    updated_at: datetime
    result: DeletionResult | None = None

    def __post_init__(self) -> None:
        identifier(self.id)
        identifier(self.plan_id)
        digest(self.plan_digest)
        if self.status not in DELETION_STATUSES:
            raise ValueError("Invalid deletion status")
        if self.cleanup_state not in CLEANUP_STATES:
            raise ValueError("Invalid deletion cleanup state")
        if self.created_at.tzinfo is None or self.updated_at.tzinfo is None:
            raise ValueError("Deletion timestamps require an explicit zone")
        if self.updated_at < self.created_at:
            raise ValueError("A deletion update cannot precede its creation")
        complete = self.status == "succeeded"
        if complete != (self.result is not None):
            raise ValueError("A succeeded deletion carries its result and no other status does")
        expected = "complete" if complete else None
        if expected is not None and self.cleanup_state != expected:
            raise ValueError("A succeeded deletion has completed cleanup")
        if not complete and self.cleanup_state == "complete":
            raise ValueError("Incomplete cleanup is never reported as complete")


def result_payload(result: DeletionResult) -> dict[str, object]:
    return {
        "deleted": [target_payload(target) for target in result.deleted],
        "retained_independent_copies": [
            target_payload(target) for target in result.retained_independent_copies
        ],
        "ledger_id": result.ledger_id,
        "completed_at": _time(result.completed_at),
    }


def deletion_payload(record: DeletionRecord) -> dict[str, object]:
    return {
        "id": record.id,
        "object": DELETION_OBJECT,
        "plan_id": record.plan_id,
        "plan_digest": record.plan_digest,
        "status": record.status,
        "cleanup_state": record.cleanup_state,
        "created_at": _time(record.created_at),
        "updated_at": _time(record.updated_at),
        "result": result_payload(record.result) if record.result is not None else None,
        "error": None,
    }


__all__ = [
    "CLEANUP_STATES",
    "DELETION_OBJECT",
    "DELETION_STATUSES",
    "DeletionConfirmationMismatchError",
    "DeletionDependencyConflictError",
    "DeletionError",
    "DeletionIdempotencyConflictError",
    "DeletionLedgerConfigurationError",
    "DeletionLedgerUnavailableError",
    "DeletionObjectStorageUnavailableError",
    "DeletionOwnerMismatchError",
    "DeletionPlan",
    "DeletionPlanChangedError",
    "DeletionPlanExpiredError",
    "DeletionPlanUnavailableError",
    "DeletionRecord",
    "DeletionReferenceUnsupportedError",
    "DeletionRequest",
    "DeletionResult",
    "DeletionTarget",
    "DeletionTargetNotFoundError",
    "DeletionTargetUnsupportedError",
    "Dependency",
    "canonical_bytes",
    "confirm",
    "confirmed_targets",
    "deletion_intent_digest",
    "deletion_payload",
    "dependency_snapshot",
    "digest",
    "idempotency_key_digest",
    "intent_digest",
    "plan_digest",
    "plan_payload",
    "result_payload",
    "retained_independent_copies",
    "unsigned_payload",
]
