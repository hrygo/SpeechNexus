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
def current() -> tuple[dict[str, Any], list[dict[str, Any]]]:
    entries: list[dict[str, Any]] = [
        {"id": "PROGRAM", "number": 1, "kind": "program", "parent": None, "dependencies": []},
        {"id": "GOAL", "number": 2, "kind": "goal", "parent": "PROGRAM", "dependencies": []},
        {"id": "DELIVERY", "number": 3, "kind": "delivery", "parent": "GOAL", "dependencies": []},
    ]
    manifest = {"schema_version": 3, "repository": "owner/repo", "entries": entries}
    issues = [
        {
            "number": e["number"],
            "html_url": f"https://github.com/owner/repo/issues/{e['number']}",
            "repository_url": "https://api.github.com/repos/owner/repo",
            "title": e["id"],
            "state": "open",
            "labels": [{"name": "status:planned"}],
            "body": (
                f"<!-- speechnexus-delivery:{e['id']}:v3 -->\n优先级：P1\n排期：当前\n"
                "交付负责人：待派工\n实现：已实现局部\n集成：待接入\n验证：待验收\n"
                "阻塞：缺少正式输入\n证据：[源码](https://example.com/code)\n"
            ),
        }
        for e in entries
    ]
    return manifest, issues


def test_current_view_works_without_any_history_files(tracker: ModuleType, current: Any) -> None:
    manifest, issues = current
    result = tracker.render(manifest, issues, "离线快照")
    assert "DELIVERY" in result and "缺少正式输入" in result and "源码" in result
    assert "快照时间：未知" in result
    assert all(x not in result for x in ["LEG-", "原始要求对应", "旧 issue 状态", "历史 Gate"])
    issues[2]["body"] = issues[2]["body"].replace("已实现局部", "已完成接线").replace("P1", "P0")
    changed = tracker.render(manifest, issues, "实时读取", "2026-11-03T20:00:00+08:00")
    assert "已完成接线" in changed and "P0" in changed and "已实现局部" not in changed


def test_lf_and_crlf_issue_bodies_render_equivalently(tracker: ModuleType, current: Any) -> None:
    manifest, issues = current
    issues[0]["body"] += "下一队列：先交付当前切片\n"
    expected = tracker.render(manifest, issues, "离线快照")
    assert "先交付当前切片" in expected and "P1 / 当前 / 待派工" in expected
    for issue in issues:
        issue["body"] = issue["body"].replace("\n", "\r\n")
    assert tracker.render(manifest, issues, "离线快照") == expected


@pytest.mark.parametrize(
    "mutation",
    [
        "duplicate_id",
        "duplicate_number",
        "unknown_parent",
        "wrong_parent_kind",
        "unknown_dependency",
        "dependency_cycle",
        "duplicate_dependency",
        "missing_artifact",
        "invalid_number",
        "wrong_kind",
        "legacy_schema",
        "historical_fields",
    ],
)
def test_invalid_registry_is_rejected(tracker: ModuleType, current: Any, mutation: str) -> None:
    manifest, _ = current
    entry = manifest["entries"][2]
    dep = {"id": "GOAL", "artifact": "固定输入", "condition": "接收后接入"}
    if mutation == "duplicate_id":
        entry["id"] = "GOAL"
    elif mutation == "duplicate_number":
        entry["number"] = 1
    elif mutation == "unknown_parent":
        entry["parent"] = "UNKNOWN"
    elif mutation == "wrong_parent_kind":
        entry["parent"] = "PROGRAM"
    elif mutation == "unknown_dependency":
        entry["dependencies"] = [{**dep, "id": "UNKNOWN"}]
    elif mutation == "dependency_cycle":
        entry["dependencies"] = [{**dep, "id": "DELIVERY"}]
    elif mutation == "duplicate_dependency":
        entry["dependencies"] = [dep, dep]
    elif mutation == "missing_artifact":
        entry["dependencies"] = [{**dep, "artifact": ""}]
    elif mutation == "invalid_number":
        entry["number"] = -1
    elif mutation == "wrong_kind":
        entry["kind"] = "legacy-gate"
    elif mutation == "legacy_schema":
        manifest["schema_version"] = 2
    else:
        manifest["requirements"] = [{"id": "LEG-0001-AC-01"}]
    with pytest.raises(ValueError):
        tracker.validate_manifest(manifest)


@pytest.mark.parametrize(
    "mutation",
    [
        "missing_tracked",
        "wrong_url",
        "wrong_repository",
        "missing_url",
        "wrong_marker",
        "duplicate_marker",
        "duplicate_number",
        "extra_wrong_source",
    ],
)
def test_wrong_or_incomplete_remote_is_rejected(
    tracker: ModuleType, current: Any, mutation: str
) -> None:
    manifest, issues = current
    if mutation == "missing_tracked":
        issues.pop()
    elif mutation == "wrong_url":
        issues[2]["html_url"] = "https://github.com/other/repo/issues/3"
    elif mutation == "wrong_repository":
        issues[2]["repository_url"] = "https://api.github.com/repos/other/repo"
    elif mutation == "missing_url":
        issues[2].pop("html_url")
    elif mutation == "wrong_marker":
        issues[2]["body"] = "<!-- speechnexus-delivery:WRONG:v3 -->"
    elif mutation == "duplicate_marker":
        issues[2]["body"] += "<!-- speechnexus-delivery:DELIVERY:v3 -->"
    elif mutation == "duplicate_number":
        issues.append(copy.deepcopy(issues[2]))
    else:
        issues.append(
            {"number": 99, "html_url": "https://github.com/other/repo/issues/99", "state": "open"}
        )
    with pytest.raises(ValueError):
        tracker.render(manifest, issues, "离线快照")


def test_new_open_issue_is_visible_as_missing_coverage(tracker: ModuleType, current: Any) -> None:
    manifest, issues = current
    issues.append(
        {
            "number": 99,
            "html_url": "https://github.com/owner/repo/issues/99",
            "title": "新发现",
            "state": "open",
            "labels": [],
        }
    )
    result = tracker.render(manifest, issues, "离线快照")
    assert "覆盖待补齐" in result and "新发现" in result and "#99" in result


@pytest.mark.parametrize(
    "labels",
    [[], [{"name": "status:done"}], [{"name": "status:ready"}, {"name": "status:planned"}]],
)
def test_conflicting_status_and_missing_fields_are_unknown(
    tracker: ModuleType, current: Any, labels: Any
) -> None:
    manifest, issues = current
    issues[2]["labels"] = labels
    issues[2]["body"] += "优先级：P2\n"
    result = tracker.render(manifest, issues, "离线快照")
    row = next(line for line in result.splitlines() if line.startswith("| [DELIVERY"))
    assert "未知" in row


def test_reorganization_close_is_not_completion(tracker: ModuleType, current: Any) -> None:
    manifest, issues = current
    issues[2].update(state="closed", state_reason="not_planned")
    result = tracker.render(manifest, issues, "离线快照")
    assert "not_planned" in result and "不计完成" in result


def test_cli_needs_only_current_registry_and_snapshot(tmp_path: Path, current: Any) -> None:
    manifest, issues = current
    registry = tmp_path / "issues.json"
    snapshot = tmp_path / "snapshot.json"
    output = tmp_path / "matrix.md"
    registry.write_text(json.dumps(manifest))
    snapshot.write_text(json.dumps(issues))
    result = subprocess.run(
        [
            sys.executable,
            str(CLI),
            "--manifest",
            str(registry),
            "--snapshot",
            str(snapshot),
            "--output",
            str(output),
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert "快照时间：未知" in output.read_text()
    assert not any(p.name in {"source.json", "delivery-baseline.json"} for p in tmp_path.iterdir())


def test_validation_or_network_failure_preserves_previous_output(
    tmp_path: Path,
    current: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest, issues = current
    registry = tmp_path / "issues.json"
    output = tmp_path / "matrix.md"
    registry.write_text(json.dumps(manifest))
    output.write_text("previous")
    module = load_module(CLI)
    monkeypatch.setattr(
        sys, "argv", [str(CLI), "--manifest", str(registry), "--output", str(output)]
    )

    def unavailable(*_args: Any) -> Any:
        raise subprocess.CalledProcessError(1, ["gh", "api"], stderr="unavailable")

    monkeypatch.setattr(module, "load_issues", unavailable)
    with pytest.raises(SystemExit):
        module.main()
    assert output.read_text() == "previous"
    issues.pop()
    monkeypatch.setattr(module, "load_issues", lambda *_args: issues)
    with pytest.raises((ValueError, SystemExit)):
        module.main()
    assert output.read_text() == "previous"


def test_live_reads_open_and_registered_closed_only(
    current: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest, issues = current
    module = load_module(CLI)
    calls: list[str] = []

    def response(command: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        path = command[2]
        calls.append(path)
        if "?state=open" in path:
            result: Any = [*issues[:2], {"number": 99, "pull_request": {}}]
        else:
            assert path.endswith("/issues/3")
            result = {**issues[2], "state": "closed", "state_reason": "not_planned"}
        return subprocess.CompletedProcess(command, 0, stdout=json.dumps(result))

    monkeypatch.setattr(module.subprocess, "run", response)
    found = module.load_issues(manifest["repository"], None, [1, 2, 3])
    assert [issue["number"] for issue in found] == [1, 2, 3]
    assert calls == [
        "repos/owner/repo/issues?state=open&per_page=100&page=1",
        "repos/owner/repo/issues/3",
    ]
