"""Only the exact approved HTTP operation removal is allowed through the gate."""

from __future__ import annotations

import copy
import importlib.util
from pathlib import Path
from types import ModuleType
from typing import Any, cast

ROOT = Path(__file__).resolve().parents[2]
SOURCE = "contracts/admin-openapi.yaml"
PATH = "/v1/nexus/admin/profiles"
POINTER = f"{SOURCE}:paths/{PATH}/get"
BASE_VERSION = "0.1.0.dev6"
CURRENT_VERSION = "0.1.0.dev7"
POLICY_BASE_VERSION = "0.1.0.dev7"
POLICY_CURRENT_VERSION = "0.1.0.dev8"
POLICY_GET_REASON = (
    "No policy table, revision history, or updated_at exists; the operation has "
    "no consumer and could only be implemented by fabricating state."
)
POLICY_PATCH_REASON = (
    "Policy changes have no table, revision history, or updated_at to persist "
    "against; PATCH had no consumer and no durable landing place."
)


def module() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "http_change_cli_removals", ROOT / "scripts/dev-env/check-http-contract-changes.py"
    )
    assert spec is not None and spec.loader is not None
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    return loaded


def document(*, version: str, get: bool = True, post: bool = True) -> dict[str, Any]:
    item: dict[str, Any] = {}
    if get:
        item["get"] = {
            "operationId": "listProfileFamilies",
            "responses": {"200": {"description": "Synthetic"}},
        }
    if post:
        item["post"] = {
            "operationId": "createProfileRevision",
            "responses": {"201": {"description": "Synthetic"}},
        }
    return {
        "openapi": "3.1.0",
        "info": {"title": "Synthetic", "version": version},
        "paths": {PATH: item},
    }


def analyze(base: dict[str, Any], current: dict[str, Any]) -> tuple[list[str], list[str]]:
    loaded = module()
    findings = list(loaded._kernel()._compare_openapi(base, current, SOURCE, standard_only=False))
    return cast(
        tuple[list[str], list[str]],
        loaded._apply_approved_operation_removals(
            SOURCE,
            base,
            current,
            base["info"]["version"],
            current["info"]["version"],
            findings,
        ),
    )


def test_the_allowlist_entry_is_frozen() -> None:
    assert module()._APPROVED_OPERATION_REMOVALS == {
        (
            SOURCE,
            PATH,
            "get",
            BASE_VERSION,
            CURRENT_VERSION,
            "operation-removed",
        ): {
            "issue": "#603",
            "reason": (
                "Unimplemented legacy declaration; the published profile lists v1/v2 "
                "already cover the capability."
            ),
        },
        (
            SOURCE,
            "/v1/nexus/admin/policies",
            "get",
            POLICY_BASE_VERSION,
            POLICY_CURRENT_VERSION,
            "operation-removed",
        ): {"issue": "#605", "reason": POLICY_GET_REASON},
        (
            SOURCE,
            "/v1/nexus/admin/policies/{policy_id}",
            "get",
            POLICY_BASE_VERSION,
            POLICY_CURRENT_VERSION,
            "operation-removed",
        ): {"issue": "#605", "reason": POLICY_GET_REASON},
        (
            SOURCE,
            "/v1/nexus/admin/policies/{policy_id}",
            "patch",
            POLICY_BASE_VERSION,
            POLICY_CURRENT_VERSION,
            "operation-removed",
        ): {"issue": "#605", "reason": POLICY_PATCH_REASON},
    }


def test_the_exact_approved_removal_is_filtered() -> None:
    base = document(version=BASE_VERSION)
    current = document(version=CURRENT_VERSION, get=False)

    findings, approved = analyze(base, current)

    assert findings == []
    assert len(approved) == 1
    assert approved[0].startswith(f"{POINTER}: operation removed")
    assert "#603" in approved[0]


def policy_document() -> dict[str, Any]:
    def operation(name: str) -> dict[str, Any]:
        return {"operationId": name, "responses": {"200": {"description": "Synthetic"}}}

    return {
        "openapi": "3.1.0",
        "info": {"title": "Synthetic", "version": POLICY_BASE_VERSION},
        "paths": {
            "/v1/nexus/admin/policies": {
                "get": operation("listPolicies"),
                "post": operation("createPolicy"),
            },
            "/v1/nexus/admin/policies/{policy_id}": {
                "get": operation("getPolicy"),
                "patch": operation("updatePolicy"),
                "put": operation("replacePolicy"),
            },
        },
    }


def test_the_three_approved_policy_removals_are_filtered() -> None:
    base = policy_document()
    current = copy.deepcopy(base)
    current["info"]["version"] = POLICY_CURRENT_VERSION
    del current["paths"]["/v1/nexus/admin/policies"]["get"]
    del current["paths"]["/v1/nexus/admin/policies/{policy_id}"]["get"]
    del current["paths"]["/v1/nexus/admin/policies/{policy_id}"]["patch"]

    findings, approved = analyze(base, current)

    assert findings == []
    assert len(approved) == 3
    assert all("operation removed" in entry and "#605" in entry for entry in approved)


def test_a_whole_path_removal_is_approved_only_when_every_operation_is_registered() -> None:
    base = policy_document()
    del base["paths"]["/v1/nexus/admin/policies"]["post"]
    del base["paths"]["/v1/nexus/admin/policies/{policy_id}"]["put"]
    current = copy.deepcopy(base)
    current["info"]["version"] = POLICY_CURRENT_VERSION
    current["paths"] = {}

    findings, approved = analyze(base, current)

    assert findings == []
    assert len(approved) == 3
    assert all("operation removed" in entry and "#605" in entry for entry in approved)


def test_a_whole_path_removal_with_an_unregistered_operation_rejects() -> None:
    base = policy_document()
    base["paths"]["/v1/nexus/admin/policies"]["post"]["operationId"] = "createPolicyRevision"
    current = copy.deepcopy(base)
    current["info"]["version"] = POLICY_CURRENT_VERSION
    current["paths"] = {}

    findings, approved = analyze(base, current)

    assert approved == []
    assert f"{SOURCE}:paths//v1/nexus/admin/policies: path removed" in findings
    assert any("every operation must be individually registered" in finding for finding in findings)


def test_an_unregistered_operation_removal_still_rejects() -> None:
    base = document(version=BASE_VERSION)
    current = document(version=CURRENT_VERSION, get=False, post=False)

    findings, approved = analyze(base, current)

    assert len(approved) == 1
    assert findings == [f"{SOURCE}:paths/{PATH}/post: operation removed"]


def test_a_stale_entry_that_did_not_remove_the_operation_rejects() -> None:
    base = document(version=BASE_VERSION)
    current = document(version=CURRENT_VERSION)

    findings, approved = analyze(base, current)

    assert approved == []
    assert len(findings) == 1
    assert "approved operation removal entry does not match" in findings[0]
    assert "still declares this operation" in findings[0]


def test_a_different_removed_method_rejects() -> None:
    base = document(version=BASE_VERSION)
    current = document(version=CURRENT_VERSION, get=True, post=False)

    findings, approved = analyze(base, current)

    assert approved == []
    assert f"{SOURCE}:paths/{PATH}/post: operation removed" in findings
    assert any("still declares this operation" in finding for finding in findings)


def test_a_version_pair_mismatch_leaves_the_removal_unapproved() -> None:
    base = document(version="0.1.0.dev5")
    current = copy.deepcopy(document(version=CURRENT_VERSION, get=False))

    findings, approved = analyze(base, current)

    assert approved == []
    assert findings == [f"{POINTER}: operation removed"]
