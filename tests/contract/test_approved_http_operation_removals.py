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
        }
    }


def test_the_exact_approved_removal_is_filtered() -> None:
    base = document(version=BASE_VERSION)
    current = document(version=CURRENT_VERSION, get=False)

    findings, approved = analyze(base, current)

    assert findings == []
    assert len(approved) == 1
    assert approved[0].startswith(f"{POINTER}: operation removed")
    assert "#603" in approved[0]


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
