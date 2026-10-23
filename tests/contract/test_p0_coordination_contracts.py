"""Contract and schema invariant tests for P0-A machine contracts and P0-B coordination DDL."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any, cast
from urllib.parse import unquote, urlparse

import pytest
import yaml
from jsonschema.exceptions import ValidationError
from jsonschema.validators import validator_for
from openapi_spec_validator import validate
from referencing import Registry, Resource

_ROOT = Path(__file__).resolve().parents[2]
_CAPACITY_MANIFEST_SCHEMA_PATH = _ROOT / "contracts" / "capacity-manifest.schema.json"
_EXECUTION_CONTROL_OPENAPI_PATH = (
    _ROOT / "contracts" / "internal" / "execution-control.openapi.yaml"
)
_RUNTIME_CONTROL_OPENAPI_PATH = _ROOT / "contracts" / "internal" / "runtime-control.openapi.yaml"
_COORDINATION_SQL_PATH = _ROOT / "migrations" / "0001_cluster_coordination_draft.sql"


def _load_json(path: Path) -> dict[str, Any]:
    raw: object = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(raw, dict)
    document = cast(dict[str, Any], raw)
    _resolve_local_refs(document, path)
    return document


def _load_yaml(path: Path) -> dict[str, Any]:
    raw: object = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(raw, dict)
    document = cast(dict[str, Any], raw)
    _resolve_local_refs(document, path)
    return document


def _resolve_local_refs(node: Any, source: Path) -> None:
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str) and not ref.startswith(("#", "http://", "https://", "file://")):
            path_part, separator, fragment = ref.partition("#")
            if path_part:
                target = (source.parent / path_part).resolve()
                node["$ref"] = target.as_uri() + (f"#{fragment}" if separator else "")
        for value in node.values():
            _resolve_local_refs(value, source)
    elif isinstance(node, list):
        for value in node:
            _resolve_local_refs(value, source)


def _retrieve_contract(uri: str) -> Resource[Any]:
    parsed = urlparse(uri)
    assert parsed.scheme == "file"
    path = Path(unquote(parsed.path))
    return Resource.from_contents(_load_json(path))


_REGISTRY: Registry[Any] = cast(Any, Registry)(retrieve=_retrieve_contract)


def test_capacity_manifest_schema_and_positive_examples_are_valid() -> None:
    schema = _load_json(_CAPACITY_MANIFEST_SCHEMA_PATH)
    validator_cls = validator_for(schema)
    validator_cls.check_schema(schema)
    validator = validator_cls(schema, registry=_REGISTRY)

    examples = schema.get("examples")
    assert isinstance(examples, list)
    assert len(examples) >= 1
    for example in examples:
        validator.validate(example)
        stage_ids = [stage["stage_id"] for stage in example["stage_graph"]]
        assert len(stage_ids) == len(set(stage_ids))


def test_capacity_manifest_envelope_aggregation_is_frozen() -> None:
    schema = _load_json(_CAPACITY_MANIFEST_SCHEMA_PATH)
    example = cast(list[dict[str, Any]], schema["examples"])[0]
    stages = cast(list[dict[str, Any]], example["stage_graph"])
    envelope = cast(dict[str, Any], example["resource_envelope"])

    for budget_name in ("resident_budget", "loading_peak_budget"):
        envelope_budget = cast(dict[str, int], envelope[budget_name])
        for resource, declared_total in envelope_budget.items():
            assert declared_total == sum(
                cast(dict[str, int], stage[budget_name])[resource] for stage in stages
            )

    per_slot = cast(dict[str, int], envelope["per_slot_incremental_budget"])
    for resource, declared_total in per_slot.items():
        stage_values = [
            cast(dict[str, int], stage["per_slot_incremental_budget"])[resource] for stage in stages
        ]
        if resource == "engine_active_slots":
            assert declared_total == max(stage_values)
        else:
            assert declared_total == sum(stage_values)

    assert envelope["max_active_slots"] == min(stage["max_active_slots"] for stage in stages)
    assert envelope["realtime_reserved_slots"] <= envelope["max_active_slots"]


def test_capacity_manifest_rejects_external_production_and_missing_parent_budget() -> None:
    schema = _load_json(_CAPACITY_MANIFEST_SCHEMA_PATH)
    validator = validator_for(schema)(schema, registry=_REGISTRY)
    examples = cast(list[dict[str, Any]], schema["examples"])
    base_example = copy.deepcopy(examples[0])

    # 1. external management_mode cannot claim production release_tier
    external_prod = copy.deepcopy(base_example)
    external_prod["management_mode"] = "external"
    external_prod["release_tier"] = "production"
    with pytest.raises(ValidationError):
        validator.validate(external_prod)

    # 2. external management_mode with experimental release_tier is allowed
    external_exp = copy.deepcopy(base_example)
    external_exp["management_mode"] = "external"
    external_exp["release_tier"] = "experimental"
    validator.validate(external_exp)

    # 3. on_demand loading_policy requires integer idle_reclaim_cooldown_ms >= 1000
    on_demand_missing_cooldown = copy.deepcopy(base_example)
    on_demand_missing_cooldown["loading_policy"] = "on_demand"
    on_demand_missing_cooldown["cancellation_and_fencing"]["idle_reclaim_cooldown_ms"] = None
    with pytest.raises(ValidationError):
        validator.validate(on_demand_missing_cooldown)

    # 4. requires_shared_parent_budget must be true
    unshared_budget = copy.deepcopy(base_example)
    unshared_budget["topology_ref"]["requires_shared_parent_budget"] = False
    with pytest.raises(ValidationError):
        validator.validate(unshared_budget)

    # 5. unknown properties are rejected
    unknown_field = copy.deepcopy(base_example)
    unknown_field["gpu_marketing_name"] = "NVIDIA GB10"
    with pytest.raises(ValidationError):
        validator.validate(unknown_field)


def test_internal_execution_and_runtime_control_openapi_specs_are_valid() -> None:
    exec_doc = _load_yaml(_EXECUTION_CONTROL_OPENAPI_PATH)
    validate(exec_doc)
    assert set(exec_doc["paths"]) == {
        "/internal/v1/executions/submit",
        "/internal/v1/executions/{submit_intent_id}",
        "/internal/v1/executions/{submit_intent_id}/abort",
        "/internal/v1/gateways/{gateway_id}/fencing-barrier",
        "/internal/v1/instances/{instance_id}/drain",
    }

    runtime_doc = _load_yaml(_RUNTIME_CONTROL_OPENAPI_PATH)
    validate(runtime_doc)
    assert set(runtime_doc["paths"]) == {
        "/internal/v1/nodes/{node_id}/instance-intents",
        "/internal/v1/nodes/{node_id}/instance-intents/{intent_id}",
        "/internal/v1/nodes/{node_id}/reconcile",
    }

    for document in (exec_doc, runtime_doc):
        assert document["security"] == [{"ComponentBearer": []}]
        security_scheme = document["components"]["securitySchemes"]["ComponentBearer"]
        assert security_scheme["type"] == "http"
        assert security_scheme["scheme"] == "bearer"
        for path_item in document["paths"].values():
            for method, operation in path_item.items():
                if method in {"get", "post", "put", "patch", "delete"}:
                    assert "401" in operation["responses"], (method, operation["operationId"])


def test_cluster_coordination_sql_draft_defines_required_tables_and_invariants() -> None:
    sql_text = _COORDINATION_SQL_PATH.read_text(encoding="utf-8")
    required_tables = (
        "CREATE TABLE host_nodes",
        "CREATE TABLE resource_pools",
        "CREATE TABLE scheduler_pool_leases",
        "CREATE TABLE engine_instances",
        "CREATE TABLE runtime_instance_intents",
        "CREATE TABLE admission_leases",
        "CREATE TABLE gateway_fencing_barriers",
        "CREATE TABLE submit_intents",
    )
    for table_decl in required_tables:
        assert table_decl in sql_text

    required_constraints = (
        "ck_host_nodes_unified_budget_ceiling",
        "ck_scheduler_pool_leases_active_requires_reconciled",
        "ck_engine_instances_peak_release_requires_ready",
        "ck_runtime_intents_stop_evidence",
        "ck_admission_leases_release_invariant",
        "ck_admission_leases_reservations_non_negative",
        "ck_engine_instances_stopped_evidence",
        "ck_runtime_intents_prepare_evidence",
        "ck_runtime_intents_fencing_positive",
        "fk_runtime_intents_execution_identity",
        "ck_submit_intents_fencing_positive",
        "fk_admission_leases_execution_identity",
        "uq_admission_leases_operation_attempt_stage",
        "fk_submit_intents_lease_stage",
        "fk_submit_intents_lease_execution_identity",
        "uq_submit_intents_operation_attempt_stage",
        "uq_engine_instances_execution_identity",
        "ck_submit_intents_aborted_requires_evidence",
        "ck_submit_intents_quarantine_invariant",
    )
    for constraint_name in required_constraints:
        assert constraint_name in sql_text
