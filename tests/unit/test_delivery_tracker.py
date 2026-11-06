from __future__ import annotations

import copy
import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
HELPER = ROOT / "scripts/dev-env/delivery_tracker.py"
CLI = HELPER.with_name("refresh-implementation-tracker.py")


def load_module(path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(path.stem, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def tracker() -> ModuleType:
    return load_module(HELPER)


@pytest.fixture
def current() -> tuple[dict[str, Any], list[dict[str, Any]], str]:
    epics = [
        {"number": n, "title": f"能力 {n}", "scope": "本域交付", "exclusions": "其他域交付"}
        for n in range(337, 347)
    ]
    requirements = [
        {
            "id": f"REQ-{n}",
            "epic": n,
            "prd": f"docs/prd.md#req-{n}",
            "designs": ["docs/design.md"],
            "implementation": "组件已有实现",
            "integration": "尚待正式接线",
            "verification": "本域 AC 待验证",
            "gap": "需要真实消费证据",
            "evidence": [{"url": "https://example.com/proof", "label": "依据", "note": "静态证据"}],
            "issues": [],
        }
        for n in range(337, 347)
    ]
    manifest = {
        "schema_version": 4,
        "repository": "owner/repo",
        "program": 336,
        "facts_as_of": "2026-11-03",
        "epics": epics,
        "requirements": requirements,
    }
    issues = [
        {
            "number": n,
            "html_url": f"https://github.com/owner/repo/issues/{n}",
            "title": "总控" if n == 336 else f"能力 {n}",
            "state": "open",
            "state_reason": None,
            "labels": [{"name": "type:program" if n == 336 else "type:epic"}],
            "parent_issue_url": None
            if n == 336
            else "https://api.github.com/repos/owner/repo/issues/336",
            "body": "目标与范围\n当前事实",
        }
        for n in range(336, 347)
    ]
    prd = "\n".join(
        f'## Epic {n} · 能力 {n}\n<a id="req-{n}"></a>\n'
        f"### REQ-{n} · 产品能力 {n}\n可观察的产品结果。\n"
        for n in range(337, 347)
    )
    return manifest, issues, prd


def test_all_requirements_remain_visible_without_implementation_issues(
    tracker: ModuleType, current: Any
) -> None:
    manifest, issues, prd = current
    result = tracker.render(manifest, issues, "离线快照", prd)
    assert "产品能力 337" in result and "产品能力 346" in result
    assert "未建卡" in result and "需要真实消费证据" in result
    assert "快照时间：未知" in result
    assert all(word not in result for word in ["LEG-", "历史映射", "原始卡号"])
    issues[1]["title"] = "最新主题标题"
    assert "最新主题标题" in tracker.render(manifest, issues, "离线快照", prd)


def test_issue_cannot_be_owned_by_two_requirements_in_same_epic(
    tracker: ModuleType, current: Any
) -> None:
    manifest, _, _ = current
    manifest["requirements"][0]["issues"] = [500]
    same_epic_requirement = copy.deepcopy(manifest["requirements"][0])
    same_epic_requirement["id"] = "REQ-337-A"
    same_epic_requirement["prd"] = "docs/prd.md#req-337-a"
    same_epic_requirement["issues"] = [500]
    manifest["requirements"].append(same_epic_requirement)

    with pytest.raises(
        ValueError,
        match=r"Implementation issue #500 .*REQ-337, REQ-337-A",
    ):
        tracker.validate_manifest(manifest)


@pytest.mark.parametrize(
    "mutation",
    [
        "missing_epic",
        "extra_epic",
        "duplicate_epic",
        "duplicate_requirement",
        "unknown_owner",
        "two_owners",
        "uncovered_epic",
        "legacy_schema",
        "history_fields",
        "empty_design",
        "invalid_issue",
        "duplicate_work_issue",
        "absolute_reference",
        "escaping_reference",
    ],
)
def test_invalid_requirement_registry_is_rejected(
    tracker: ModuleType, current: Any, mutation: str
) -> None:
    manifest, _, _ = current
    req = manifest["requirements"][0]
    if mutation == "missing_epic":
        manifest["epics"].pop()
    elif mutation == "extra_epic":
        manifest["epics"].append({**manifest["epics"][0], "number": 347})
    elif mutation == "duplicate_epic":
        manifest["epics"][1]["number"] = 337
    elif mutation == "duplicate_requirement":
        manifest["requirements"][1]["id"] = req["id"]
    elif mutation == "unknown_owner":
        req["epic"] = 999
    elif mutation == "two_owners":
        req["epic"] = [337, 338]
    elif mutation == "uncovered_epic":
        req["epic"] = 338
    elif mutation == "legacy_schema":
        manifest["schema_version"] = 3
    elif mutation == "history_fields":
        manifest["history"] = []
    elif mutation == "empty_design":
        req["designs"] = []
    elif mutation == "invalid_issue":
        req["issues"] = [True]
    elif mutation == "duplicate_work_issue":
        req["issues"] = [500, 500]
    elif mutation == "absolute_reference":
        req["designs"] = ["/private/document.md"]
    else:
        req["designs"] = ["../../outside.md"]
    with pytest.raises(ValueError):
        tracker.validate_manifest(manifest)


@pytest.mark.parametrize(
    "mutation",
    ["missing_epic", "wrong_url", "duplicate_number", "epic_parent", "work_parent", "missing_type"],
)
def test_remote_source_and_native_hierarchy_are_verified(
    tracker: ModuleType, current: Any, mutation: str
) -> None:
    manifest, issues, prd = current
    if mutation == "missing_epic":
        issues.pop()
    elif mutation == "wrong_url":
        issues[1]["html_url"] = "https://github.com/other/repo/issues/337"
    elif mutation == "duplicate_number":
        issues.append(copy.deepcopy(issues[1]))
    elif mutation == "epic_parent":
        issues[1]["parent_issue_url"] = "https://api.github.com/repos/owner/repo/issues/338"
    elif mutation == "missing_type":
        issues[1]["labels"] = []
    else:
        manifest["requirements"][0]["issues"] = [500]
        issues.append(
            {
                "number": 500,
                "html_url": "https://github.com/owner/repo/issues/500",
                "title": "实施",
                "state": "open",
                "parent_issue_url": "https://api.github.com/repos/owner/repo/issues/338",
            }
        )
    with pytest.raises(ValueError):
        tracker.render(manifest, issues, "离线快照", prd)


def test_prd_coverage_cannot_hide_missing_or_extra_requirements(
    tracker: ModuleType, current: Any
) -> None:
    manifest, issues, prd = current
    for changed in [
        prd.replace("### REQ-337 ·", "### NEW-337 ·"),
        prd + "\n### NEW-999 · 新需求\n",
        prd + "\n### REQ-337 · 重复需求\n",
    ]:
        with pytest.raises(ValueError):
            tracker.render(manifest, issues, "离线快照", changed)


def test_closed_task_does_not_complete_the_product_requirement(
    tracker: ModuleType, current: Any
) -> None:
    manifest, issues, prd = current
    manifest["requirements"][0]["issues"] = [500]
    issues.append(
        {
            "number": 500,
            "html_url": "https://github.com/owner/repo/issues/500",
            "title": "旧实施卡",
            "state": "closed",
            "state_reason": "not_planned",
            "parent_issue_url": "https://api.github.com/repos/owner/repo/issues/337",
        }
    )
    result = tracker.render(manifest, issues, "离线快照", prd)
    assert "不计完成" in result and "本域 AC 待验证" in result


def test_unregistered_and_orphan_open_issue_remains_visible(
    tracker: ModuleType, current: Any
) -> None:
    manifest, issues, prd = current
    issues.append(
        {
            "number": 500,
            "html_url": "https://github.com/owner/repo/issues/500",
            "title": "未关联需求",
            "state": "open",
        }
    )
    result = tracker.render(manifest, issues, "离线快照", prd)
    assert "未登记" in result and "父 Epic 未核实" in result and "#500" in result


def test_newline_style_does_not_change_the_view(tracker: ModuleType, current: Any) -> None:
    manifest, issues, prd = current
    expected = tracker.render(manifest, issues, "离线快照", prd)
    for issue in issues:
        issue["body"] = issue["body"].replace("\n", "\r\n")
    assert tracker.render(manifest, issues, "离线快照", prd.replace("\n", "\r\n")) == expected


def test_prd_section_must_match_the_primary_epic(tracker: ModuleType, current: Any) -> None:
    manifest, issues, prd = current
    moved = prd.replace("### REQ-337", "### TEMP")
    moved = moved.replace("### REQ-338", "### REQ-337").replace("### TEMP", "### REQ-338")
    with pytest.raises(ValueError, match="PRD epic ownership mismatch"):
        tracker.render(manifest, issues, "离线快照", moved)


def test_document_targets_and_explicit_requirement_anchors_are_checked(
    tracker: ModuleType, current: Any, tmp_path: Path
) -> None:
    manifest, _, prd = current
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs/prd.md").write_text(prd)
    (tmp_path / "docs/design.md").write_text("# Design\n")
    assert tracker.validate_documents(manifest, tmp_path) == prd
    (tmp_path / "docs/prd.md").write_text(prd.replace('id="req-337"', 'id="wrong"'))
    with pytest.raises(ValueError):
        tracker.validate_documents(manifest, tmp_path)


def test_live_reads_open_and_explicitly_registered_closed_only(
    current: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, issues, _ = current
    cli = load_module(CLI)
    calls = []

    def response(command: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        path = command[2]
        calls.append(path)
        payload: Any = [*issues, {"number": 999, "pull_request": {}}]
        if "?state=open" not in path:
            assert path.endswith("/issues/500")
            payload = {"number": 500, "state": "closed"}
        return subprocess.CompletedProcess(command, 0, stdout=json.dumps(payload))

    monkeypatch.setattr(cli.subprocess, "run", response)
    found = cli.load_issues("owner/repo", None, [336, 500])
    assert len(found) == 12
    assert calls == [
        "repos/owner/repo/issues?state=open&per_page=100&page=1",
        "repos/owner/repo/issues/500",
    ]


def test_cli_validation_or_network_failure_preserves_previous_output(
    current: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest, issues, prd = current
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs/prd.md").write_text(prd)
    (tmp_path / "docs/design.md").write_text("# Design\n")
    registry = tmp_path / "registry.json"
    registry.write_text(json.dumps(manifest))
    output = tmp_path / "view.md"
    output.write_text("previous")
    cli = load_module(CLI)
    monkeypatch.setattr(cli, "ROOT", tmp_path)
    monkeypatch.setattr(
        sys, "argv", [str(CLI), "--manifest", str(registry), "--output", str(output)]
    )

    def unavailable(*_args: Any) -> Any:
        raise subprocess.CalledProcessError(1, ["gh", "api"], stderr="unavailable")

    monkeypatch.setattr(cli, "load_issues", unavailable)
    with pytest.raises(SystemExit):
        cli.main()
    assert output.read_text() == "previous"
    issues[1]["parent_issue_url"] = None
    monkeypatch.setattr(cli, "load_issues", lambda *_args: issues)
    with pytest.raises(ValueError):
        cli.main()
    assert output.read_text() == "previous"
