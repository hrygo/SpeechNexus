"""Explicit aliases retain shared schemas and cannot fetch unreviewed sources."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType
from typing import Any, cast

import pytest
from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parents[2]


def generator() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "contract_binding_generator", ROOT / "scripts/dev-env/generate-contract-bindings.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def manifest(reference: str) -> dict[str, Any]:
    return {
        "version": "synthetic",
        "source": "workbench-openapi.yaml",
        "schemas": ["DeletionPlanRequest"],
        "schema_refs": {"DeletionPlanRequest": reference},
    }


def test_external_entrypoint_retains_shared_owner_and_revision_rules() -> None:
    bundle = cast(Any, generator().bundle_contracts)(
        manifest("shared/deletion.schema.json#/$defs/DeletionPlanRequest")
    )
    schema = bundle["$defs"]["DeletionPlanRequest"]
    assert schema["$ref"].startswith("#/$defs/External_shared_deletion_schema_json_")
    validator = Draft202012Validator({**bundle, "$ref": "#/$defs/DeletionPlanRequest"})
    target = {
        "owner": "workbench",
        "kind": "project",
        "resource": {"id": "project-demo", "revision": "1"},
    }
    assert validator.is_valid({"target": target, "mode": "ordinary"})
    assert not validator.is_valid({"target": target | {"owner": "speech"}, "mode": "ordinary"})
    assert not validator.is_valid(
        {"target": target | {"resource": {"id": "project-demo", "revision": 1}}, "mode": "ordinary"}
    )


@pytest.mark.parametrize(
    "reference",
    [
        "https://unreviewed.invalid/schema#/$defs/DeletionPlanRequest",
        "file:///tmp/unreviewed.json#/$defs/DeletionPlanRequest",
        "../../AGENTS.md#/anything",
        "shared/deletion.schema.json",
        "shared/deletion.schema.json#not-a-pointer",
    ],
)
def test_entrypoint_never_fetches_remote_or_outside_contract_tree(reference: str) -> None:
    with pytest.raises(ValueError):
        cast(Any, generator().bundle_contracts)(manifest(reference))


def test_undeclared_alias_is_rejected_instead_of_silently_ignored() -> None:
    config = manifest("shared/deletion.schema.json#/$defs/DeletionPlanRequest")
    config["schemas"] = ["ProjectCreateRequest"]
    with pytest.raises(ValueError, match="declared schema"):
        cast(Any, generator().bundle_contracts)(config)
