"""Registration transaction rules: host, pool, quota, idempotency and generation order.

These tests drive the orchestration with a SQL-shaped fake cursor. The domain
transactions (``create_in_transaction`` / ``observe_in_transaction``) are stubbed
here because their real SQL is proven against PostgreSQL in
``tests/integration/test_engine_registration_pg.py``.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any, cast

import pytest

from speechnexus_backend import engine_registration as registration
from speechnexus_backend.engine_registration import (
    EXIT_CONFLICT,
    EXIT_UNAVAILABLE,
    EngineProbe,
    RegistrationError,
    RegistrationResult,
    RegistrationSpec,
    _Cursor,
    _default_capacity,
    _ensure_host,
    _ensure_pool,
    _ensure_quota,
    _existing_result,
    _register_in_transaction,
    _require_capacity,
    _require_schema,
    _snapshot,
)
from speechnexus_backend.speech.domain.capacity import PhysicalResources
from speechnexus_backend.speech.domain.runtime_intents import (
    ApprovedRuntimePlan,
    RuntimeBinding,
)
from speechnexus_backend.speech.domain.runtime_observations import VerifiedRuntimeReadiness
from speechnexus_backend.speech.infrastructure.runtime_observation_codec import (
    readiness_to_document,
)

DIGEST = "sha256:" + "a" * 64
DIMENSIONS = (
    "unified_physical_memory_bytes",
    "device_memory_bytes",
    "host_memory_bytes",
    "cpu_decode_millicores",
    "host_pcm_buffer_bytes",
    "scratch_disk_bytes",
)
PEAK = PhysicalResources(0, 4_000_000_000, 1_000_000_000, 1_000, 0, 0)
PER_SLOT = PhysicalResources(0, 500_000_000, 250_000_000, 100, 0, 0)
ZERO = PhysicalResources(0, 0, 0, 0, 0, 0)
NOW = 1_000_000


def make_plan(*, max_slots: int = 2) -> ApprovedRuntimePlan:
    return ApprovedRuntimePlan(
        RuntimeBinding(
            "node-1",
            "asr-batch",
            "gateway-1",
            "template-1",
            "binding-rev-1",
            "manifest-1",
            DIGEST,
        ),
        DIGEST,
        DIGEST,
        "discrete_device_memory",
        "batch",
        "resident",
        max_slots,
        0,
        PEAK,
        PEAK,
        PER_SLOT,
    )


def make_spec(*, generation: int = 1) -> RegistrationSpec:
    plan = make_plan()
    return RegistrationSpec(
        model_id="asr-quality",
        workspace_id="workspace-default",
        policy_revision_id="policy-revision-1",
        instance_id="instance-1",
        generation=generation,
        plan=plan,
        host_capacity=_default_capacity(plan),
        max_attempts=8,
    )


def make_probe() -> EngineProbe:
    return EngineProbe(
        url="http://127.0.0.1:18100/health",
        status=200,
        content_type="application/json",
        body_sha256="sha256:" + "b" * 64,
        body_bytes=15,
        observed_at_us=NOW,
    )


def with_vector(row: dict[str, Any], prefix: str, vector: PhysicalResources) -> dict[str, Any]:
    for name, value in zip(DIMENSIONS, vector.values, strict=True):
        row[prefix + name] = value
    return row


def host_row(
    node: str = "node-1",
    *,
    architecture: str = "discrete_device_memory",
    topology: str = DIGEST,
    state: str = "active",
    allocatable: PhysicalResources | None = None,
    held: PhysicalResources = ZERO,
    quarantined: PhysicalResources = ZERO,
    version: int = 1,
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "host_node_id": node,
        "memory_architecture": architecture,
        "node_state": state,
        "topology_digest": topology,
        "version": version,
    }
    with_vector(row, "allocatable_", allocatable or _default_capacity(make_plan()))
    with_vector(row, "held_", held)
    with_vector(row, "quarantined_", quarantined)
    return row


def pool_row(
    *,
    service_class: str = "batch",
    owner: str | None = None,
    fence: int = 0,
    expires: int = 0,
    owner_state: str = "reconciling_takeover",
    pool_state: str = "active",
    version: int = 1,
) -> dict[str, Any]:
    return {
        "pool_id": "asr-batch",
        "service_class": service_class,
        "pool_state": pool_state,
        "owner_id": owner,
        "fencing_token": fence,
        "owner_expires_at_us": expires,
        "owner_state": owner_state,
        "version": version,
    }


def quota_row(
    *,
    policy: str = "policy-revision-1",
    max_attempts: int = 8,
    version: int = 1,
) -> dict[str, Any]:
    return {
        "workspace_id": "workspace-default",
        "max_attempts": max_attempts,
        "held_attempts": 0,
        "quarantined_attempts": 0,
        "policy_revision": policy,
        "version": version,
    }


def head_row(
    spec: RegistrationSpec,
    *,
    generation: int | None = None,
    high_water: int | None = None,
    prepare: str | None = None,
    stop: str | None = None,
    version: int = 1,
) -> dict[str, Any]:
    current = spec.generation if generation is None else generation
    return {
        "instance_id": spec.instance_id,
        "current_generation": current,
        "generation_high_water": current if high_water is None else high_water,
        "current_prepare_intent_id": prepare,
        "current_stop_intent_id": stop,
        "version": version,
    }


def instance_row(
    spec: RegistrationSpec,
    *,
    generation: int | None = None,
    state: str = "ready",
    budget: str = "steady",
    fence: int = 1,
    ready_at: int | None = NOW,
    version: int = 1,
    **overrides: object,
) -> dict[str, Any]:
    plan, binding = spec.plan, spec.plan.binding
    row: dict[str, Any] = {
        "instance_id": spec.instance_id,
        "instance_generation": spec.generation if generation is None else generation,
        "instance_state": state,
        "base_budget_kind": budget,
        "host_node_id": binding.target_node_id,
        "pool_id": binding.pool_id,
        "gateway_id": binding.gateway_id,
        "binding_revision_id": binding.binding_revision_id,
        "manifest_id": binding.manifest_id,
        "approved_template_id": binding.approved_template_id,
        "management_mode": "managed",
        "loading_policy": plan.loading_policy,
        "config_digest": binding.config_digest,
        "max_slots": plan.max_slots,
        "realtime_reserved_slots": plan.realtime_reserved_slots,
        "last_fencing_token": fence,
        "ready_at_us": ready_at,
        "version": version,
    }
    with_vector(row, "peak_", plan.peak)
    with_vector(row, "steady_", plan.steady)
    with_vector(row, "per_slot_", plan.per_slot)
    row.update(overrides)
    return row


def readiness(
    spec: RegistrationSpec, *, verified: int = NOW, expires: int = NOW + 60_000_000
) -> dict[str, Any]:
    receipt = VerifiedRuntimeReadiness(
        spec.plan.binding,
        spec.instance_id,
        spec.generation,
        1,
        spec.plan.topology_digest,
        spec.plan.qualification_digest,
        DIGEST,
        verified,
        expires,
    )
    return cast(dict[str, Any], readiness_to_document(receipt))


def intent_row(
    spec: RegistrationSpec,
    intent_id: str,
    *,
    state: str = "succeeded",
    observation: bool = True,
    receipt: dict[str, Any] | None = None,
    version: int = 1,
) -> dict[str, Any]:
    return {
        "target_node_id": spec.plan.binding.target_node_id,
        "intent_id": intent_id,
        "instance_id": spec.instance_id,
        "instance_generation": spec.generation,
        "action": "prepare",
        "intent_state": state,
        "request_digest": DIGEST,
        "observation_document": "{}" if observation else None,
        "readiness_document": receipt if receipt is not None else readiness(spec),
        "version": version,
        "host_node_id": spec.plan.binding.target_node_id,
    }


class Store:
    """In-memory stand-in for the coordination tables the command touches."""

    def __init__(self) -> None:
        self.hosts: dict[str, dict[str, Any]] = {}
        self.pools: dict[str, dict[str, Any]] = {}
        self.quotas: dict[str, dict[str, Any]] = {}
        self.instances: dict[tuple[str, int], dict[str, Any]] = {}
        self.heads: dict[str, dict[str, Any]] = {}
        self.intents: dict[tuple[str, str], dict[str, Any]] = {}
        self.now_us = NOW
        self.present: dict[str, str | None] = {
            "engine": "engine_instances",
            "heads": "runtime_instance_heads",
            "intents": "runtime_instance_intents",
        }
        self.writes: list[tuple[str, tuple[object, ...]]] = []
        self.unhandled: list[str] = []
        self.failing_updates: set[str] = set()
        self.rows: list[dict[str, Any]] = []
        self.rowcount = 1

    def cursor(self) -> _Cursor:
        return _Cursor(self, time.monotonic() + 60)

    def __enter__(self) -> Store:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def execute(self, query: str, parameters: tuple[object, ...] = ()) -> Store:
        self.rows = self.handle(" ".join(query.split()), parameters)
        return self

    def fetchall(self) -> list[dict[str, Any]]:
        return self.rows

    def write(self, sql: str, parameters: tuple[object, ...]) -> None:
        self.writes.append((sql, parameters))
        self.rowcount = 0 if any(sql.startswith(name) for name in self.failing_updates) else 1

    def handle(self, sql: str, params: tuple[object, ...]) -> list[dict[str, Any]]:
        self.rowcount = 1
        if "set_config" in sql:
            return []
        if "to_regclass" in sql:
            return [dict(self.present)]
        if "clock_timestamp" in sql:
            return [{"now_us": self.now_us}]
        if sql.startswith("INSERT INTO host_nodes"):
            node = cast(str, params[0])
            self.hosts.setdefault(
                node,
                host_row(
                    node,
                    architecture=cast(str, params[1]),
                    topology=cast(str, params[2]),
                    allocatable=PhysicalResources(*cast(tuple[int, ...], params[3:])),
                ),
            )
            return []
        if sql.startswith("UPDATE host_nodes"):
            self.write(sql, params)
            if self.rowcount:
                self.hosts[cast(str, params[0])].update(
                    node_state="active",
                    version=int(self.hosts[cast(str, params[0])]["version"]) + 1,
                )
            return []
        if "FROM host_nodes" in sql:
            return [self.hosts[cast(str, params[0])]]
        if sql.startswith("INSERT INTO resource_pools"):
            self.pools.setdefault(
                cast(str, params[0]), pool_row(service_class=cast(str, params[1]))
            )
            return []
        if sql.startswith("UPDATE resource_pools"):
            self.write(sql, params)
            if self.rowcount:
                row = self.pools[cast(str, params[3])]
                row.update(
                    owner_id=params[0],
                    fencing_token=params[1],
                    owner_expires_at_us=params[2],
                    owner_state="active",
                    pool_state="active",
                    version=int(row["version"]) + 1,
                )
            return []
        if "FROM resource_pools" in sql:
            return [self.pools[cast(str, params[0])]]
        if sql.startswith("INSERT INTO workspace_quotas"):
            self.quotas.setdefault(
                cast(str, params[0]),
                quota_row(max_attempts=cast(int, params[1]), policy=cast(str, params[2])),
            )
            return []
        if "FROM workspace_quotas" in sql:
            return [self.quotas[cast(str, params[0])]]
        if "FROM runtime_instance_heads" in sql:
            head = self.heads.get(cast(str, params[0]))
            return [head] if head is not None else []
        if "SELECT instance_id FROM engine_instances" in sql:
            return [
                {"instance_id": key[0]} for key in self.instances if key[0] == cast(str, params[0])
            ][:1]
        if "FROM engine_instances" in sql:
            if len(params) == 2:
                instance = self.instances.get((cast(str, params[0]), cast(int, params[1])))
                return [instance] if instance is not None else []
            return [self.instances[key] for key in self.instances if key[0] == params[0]]
        if "FROM runtime_instance_intents" in sql:
            intent = self.intents.get((cast(str, params[0]), cast(str, params[1])))
            if intent is None:
                return []
            if "has_observation" in sql:
                return [
                    dict(
                        intent,
                        has_observation=intent["observation_document"] is not None,
                        has_readiness=intent["readiness_document"] is not None,
                    )
                ]
            return [intent]
        if sql.startswith("UPDATE runtime_instance_intents"):
            self.write(sql, params)
            if self.rowcount:
                self.intents[(cast(str, params[2]), cast(str, params[3]))].update(
                    readiness_document=params[0], updated_at_us=params[1]
                )
            return []
        if sql.startswith("UPDATE engine_instances"):
            self.write(sql, params)
            if self.rowcount:
                row = self.instances[(cast(str, params[1]), cast(int, params[2]))]
                row["ready_at_us"] = params[0]
                row["version"] = int(row["version"]) + 1
            return []
        if sql.startswith("UPDATE runtime_intent_deliveries"):
            self.write(sql, params)
            return []
        self.unhandled.append(sql)
        return []


def ready_store(spec: RegistrationSpec, *, receipt: dict[str, Any] | None = None) -> Store:
    store = Store()
    store.hosts["node-1"] = host_row()
    store.pools["asr-batch"] = pool_row(
        owner="speechnexus-operator", fence=1, expires=NOW + 10**12, owner_state="active"
    )
    store.quotas["workspace-default"] = quota_row()
    store.instances[(spec.instance_id, spec.generation)] = instance_row(spec)
    store.heads[spec.instance_id] = head_row(spec, prepare="register-instance-1-g1")
    store.intents[("node-1", "register-instance-1-g1")] = intent_row(
        spec,
        "register-instance-1-g1",
        receipt=readiness(spec) if receipt is None else receipt,
    )
    return store


def error_of(call: Callable[[], object]) -> RegistrationError:
    with pytest.raises(RegistrationError) as caught:
        call()
    return caught.value


def test_require_schema_accepts_every_coordination_table() -> None:
    store = Store()
    _require_schema(store.cursor())
    assert store.unhandled == []


@pytest.mark.parametrize("missing", ["engine", "heads", "intents"])
def test_require_schema_names_the_migration_step(missing: str) -> None:
    store = Store()
    store.present[missing] = None
    error = error_of(lambda: _require_schema(store.cursor()))
    assert (error.code, error.exit_code) == ("schema_not_migrated", EXIT_UNAVAILABLE)


def test_ensure_host_inserts_the_declared_capacity() -> None:
    store, spec = Store(), make_spec()
    row = _ensure_host(store.cursor(), spec)
    assert row["allocatable_device_memory_bytes"] == spec.host_capacity.device_memory_bytes
    assert row["node_state"] == "active"
    assert store.writes == []


@pytest.mark.parametrize(
    ("changes", "code"),
    [
        ({"architecture": "unified_memory"}, "host_architecture_mismatch"),
        ({"topology": "sha256:" + "c" * 64}, "host_topology_mismatch"),
        ({"state": "quarantined"}, "host_quarantined"),
    ],
)
def test_ensure_host_refuses_a_different_or_quarantined_host(
    changes: dict[str, object], code: str
) -> None:
    store, spec = Store(), make_spec()
    store.hosts["node-1"] = host_row(**cast(Any, changes))
    error = error_of(lambda: _ensure_host(store.cursor(), spec))
    assert (error.code, error.exit_code) == (code, EXIT_CONFLICT)


def test_ensure_host_reactivates_a_draining_node() -> None:
    store, spec = Store(), make_spec()
    store.hosts["node-1"] = host_row(state="draining")
    row = _ensure_host(store.cursor(), spec)
    assert row["node_state"] == "active"
    assert store.writes[0][0].startswith("UPDATE host_nodes")


def test_ensure_host_reports_a_version_conflict_while_reactivating() -> None:
    store, spec = Store(), make_spec()
    store.hosts["node-1"] = host_row(state="draining")
    store.failing_updates.add("UPDATE host_nodes")
    assert error_of(lambda: _ensure_host(store.cursor(), spec)).code == "version_conflict"


def test_ensure_pool_claims_a_fresh_pool_with_the_first_fencing_token() -> None:
    store, spec = Store(), make_spec()
    row = _ensure_pool(
        store.cursor(), spec, owner_id="speechnexus-operator", fencing_token=None, owner_ttl_us=10
    )
    assert (row["owner_id"], row["fencing_token"], row["owner_state"]) == (
        "speechnexus-operator",
        1,
        "active",
    )
    assert len(store.writes) == 1


def test_ensure_pool_honours_an_explicit_first_token() -> None:
    store, spec = Store(), make_spec()
    row = _ensure_pool(
        store.cursor(), spec, owner_id="speechnexus-operator", fencing_token=7, owner_ttl_us=10
    )
    assert row["fencing_token"] == 7


def test_ensure_pool_keeps_a_live_owner_without_rewriting_it() -> None:
    store, spec = Store(), make_spec()
    store.pools["asr-batch"] = pool_row(
        owner="speechnexus-operator", fence=4, expires=NOW + 10, owner_state="active"
    )
    row = _ensure_pool(
        store.cursor(), spec, owner_id="speechnexus-operator", fencing_token=4, owner_ttl_us=10
    )
    assert row["fencing_token"] == 4
    assert store.writes == []


def test_ensure_pool_renews_an_expired_lease_for_the_same_owner() -> None:
    store, spec = Store(), make_spec()
    store.pools["asr-batch"] = pool_row(
        owner="speechnexus-operator", fence=4, expires=NOW, owner_state="active"
    )
    row = _ensure_pool(
        store.cursor(), spec, owner_id="speechnexus-operator", fencing_token=4, owner_ttl_us=10
    )
    assert row["fencing_token"] == 4
    assert len(store.writes) == 1


@pytest.mark.parametrize(
    ("pool", "fence", "code"),
    [
        (
            pool_row(owner="speechnexus-operator", fence=0, expires=NOW + 10),
            None,
            "stale_fencing_token",
        ),
        (
            pool_row(owner="speechnexus-operator", fence=3, expires=NOW + 10),
            9,
            "fencing_token_conflict",
        ),
        (pool_row(owner="other-owner", fence=3, expires=NOW + 10), None, "pool_owner_conflict"),
        (pool_row(owner="other-owner", fence=5, expires=NOW), 5, "stale_fencing_token"),
        (pool_row(service_class="realtime"), None, "pool_service_class_conflict"),
    ],
)
def test_ensure_pool_refuses_foreign_or_stale_ownership(
    pool: dict[str, Any], fence: int | None, code: str
) -> None:
    store, spec = Store(), make_spec()
    store.pools["asr-batch"] = pool
    error = error_of(
        lambda: _ensure_pool(
            store.cursor(),
            spec,
            owner_id="speechnexus-operator",
            fencing_token=fence,
            owner_ttl_us=10,
        )
    )
    assert (error.code, error.exit_code) == (code, EXIT_CONFLICT)


def test_ensure_pool_takes_over_an_expired_foreign_owner_with_a_higher_token() -> None:
    store, spec = Store(), make_spec()
    store.pools["asr-batch"] = pool_row(
        owner="other-owner", fence=5, expires=NOW, owner_state="active"
    )
    row = _ensure_pool(
        store.cursor(), spec, owner_id="speechnexus-operator", fencing_token=None, owner_ttl_us=10
    )
    assert (row["owner_id"], row["fencing_token"]) == ("speechnexus-operator", 6)


def test_ensure_pool_reports_a_version_conflict_when_another_writer_moved_it() -> None:
    store, spec = Store(), make_spec()
    store.failing_updates.add("UPDATE resource_pools")
    assert (
        error_of(
            lambda: _ensure_pool(
                store.cursor(),
                spec,
                owner_id="speechnexus-operator",
                fencing_token=None,
                owner_ttl_us=10,
            )
        ).code
        == "version_conflict"
    )


def test_ensure_quota_creates_the_workspace_allowance() -> None:
    store, spec = Store(), make_spec()
    row = _ensure_quota(store.cursor(), spec)
    assert (row["max_attempts"], row["policy_revision"]) == (8, "policy-revision-1")


def test_ensure_quota_accepts_a_looser_existing_allowance() -> None:
    store, spec = Store(), make_spec()
    store.quotas["workspace-default"] = quota_row(max_attempts=16)
    assert _ensure_quota(store.cursor(), spec)["max_attempts"] == 16


@pytest.mark.parametrize(
    ("row", "code"),
    [
        (quota_row(policy="policy-other"), "quota_policy_conflict"),
        (quota_row(max_attempts=1), "quota_capacity_conflict"),
    ],
)
def test_ensure_quota_refuses_to_reinterpret_or_shrink_the_allowance(
    row: dict[str, Any], code: str
) -> None:
    store, spec = Store(), make_spec()
    store.quotas["workspace-default"] = row
    assert error_of(lambda: _ensure_quota(store.cursor(), spec)).code == code


def test_require_capacity_accepts_a_host_with_a_free_margin() -> None:
    store, spec = Store(), make_spec()
    store.hosts["node-1"] = host_row(
        allocatable=PhysicalResources(0, 9_000_000_000, 3_000_000_000, 3_000, 0, 0)
    )
    _require_capacity(store.cursor(), spec)


def test_require_capacity_counts_held_and_quarantined_resources() -> None:
    store, spec = Store(), make_spec()
    store.hosts["node-1"] = host_row(
        allocatable=PhysicalResources(0, 5_000_000_000, 1_500_000_000, 1_200, 0, 0),
        held=PhysicalResources(0, 100_000_000, 0, 0, 0, 0),
    )
    error = error_of(lambda: _require_capacity(store.cursor(), spec))
    assert (error.code, error.exit_code) == ("host_capacity_insufficient", EXIT_CONFLICT)
    assert "need at least 0,5000000000,1500000000,1200,0,0" in error.message


def test_existing_result_returns_none_for_a_new_instance() -> None:
    store, spec = Store(), make_spec()
    store.pools["asr-batch"] = pool_row(owner="speechnexus-operator", fence=1, expires=NOW + 10)
    result = _existing_result(
        store.cursor(),
        spec,
        pool=store.pools["asr-batch"],
        owner_id="speechnexus-operator",
        evidence_digest=DIGEST,
        evidence_document={},
        probe=make_probe(),
        readiness_ttl_us=60_000_000,
    )
    assert result is None


def test_existing_result_requires_reconciliation_for_a_headless_instance() -> None:
    store, spec = Store(), make_spec()
    store.pools["asr-batch"] = pool_row(owner="speechnexus-operator", fence=1, expires=NOW + 10)
    store.instances[(spec.instance_id, spec.generation)] = instance_row(spec)
    error = error_of(
        lambda: _existing_result(
            store.cursor(),
            spec,
            pool=store.pools["asr-batch"],
            owner_id="speechnexus-operator",
            evidence_digest=DIGEST,
            evidence_document={},
            probe=make_probe(),
            readiness_ttl_us=60_000_000,
        )
    )
    assert error.code == "reconciliation_required"


@pytest.mark.parametrize(("generation", "current", "high_water"), [(1, 2, 2), (2, 3, 4), (5, 3, 5)])
def test_existing_result_refuses_a_generation_below_high_water(
    generation: int, current: int, high_water: int
) -> None:
    spec = make_spec(generation=generation)
    store = Store()
    store.pools["asr-batch"] = pool_row(owner="speechnexus-operator", fence=1, expires=NOW + 10)
    store.heads[spec.instance_id] = head_row(spec, generation=current, high_water=high_water)
    store.instances[(spec.instance_id, generation)] = instance_row(spec, generation=generation)
    error = error_of(
        lambda: _existing_result(
            store.cursor(),
            spec,
            pool=store.pools["asr-batch"],
            owner_id="speechnexus-operator",
            evidence_digest=DIGEST,
            evidence_document={},
            probe=make_probe(),
            readiness_ttl_us=60_000_000,
        )
    )
    assert (error.code, error.exit_code) == ("stale_instance_generation", EXIT_CONFLICT)
    assert f"generation {generation} is not above high water {high_water}" in error.message


@pytest.mark.parametrize(
    ("mutation", "code"),
    [
        ({"instance_state": "draining"}, "instance_not_ready"),
        ({"config_digest": "sha256:" + "c" * 64}, "instance_config_conflict"),
        ({"last_fencing_token": 9}, "stale_fencing_token"),
        ({"management_mode": "external"}, "instance_config_conflict"),
        ({"max_slots": 3}, "instance_config_conflict"),
    ],
)
def test_existing_result_refuses_a_ready_row_that_drifted(
    mutation: dict[str, object], code: str
) -> None:
    spec = make_spec()
    store = ready_store(spec)
    store.instances[(spec.instance_id, spec.generation)].update(mutation)
    error = error_of(
        lambda: _existing_result(
            store.cursor(),
            spec,
            pool=store.pools["asr-batch"],
            owner_id="speechnexus-operator",
            evidence_digest=DIGEST,
            evidence_document={},
            probe=make_probe(),
            readiness_ttl_us=60_000_000,
        )
    )
    assert (error.code, error.exit_code) == (code, EXIT_CONFLICT)


def test_existing_result_requires_a_prepare_intent() -> None:
    spec = make_spec()
    store = ready_store(spec)
    store.heads[spec.instance_id]["current_prepare_intent_id"] = None
    error = error_of(
        lambda: _existing_result(
            store.cursor(),
            spec,
            pool=store.pools["asr-batch"],
            owner_id="speechnexus-operator",
            evidence_digest=DIGEST,
            evidence_document={},
            probe=make_probe(),
            readiness_ttl_us=60_000_000,
        )
    )
    assert error.code == "reconciliation_required"


def test_existing_result_refuses_an_outstanding_stop_intent() -> None:
    spec = make_spec()
    store = ready_store(spec)
    store.heads[spec.instance_id]["current_stop_intent_id"] = "stop-instance-1-g1"
    error = error_of(
        lambda: _existing_result(
            store.cursor(),
            spec,
            pool=store.pools["asr-batch"],
            owner_id="speechnexus-operator",
            evidence_digest=DIGEST,
            evidence_document={},
            probe=make_probe(),
            readiness_ttl_us=60_000_000,
        )
    )
    assert error.code == "instance_not_ready"


@pytest.mark.parametrize("key", ["intent_state", "observation_document", "readiness_document"])
def test_existing_result_requires_successful_observation_and_readiness(key: str) -> None:
    spec = make_spec()
    store = ready_store(spec)
    store.intents[("node-1", "register-instance-1-g1")][key] = None
    error = error_of(
        lambda: _existing_result(
            store.cursor(),
            spec,
            pool=store.pools["asr-batch"],
            owner_id="speechnexus-operator",
            evidence_digest=DIGEST,
            evidence_document={},
            probe=make_probe(),
            readiness_ttl_us=60_000_000,
        )
    )
    assert error.code == "reconciliation_required"


def test_existing_result_rejects_an_unparseable_readiness_receipt() -> None:
    spec = make_spec()
    store = ready_store(spec)
    store.intents[("node-1", "register-instance-1-g1")]["readiness_document"] = {
        "instance_id": "not a valid receipt"
    }
    error = error_of(
        lambda: _existing_result(
            store.cursor(),
            spec,
            pool=store.pools["asr-batch"],
            owner_id="speechnexus-operator",
            evidence_digest=DIGEST,
            evidence_document={},
            probe=make_probe(),
            readiness_ttl_us=60_000_000,
        )
    )
    assert error.code == "reconciliation_required"


def test_existing_result_returns_already_ready_without_writing() -> None:
    spec = make_spec()
    store = ready_store(spec)
    result = _existing_result(
        store.cursor(),
        spec,
        pool=store.pools["asr-batch"],
        owner_id="speechnexus-operator",
        evidence_digest=DIGEST,
        evidence_document={"kind": "probe"},
        probe=make_probe(),
        readiness_ttl_us=60_000_000,
    )
    assert result is not None
    assert (result.outcome, result.instance_id, result.fencing_token) == (
        "already_ready",
        "instance-1",
        1,
    )
    assert result.evidence_digest == DIGEST
    assert store.writes == []
    assert result.rows["engine_instances"]["instance_state"] == "ready"


def test_existing_result_refreshes_an_expired_readiness_receipt() -> None:
    spec = make_spec()
    store = ready_store(spec, receipt=readiness(spec, verified=NOW - 10, expires=NOW))
    result = _existing_result(
        store.cursor(),
        spec,
        pool=store.pools["asr-batch"],
        owner_id="speechnexus-operator",
        evidence_digest=DIGEST,
        evidence_document={},
        probe=make_probe(),
        readiness_ttl_us=60_000_000,
    )
    assert result is not None
    assert result.outcome == "already_ready"
    assert result.readiness_expires_at_us == NOW + 60_000_000
    assert [sql.split(" SET", 1)[0] for sql, _ in store.writes] == [
        "UPDATE runtime_instance_intents",
        "UPDATE engine_instances",
    ]


@pytest.mark.parametrize(
    ("prefix", "message"),
    [
        ("UPDATE runtime_instance_intents", "prepare intent changed while refreshing readiness"),
        ("UPDATE engine_instances", "instance changed while refreshing readiness"),
    ],
)
def test_existing_result_reports_a_version_conflict_while_refreshing(
    prefix: str, message: str
) -> None:
    spec = make_spec()
    store = ready_store(spec, receipt=readiness(spec, verified=NOW - 10, expires=NOW))
    store.failing_updates.add(prefix)
    error = error_of(
        lambda: _existing_result(
            store.cursor(),
            spec,
            pool=store.pools["asr-batch"],
            owner_id="speechnexus-operator",
            evidence_digest=DIGEST,
            evidence_document={},
            probe=make_probe(),
            readiness_ttl_us=60_000_000,
        )
    )
    assert (error.code, error.exit_code) == ("version_conflict", EXIT_CONFLICT)
    assert error.message == message


def test_existing_result_refuses_a_newer_generation_while_the_current_one_is_live() -> None:
    spec = make_spec(generation=2)
    store = ready_store(make_spec(generation=1))
    store.heads[spec.instance_id].update(current_generation=1, generation_high_water=1)
    error = error_of(
        lambda: _existing_result(
            store.cursor(),
            spec,
            pool=store.pools["asr-batch"],
            owner_id="speechnexus-operator",
            evidence_digest=DIGEST,
            evidence_document={},
            probe=make_probe(),
            readiness_ttl_us=60_000_000,
        )
    )
    assert (error.code, error.exit_code) == ("previous_generation_active", EXIT_CONFLICT)
    assert "stop it before registering a new one" in error.message


def test_existing_result_allows_a_newer_generation_after_a_released_stop() -> None:
    spec = make_spec(generation=2)
    store = ready_store(make_spec(generation=1))
    store.heads[spec.instance_id].update(current_generation=1, generation_high_water=1)
    store.instances[(spec.instance_id, 1)].update(
        instance_state="stopped", base_budget_kind="released"
    )
    assert (
        _existing_result(
            store.cursor(),
            spec,
            pool=store.pools["asr-batch"],
            owner_id="speechnexus-operator",
            evidence_digest=DIGEST,
            evidence_document={},
            probe=make_probe(),
            readiness_ttl_us=60_000_000,
        )
        is None
    )


def test_snapshot_reports_every_coordination_row() -> None:
    spec = make_spec()
    store = ready_store(spec)
    rows = _snapshot(store.cursor(), spec)
    assert set(rows) == {
        "engine_instances",
        "runtime_instance_heads",
        "runtime_instance_intents",
        "resource_pools",
        "workspace_quotas",
    }
    assert rows["runtime_instance_intents"]["has_observation"] is True
    assert rows["runtime_instance_intents"]["has_readiness"] is True
    assert store.unhandled == []


def register_arguments(spec: RegistrationSpec) -> dict[str, Any]:
    return {
        "schema": "speech",
        "spec": spec,
        "probe": make_probe(),
        "evidence_digest": DIGEST,
        "evidence_document": {"kind": "probe"},
        "owner_id": "speechnexus-operator",
        "fencing_token": None,
        "owner_ttl_us": 86_400_000_000,
        "readiness_ttl_us": 60_000_000,
        "intent_deadline_us": 300_000_000,
        "intent_id": None,
        "timeout_seconds": 10.0,
    }


class Connection:
    def __init__(self, store: Store) -> None:
        self.store = store

    def cursor(self) -> Store:
        return self.store


def stub_domain(
    monkeypatch: pytest.MonkeyPatch,
    store: Store,
    spec: RegistrationSpec,
    *,
    create: str = "committed",
    observe: str = "committed",
) -> list[str]:
    """Simulate the domain transactions the real command delegates to."""
    calls: list[str] = []
    seen = {"intent_id": f"register-{spec.instance_id}-g{spec.generation}"}

    def create_in_transaction(
        cursor: object, command: Any, prepared: Any, *, clock: Callable[[], int]
    ) -> Any:
        calls.append("create")
        seen["intent_id"] = command.request.intent_id
        token = command.request.fencing_token
        store.instances[(spec.instance_id, spec.generation)] = instance_row(
            spec, state="preparing", fence=token, ready_at=None
        )
        store.heads[spec.instance_id] = head_row(spec, prepare=command.request.intent_id)
        store.intents[(spec.plan.binding.target_node_id, command.request.intent_id)] = intent_row(
            spec, command.request.intent_id, state="pending", observation=False, receipt={}
        )
        return SimpleNamespace(outcome=create, error_code="runtime_create_rejected")

    def observe_in_transaction(
        cursor: object, command: Any, prepared: Any, *, clock: Callable[[], int]
    ) -> Any:
        calls.append("observe")
        store.instances[(spec.instance_id, spec.generation)].update(
            instance_state="ready", ready_at_us=NOW
        )
        row = store.intents[(spec.plan.binding.target_node_id, seen["intent_id"])]
        row.update(
            intent_state="succeeded",
            observation_document="{}",
            readiness_document=readiness(spec),
        )
        return SimpleNamespace(outcome=observe, error_code="runtime_observe_rejected")

    monkeypatch.setattr(registration, "create_in_transaction", create_in_transaction)
    monkeypatch.setattr(registration, "observe_in_transaction", observe_in_transaction)
    monkeypatch.setattr(
        registration,
        "prepare_observation",
        lambda value: SimpleNamespace(observation=object(), document='{"stub":true}'),
    )
    return calls


def run_registration(spec: RegistrationSpec, store: Store, **changes: object) -> RegistrationResult:
    arguments = register_arguments(spec) | changes
    return _register_in_transaction(cast(Any, Connection(store)), **cast(Any, arguments))


def test_register_in_transaction_activates_exactly_one_instance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec, store = make_spec(), Store()
    calls = stub_domain(monkeypatch, store, spec)
    result = run_registration(spec, store)
    assert calls == ["create", "observe"]
    assert (result.outcome, result.instance_id, result.instance_generation) == (
        "registered",
        "instance-1",
        1,
    )
    assert (result.fencing_token, result.owner_id) == (1, "speechnexus-operator")
    assert result.rows["engine_instances"]["instance_state"] == "ready"
    assert result.rows["runtime_instance_heads"]["current_generation"] == 1
    deliveries = [write for write in store.writes if "runtime_intent_deliveries" in write[0]]
    assert len(deliveries) == 1
    assert cast(str, deliveries[0][1][-1]).startswith("runtime-event-")
    assert store.unhandled == []


def test_register_in_transaction_honours_an_explicit_intent_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec, store = make_spec(), Store()
    stub_domain(monkeypatch, store, spec)
    result = run_registration(spec, store, intent_id="operator-intent-1")
    assert result.intent_id == "operator-intent-1"
    assert store.heads[spec.instance_id]["current_prepare_intent_id"] == "operator-intent-1"


def test_register_in_transaction_reports_a_rejected_create(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec, store = make_spec(), Store()
    stub_domain(monkeypatch, store, spec, create="rejected")
    error = error_of(lambda: run_registration(spec, store))
    assert (error.code, error.exit_code) == ("runtime_create_rejected", EXIT_CONFLICT)


def test_register_in_transaction_reports_a_rejected_observation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec, store = make_spec(), Store()
    stub_domain(monkeypatch, store, spec, observe="rejected")
    error = error_of(lambda: run_registration(spec, store))
    assert (error.code, error.exit_code) == ("runtime_observe_rejected", EXIT_CONFLICT)


def test_register_in_transaction_short_circuits_an_already_ready_instance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec = make_spec()
    store = ready_store(spec)

    def refuse(*_: object, **__: object) -> Any:
        raise AssertionError("a ready instance must not be created again")

    monkeypatch.setattr(registration, "create_in_transaction", refuse)
    result = run_registration(spec, store)
    assert result.outcome == "already_ready"
    assert store.writes == []


def test_register_in_transaction_fails_past_its_bounded_deadline() -> None:
    spec, store = make_spec(), Store()
    error = error_of(lambda: run_registration(spec, store, timeout_seconds=-1.0))
    assert (error.code, error.exit_code) == ("transaction_timeout", EXIT_UNAVAILABLE)
    assert store.writes == []
