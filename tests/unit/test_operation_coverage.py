"""Operation coverage must fail when a binding, consumer or test leg disappears."""

from __future__ import annotations

import copy
import importlib.util
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "operation_coverage", ROOT / "scripts/dev-env/check-operation-coverage.py"
)
assert SPEC is not None and SPEC.loader is not None


def load() -> Any:
    assert SPEC is not None and SPEC.loader is not None
    module: Any = importlib.util.module_from_spec(SPEC)
    SPEC.loader.exec_module(module)
    return module


def operation(identifier: str = "getProject", **overrides: Any) -> dict[str, Any]:
    value: dict[str, Any] = {
        "id": identifier,
        "method": "get",
        "path": "/v1/workbench/projects/{project_id}",
        "binding": {
            "path_entry": True,
            "http_operation_union": True,
            "python_http_schema": True,
        },
        "consumers": ["web/workbench/src/manuscripts/workbench-client.ts"],
        "tests": ["web/workbench/src/manuscripts/workbench-client.test.ts"],
        "status": "bound",
    }
    value.update(overrides)
    return value


def derived(*operations: dict[str, Any]) -> dict[str, Any]:
    return {
        "version": "1.0.0",
        "scope": "fixture",
        "contract": "contracts/workbench-openapi.yaml",
        "contract_sha256": "0" * 64,
        "operations": list(operations),
    }


def committed(*operations: dict[str, Any], digest: str = "0" * 64) -> dict[str, Any]:
    value = derived(*operations)
    value["contract_sha256"] = digest
    return value


def codes(module: Any, reviewed: dict[str, Any], fresh: dict[str, Any]) -> set[str]:
    return {finding["code"] for finding in module.differences(reviewed, fresh)}


def test_committed_map_is_current() -> None:
    module = load()
    reviewed = module.json.loads(module.DEFAULT_MAP.read_text())
    assert module.differences(reviewed, module.derive(module.ROOT)) == []


def test_declared_operation_without_mapping_fails() -> None:
    module = load()
    reviewed = committed(operation())
    fresh = derived(operation(), operation("getChapter", path="/v1/workbench/chapters/{id}"))
    assert "operation_missing" in codes(module, reviewed, fresh)


def test_mapping_for_undeclared_operation_fails() -> None:
    module = load()
    reviewed = committed(operation(), operation("retiredProject"))
    assert "operation_extra" in codes(module, reviewed, derived(operation()))


def test_removed_consumer_is_detected() -> None:
    module = load()
    reviewed = committed(operation(consumers=[]))
    assert "consumers_drift" in codes(module, reviewed, derived(operation()))


def test_removed_test_is_detected() -> None:
    module = load()
    reviewed = committed(operation(tests=[]))
    assert "tests_drift" in codes(module, reviewed, derived(operation()))


def test_binding_flag_drift_is_detected() -> None:
    module = load()
    stale = operation()
    stale["binding"]["path_entry"] = False
    assert "binding_drift" in codes(module, committed(stale), derived(operation()))


def test_consumer_present_makes_registration_stale() -> None:
    module = load()
    registered = operation(
        status="consumer_pending",
        consumers=[],
        pending={
            "owner": "C",
            "artifact": "later",
            "unblock_condition": "later",
            "impact": "later",
        },
    )
    assert "stale_registration" in codes(module, committed(registered), derived(operation()))


def test_bound_without_consumer_fails() -> None:
    module = load()
    unbound = operation(consumers=[], status="bound")
    fresh = derived(operation(consumers=[], status="consumer_pending"))
    assert "consumer_missing" in codes(module, committed(unbound), fresh)


@pytest.mark.parametrize("missing", ["owner", "artifact", "unblock_condition", "impact"])
def test_incomplete_registration_fails(missing: str) -> None:
    module = load()
    pending = {
        "owner": "C",
        "artifact": "later",
        "unblock_condition": "later",
        "impact": "later",
    }
    del pending[missing]
    registered = operation(status="consumer_pending", consumers=[], pending=pending)
    fresh = derived(operation(consumers=[], status="consumer_pending"))
    assert "registration_incomplete" in codes(module, committed(registered), fresh)


def test_contract_digest_change_fails() -> None:
    module = load()
    reviewed = committed(operation(), digest="f" * 64)
    assert "contract_drift" in codes(module, reviewed, derived(operation()))


def test_write_preserves_registrations(tmp_path: Path) -> None:
    module = load()
    existing = committed(
        operation(
            "getChapter",
            status="consumer_pending",
            consumers=[],
            pending={
                "owner": "C",
                "artifact": "artifact",
                "unblock_condition": "condition",
                "impact": "impact",
            },
        )
    )
    target = tmp_path / "map.json"
    target.write_text(module.json.dumps(existing))
    fresh = module.merge_registrations(
        derived(operation("getChapter", status="consumer_pending", consumers=[])),
        copy.deepcopy(existing),
    )
    assert fresh["operations"][0]["pending"]["owner"] == "C"


def test_spy_target_method_name_counts_as_behavior_test(tmp_path: Path) -> None:
    module = load()
    source = tmp_path / "web/workbench/src/assistant/assistant-session.test.ts"
    source.parent.mkdir(parents=True)
    source.write_text('vi.spyOn(client, "assistantTurn").mockResolvedValue(turn());')
    operation = {
        "id": "getAssistantTurn",
        "method": "get",
        "path": "/v1/workbench/assistant-turns/{turn_id}",
    }
    pattern = module.test_pattern(
        operation, module.client_methods(module.ROOT).get(("GET", operation["path"]))
    )
    assert pattern.search(source.read_text())
