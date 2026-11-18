"""Deletion preview behaviour: owner scope, idempotency, frozen digest, HTTP mapping.

The store here is the same transaction protocol the PostgreSQL adapter implements,
so the idempotency decision under test is the production one. The PostgreSQL module
pins the statements on a real database; this module pins the decisions.
"""

from __future__ import annotations

import copy
import hashlib
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
import rfc8785

from speechnexus_backend.app import create_app
from speechnexus_backend.speech.application.deletions import (
    PlanReceipt,
    PlanSnapshot,
    SpeechDeletions,
)
from speechnexus_backend.speech.domain.deletion import (
    DeletionIdempotencyConflictError,
    DeletionOwnerMismatchError,
    DeletionPlan,
    DeletionPlanChangedError,
    DeletionTarget,
    DeletionTargetNotFoundError,
    DeletionTargetUnsupportedError,
    Dependency,
    plan_digest,
    plan_payload,
)
from tests.contract.test_architecture_repairs import _deletion_validator
from tests.unit.backend.test_health import _settings

WORKSPACE = "workspace-default"
NOW = datetime(2026, 11, 19, 3, 0, 0, tzinfo=UTC)
ASSET = DeletionTarget("speech", "asset", "asset-plan-1", 3)
JOB = DeletionTarget("speech", "operation_content", "job-plan-1", 4)


def snapshot(target: DeletionTarget, dependencies: tuple[Dependency, ...] = ()) -> PlanSnapshot:
    return PlanSnapshot(
        target=target,
        dependencies=dependencies,
        target_digest="sha256:" + "a" * 64,
        content_snapshot={"synthetic": True},
    )


class Transaction:
    def __init__(self, store: Store) -> None:
        self._store = store

    def now(self) -> datetime:
        return NOW

    def replay(self, key_digest: str) -> PlanReceipt | None:
        stored = self._store.plans.get(key_digest)
        return None if stored is None else PlanReceipt(stored[0], stored[1])

    def snapshot(self, target: DeletionTarget, mode: str) -> PlanSnapshot | None:
        if target.kind not in {"asset", "operation_content"}:
            raise DeletionTargetUnsupportedError
        return self._store.snapshots.get(target.sort_key[:3])

    def save(
        self,
        plan: DeletionPlan,
        *,
        key_digest: str,
        intent_digest: str,
        target_digest: str,
        content_snapshot: dict[str, object],
    ) -> DeletionPlan:
        assert target_digest.startswith("sha256:")
        assert content_snapshot
        self._store.plans[key_digest] = (intent_digest, plan)
        return plan


class Store:
    def __init__(self, *snapshots: PlanSnapshot) -> None:
        # Identity is owner/kind/id; the requested revision is compared, not used
        # to find the row, exactly like the locked PostgreSQL snapshot.
        self.snapshots = {item.target.sort_key[:3]: item for item in snapshots}
        self.plans: dict[str, tuple[str, DeletionPlan]] = {}

    @contextmanager
    def transaction(self, workspace_id: str) -> Iterator[Transaction]:
        assert workspace_id == WORKSPACE
        yield Transaction(self)


def service(*snapshots: PlanSnapshot) -> SpeechDeletions:
    return SpeechDeletions(Store(*snapshots), workspace_id=WORKSPACE)


def reference() -> Dependency:
    return Dependency(
        DeletionTarget("speech", "voice_revision", "voice-plan-1", 2),
        "persistent_reference",
        "blocks",
    )


def test_same_key_same_request_replays_the_stored_plan() -> None:
    deletions = service(snapshot(ASSET, (reference(),)))
    first = deletions.preview(ASSET, "ordinary", "key-plan-1")
    second = deletions.preview(ASSET, "ordinary", "key-plan-1")
    assert second == first
    assert second.id == first.id
    assert second.revision == first.revision == 1


def test_same_key_different_request_is_a_conflict() -> None:
    deletions = service(snapshot(ASSET, (reference(),)))
    deletions.preview(ASSET, "ordinary", "key-plan-2")
    with pytest.raises(DeletionIdempotencyConflictError):
        deletions.preview(ASSET, "cascade", "key-plan-2")
    with pytest.raises(DeletionIdempotencyConflictError):
        deletions.preview(
            DeletionTarget("speech", "asset", "asset-plan-2", 1), "ordinary", "key-plan-2"
        )


def test_missing_target_is_not_found() -> None:
    with pytest.raises(DeletionTargetNotFoundError):
        service().preview(ASSET, "ordinary", "key-plan-3")


def test_a_workbench_owned_target_is_an_owner_mismatch() -> None:
    target = DeletionTarget("workbench", "project", "project-plan-1", 1)
    with pytest.raises(DeletionOwnerMismatchError):
        service(snapshot(target)).preview(target, "ordinary", "key-plan-4")


def test_a_stale_revision_is_a_changed_plan() -> None:
    stale = DeletionTarget("speech", "asset", "asset-plan-1", 2)
    with pytest.raises(DeletionPlanChangedError):
        service(snapshot(ASSET)).preview(stale, "ordinary", "key-plan-5")


def test_a_snapshot_is_an_immutable_plan_with_the_fixed_window() -> None:
    plan = service(snapshot(ASSET, (reference(),))).preview(ASSET, "ordinary", "key-plan-6")
    assert plan.eligible is False
    assert plan.expires_at - plan.created_at == timedelta(seconds=300)
    assert plan.dependencies == (reference(),)
    wire: dict[str, Any] = plan_payload(plan)
    _deletion_validator("DeletionPlan").validate(wire)
    digest = wire.pop("plan_digest")
    canonical = rfc8785.dumps(wire)
    assert digest == "sha256:" + hashlib.sha256(canonical).hexdigest()
    assert digest == plan_digest(plan)


def test_a_plan_without_blockers_is_eligible() -> None:
    plan = service(snapshot(ASSET)).preview(ASSET, "ordinary", "key-plan-7")
    assert plan.dependencies == ()
    assert plan.eligible is True


async def _post(
    app: Any, body: Any, *, key: str | None = "key-http-1", headers: dict[str, str] | None = None
) -> httpx.Response:
    request_headers = dict(headers or {})
    if key is not None:
        request_headers["Idempotency-Key"] = key
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://localhost"
    ) as http:
        return await http.post("/v1/nexus/deletion-plans", json=body, headers=request_headers)


def request_body(target: DeletionTarget, mode: str = "ordinary") -> dict[str, Any]:
    return {
        "target": {
            "owner": target.owner,
            "kind": target.kind,
            "resource": {"id": target.resource_id, "revision": str(target.revision)},
        },
        "mode": mode,
    }


def http_app(*snapshots: PlanSnapshot) -> Any:
    return create_app(settings=_settings(), deletions=service(*snapshots))


async def test_http_returns_the_contract_plan_and_replays_it() -> None:
    app = http_app(snapshot(ASSET, (reference(),)))
    first = await _post(app, request_body(ASSET))
    assert first.status_code == 200, first.text
    _deletion_validator("DeletionPlan").validate(first.json())
    assert first.json()["target"]["resource"] == {"id": ASSET.resource_id, "revision": "3"}
    assert first.json()["eligible"] is False
    assert first.json()["dependencies"] == [
        {
            "target": {
                "owner": "speech",
                "kind": "voice_revision",
                "resource": {"id": "voice-plan-1", "revision": "2"},
            },
            "relation": "persistent_reference",
            "disposition": "blocks",
        }
    ]
    replay = await _post(app, request_body(ASSET))
    assert replay.status_code == 200
    assert replay.json() == first.json()


async def test_http_same_key_different_request_is_409() -> None:
    app = http_app(snapshot(ASSET))
    assert (await _post(app, request_body(ASSET))).status_code == 200
    conflict = await _post(app, request_body(ASSET, "cascade"))
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "idempotency_conflict"
    assert conflict.json()["error"]["retryable"] is False


async def test_http_owner_mismatch_is_422() -> None:
    target = DeletionTarget("workbench", "project", "project-plan-1", 1)
    response = await _post(http_app(snapshot(target)), request_body(target))
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "deletion_owner_mismatch"


async def test_http_missing_target_is_404() -> None:
    response = await _post(http_app(), request_body(ASSET))
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "resource_not_found"


async def test_http_stale_revision_is_409() -> None:
    stale = DeletionTarget("speech", "asset", "asset-plan-1", 2)
    response = await _post(http_app(snapshot(ASSET)), request_body(stale))
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "deletion_plan_changed"


async def test_http_requires_one_bounded_idempotency_key() -> None:
    app = http_app(snapshot(ASSET))
    missing = await _post(app, request_body(ASSET), key=None)
    assert missing.status_code == 422
    assert missing.json()["error"]["code"] == "invalid_idempotency_key"
    assert missing.json()["error"]["param"] == "Idempotency-Key"
    too_long = await _post(app, request_body(ASSET), key="k" * 129)
    assert too_long.status_code == 422


async def test_http_rejects_an_unknown_body_shape() -> None:
    app = http_app(snapshot(ASSET))
    response = await _post(app, {"target": {"owner": "speech"}, "mode": "ordinary"})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_deletion_request"


async def test_http_unsupported_speech_kind_is_422() -> None:
    target = DeletionTarget("speech", "artifact", "artifact-plan-1", 1)
    response = await _post(http_app(), request_body(target))
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "deletion_target_unsupported"


async def test_http_without_assembled_storage_is_a_real_503() -> None:
    response = await _post(create_app(settings=_settings()), request_body(ASSET))
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "deletion_plans_unavailable"
    # A real outage is not the retired placeholder: callers can retry it.
    assert response.json()["error"]["retryable"] is True


async def test_http_does_not_echo_a_body_over_the_bound() -> None:
    app = http_app(snapshot(ASSET))
    oversized = copy.deepcopy(request_body(ASSET))
    oversized["target"]["resource"]["id"] = "asset-" + "x" * 20_000
    response = await _post(app, oversized)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_deletion_request"


# The execution half is decided in the domain before any transaction runs: these
# pin the refusal order and the set semantics the route relies on.


def _confirmation(plan: DeletionPlan, targets: tuple[DeletionTarget, ...] | None = None) -> Any:
    from speechnexus_backend.speech.domain.deletion import DeletionRequest

    return DeletionRequest(
        plan.id,
        plan.revision,
        plan_digest(plan),
        targets if targets is not None else (plan.target,),
    )


def test_confirmation_is_order_insensitive_and_rejects_duplicates() -> None:
    plan = service(snapshot(ASSET)).preview(ASSET, "ordinary", "key-plan-8")
    from speechnexus_backend.speech.domain.deletion import DeletionRequest, confirm

    reordered = DeletionRequest(plan.id, plan.revision, plan_digest(plan), (plan.target,))
    confirm(plan, reordered, now=plan.created_at)
    with pytest.raises(ValueError, match="distinct targets"):
        DeletionRequest(plan.id, plan.revision, plan_digest(plan), (plan.target, plan.target))
    with pytest.raises(ValueError, match="at least one target"):
        DeletionRequest(plan.id, plan.revision, plan_digest(plan), ())


def test_confirm_reports_the_first_reason_a_plan_is_stale() -> None:
    from speechnexus_backend.speech.domain.deletion import (
        DeletionConfirmationMismatchError,
        DeletionDependencyConflictError,
        DeletionPlanExpiredError,
        DeletionRequest,
        confirm,
    )

    plan = service(snapshot(ASSET)).preview(ASSET, "ordinary", "key-plan-9")
    confirmed = _confirmation(plan)
    # A wrong digest identifies a different plan, reported before the expiry the
    # caller would otherwise see.
    wrong_digest = DeletionRequest(plan.id, plan.revision, "sha256:" + "0" * 64, confirmed.targets)
    with pytest.raises(DeletionPlanChangedError):
        confirm(plan, wrong_digest, now=plan.created_at + timedelta(seconds=301))
    # The right plan, past its fixed window, reports expiry.
    with pytest.raises(DeletionPlanExpiredError):
        confirm(plan, confirmed, now=plan.created_at + timedelta(seconds=301))
    # A blocker is reported before the confirmed set can be compared.
    blocked = service(snapshot(ASSET, (reference(),))).preview(ASSET, "ordinary", "key-plan-10")
    with pytest.raises(DeletionDependencyConflictError):
        confirm(blocked, _confirmation(blocked), now=blocked.created_at)
    # Only then is the scope compared, and a target outside it is a mismatch.
    rejected = DeletionTarget("speech", "asset", "asset-other", 1)
    with pytest.raises(DeletionConfirmationMismatchError):
        confirm(plan, _confirmation(plan, (rejected,)), now=plan.created_at)


def test_the_intent_digest_ignores_confirmation_order() -> None:
    from speechnexus_backend.speech.domain.deletion import deletion_intent_digest

    plan = service(snapshot(ASSET)).preview(ASSET, "ordinary", "key-plan-11")
    first = deletion_intent_digest(_confirmation(plan))
    second = deletion_intent_digest(_confirmation(plan, tuple(reversed((plan.target,)))))
    assert first == second
    different = deletion_intent_digest(
        _confirmation(plan, (DeletionTarget("speech", "asset", "asset-other", 1),))
    )
    assert different != first
