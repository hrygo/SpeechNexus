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


def prepare_cli_tree(root: Path, manifest: dict[str, Any], prd: str) -> tuple[Path, Path]:
    (root / "docs").mkdir()
    (root / "docs/prd.md").write_text(prd, encoding="utf-8")
    (root / "docs/design.md").write_text("# Design\n", encoding="utf-8")
    registry = root / "registry.json"
    registry.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    return registry, root / "view.md"


def reject_wall_clock() -> str:
    raise AssertionError("snapshot rendering must not read the wall clock")


def test_snapshot_render_is_byte_reproducible_without_a_clock(
    current: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest, issues, prd = current
    registry, output = prepare_cli_tree(tmp_path, manifest, prd)
    snapshot = tmp_path / "snapshot.json"
    cli = load_module(CLI)
    monkeypatch.setattr(cli, "ROOT", tmp_path)
    monkeypatch.setattr(cli, "current_time", reject_wall_clock)
    cli.write_snapshot(snapshot, manifest["repository"], issues, "2026-11-07T09:00:00+08:00")
    rendered = []
    for _ in range(2):
        monkeypatch.setattr(
            sys,
            "argv",
            [
                str(CLI),
                "--manifest",
                str(registry),
                "--snapshot",
                str(snapshot),
                "--output",
                str(output),
            ],
        )
        cli.main()
        rendered.append(output.read_bytes())
    assert rendered[0] == rendered[1]
    assert "快照时间：2026-11-07T09:00:00+08:00" in rendered[0].decode("utf-8")


def test_refresh_snapshot_then_offline_render_is_stable(
    current: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest, issues, prd = current
    registry, live_output = prepare_cli_tree(tmp_path, manifest, prd)
    snapshot = tmp_path / "github-issues-snapshot.json"
    offline_output = tmp_path / "offline.md"
    cli = load_module(CLI)
    monkeypatch.setattr(cli, "ROOT", tmp_path)
    monkeypatch.setattr(cli, "load_issues", lambda *_args, **_kwargs: copy.deepcopy(issues))
    monkeypatch.setattr(
        sys,
        "argv",
        [
            str(CLI),
            "--manifest",
            str(registry),
            "--refresh-snapshot",
            str(snapshot),
            "--snapshot-at",
            "2026-11-07T09:00:00+08:00",
            "--output",
            str(live_output),
        ],
    )
    cli.main()
    live_bytes = live_output.read_bytes()

    def offline_only(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("offline rendering must not read GitHub")

    monkeypatch.setattr(cli, "load_issues", offline_only)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            str(CLI),
            "--manifest",
            str(registry),
            "--snapshot",
            str(snapshot),
            "--output",
            str(offline_output),
        ],
    )
    cli.main()
    assert live_bytes == offline_output.read_bytes()

    payload = json.loads(snapshot.read_text(encoding="utf-8"))
    assert payload["schema_version"] == 1
    assert payload["repository"] == manifest["repository"]
    assert payload["fetched_at"] == "2026-11-07T09:00:00+08:00"
    numbers = [record["number"] for record in payload["issues"]]
    assert numbers == sorted(numbers)
    assert all(set(record) == set(cli.SNAPSHOT_FIELDS) for record in payload["issues"])
    assert all("body" not in record for record in payload["issues"])


def test_raw_snapshot_without_a_fixed_time_is_rejected(
    current: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest, issues, prd = current
    registry, output = prepare_cli_tree(tmp_path, manifest, prd)
    snapshot = tmp_path / "raw.json"
    snapshot.write_text(json.dumps(issues), encoding="utf-8")
    output.write_text("previous", encoding="utf-8")
    cli = load_module(CLI)
    monkeypatch.setattr(cli, "ROOT", tmp_path)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            str(CLI),
            "--manifest",
            str(registry),
            "--snapshot",
            str(snapshot),
            "--output",
            str(output),
        ],
    )
    with pytest.raises(ValueError, match="--snapshot-at"):
        cli.main()
    assert output.read_text(encoding="utf-8") == "previous"


@pytest.mark.parametrize(
    "mutation", ["schema", "repository", "missing_time", "naive_time", "foreign_field"]
)
def test_invalid_snapshot_metadata_is_rejected(
    current: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    manifest, issues, prd = current
    registry, output = prepare_cli_tree(tmp_path, manifest, prd)
    snapshot = tmp_path / "snapshot.json"
    cli = load_module(CLI)
    monkeypatch.setattr(cli, "ROOT", tmp_path)
    cli.write_snapshot(snapshot, manifest["repository"], issues, "2026-11-07T09:00:00+08:00")
    payload = json.loads(snapshot.read_text(encoding="utf-8"))
    if mutation == "schema":
        payload["schema_version"] = 2
    elif mutation == "repository":
        payload["repository"] = "other/repo"
    elif mutation == "missing_time":
        payload["fetched_at"] = None
    elif mutation == "naive_time":
        payload["fetched_at"] = "2026-11-07T09:00:00"
    else:
        payload["unexpected"] = True
    snapshot.write_text(json.dumps(payload), encoding="utf-8")
    output.write_text("previous", encoding="utf-8")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            str(CLI),
            "--manifest",
            str(registry),
            "--snapshot",
            str(snapshot),
            "--output",
            str(output),
        ],
    )
    with pytest.raises(ValueError):
        cli.main()
    assert output.read_text(encoding="utf-8") == "previous"


def test_snapshot_at_overrides_the_recorded_time(
    current: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest, issues, prd = current
    registry, output = prepare_cli_tree(tmp_path, manifest, prd)
    snapshot = tmp_path / "snapshot.json"
    cli = load_module(CLI)
    monkeypatch.setattr(cli, "ROOT", tmp_path)
    cli.write_snapshot(snapshot, manifest["repository"], issues, "2026-11-01T00:00:00+08:00")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            str(CLI),
            "--manifest",
            str(registry),
            "--snapshot",
            str(snapshot),
            "--snapshot-at",
            "2026-11-02T03:04:05+08:00",
            "--output",
            str(output),
        ],
    )
    cli.main()
    assert "快照时间：2026-11-02T03:04:05+08:00" in output.read_text(encoding="utf-8")
