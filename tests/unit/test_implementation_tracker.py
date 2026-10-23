"""The tracker must not turn unrelated or premature closures into progress."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def tracker() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "implementation_tracker", ROOT / "scripts/dev-env/refresh-implementation-tracker.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def manifest() -> dict[str, Any]:
    data: dict[str, Any] = json.loads(
        (ROOT / "docs/plans/2026-10-23-implementation-issues.json").read_text()
    )
    return data


def issues_for(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "number": entry["number"],
            "repository_url": f"https://api.github.com/repos/{manifest['repository']}",
            "html_url": entry["url"],
            "state": "open",
            "state_reason": None,
            "labels": [{"name": "status:planned"}],
        }
        for entry in manifest["entries"]
    ]


def close_issue(manifest: dict[str, Any], issues: list[dict[str, Any]], ident: str) -> None:
    number = next(entry["number"] for entry in manifest["entries"] if entry["id"] == ident)
    issue = next(issue for issue in issues if issue["number"] == number)
    issue.update(state="closed", state_reason="completed")


@pytest.mark.parametrize("origin", ["wrong_repository", "missing", "conflicting_url"])
def test_rejects_snapshot_without_matching_source(
    tracker: ModuleType, manifest: dict[str, Any], origin: str
) -> None:
    issues = issues_for(manifest)
    for issue in issues:
        issue.update(state="closed", state_reason="completed")
        if origin == "wrong_repository":
            issue["repository_url"] = "https://api.github.com/repos/example/unrelated"
            issue["html_url"] = f"https://github.com/example/unrelated/issues/{issue['number']}"
        elif origin == "missing":
            del issue["repository_url"], issue["html_url"]
        else:
            issue["html_url"] = "https://github.com/example/unrelated/issues/1"
    with pytest.raises(ValueError, match=r"source missing|repository mismatch|URL mismatch"):
        tracker.render(manifest, issues, "test snapshot")


@pytest.mark.parametrize(
    "labels",
    [["ready"], ["status:ready", "status:typo"], ["status:done"]],
)
def test_rejects_ambiguous_or_unscoped_open_status(tracker: ModuleType, labels: list[str]) -> None:
    issue = {"state": "open", "labels": [{"name": label} for label in labels]}
    assert "状态待核对" in tracker.status(issue)


def test_gate_requires_completed_prerequisites(
    tracker: ModuleType, manifest: dict[str, Any]
) -> None:
    issues = issues_for(manifest)
    close_issue(manifest, issues, "G3")
    body = tracker.render(manifest, issues, "test snapshot")
    assert "验收 Gate 通过：0/6" in body
    assert "已关闭待核对（前置未通过；不计 Gate 通过）" in body
    for ident in ("A00", "B00", "C00", "G0"):
        close_issue(manifest, issues, ident)
    body = tracker.render(manifest, issues, "test snapshot")
    assert "工作包完成：3/25" in body
    assert "验收 Gate 通过：1/6" in body


def test_non_completion_closure_is_not_delivery(
    tracker: ModuleType, manifest: dict[str, Any]
) -> None:
    issues = issues_for(manifest)
    for ident in ("A00", "B00", "C00", "G0"):
        close_issue(manifest, issues, ident)
    number = next(entry["number"] for entry in manifest["entries"] if entry["id"] == "A00")
    next(issue for issue in issues if issue["number"] == number)["state_reason"] = "not_planned"
    body = tracker.render(manifest, issues, "test snapshot")
    assert "工作包完成：2/25" in body
    assert "验收 Gate 通过：0/6" in body


def test_rejects_blocking_cycle_and_invalid_delivery_reference(
    tracker: ModuleType, manifest: dict[str, Any]
) -> None:
    gate = next(entry for entry in manifest["entries"] if entry["id"] == "G0")
    gate["native_blocked_by"] = ["G1"]
    with pytest.raises(ValueError, match="cycle"):
        tracker.validate_manifest(manifest)
    gate["native_blocked_by"] = ["A00", "B00", "C00"]
    manifest["delivery_coverage"][0]["packages"] = ["G0"]
    with pytest.raises(ValueError, match="Invalid work-package coverage"):
        tracker.validate_manifest(manifest)
