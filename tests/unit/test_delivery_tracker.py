from __future__ import annotations

import copy
import hashlib
import importlib.util
from pathlib import Path
from typing import Any

import pytest

PATH = Path(__file__).resolve().parents[2] / "scripts/dev-env/delivery_tracker.py"
spec = importlib.util.spec_from_file_location("delivery_tracker", PATH)
assert spec and spec.loader
tracker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tracker)


def fixture() -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
    body = "## 目标\n\n完成任务。\n\n## 验收标准\n\n- [ ] 可验证\n"
    repo = "owner/repo"
    source: dict[str, Any] = {
        "schema_version": 1,
        "repository": repo,
        "captured_at": "2026-11-03T00:00:00Z",
        "base_sha": "abc",
        "old_open_numbers": [1],
        "sources": [
            {
                "number": 1,
                "title": "旧卡",
                "body": body,
                "body_sha256": hashlib.sha256(body.encode()).hexdigest(),
                "url": "https://github.com/owner/repo/issues/1",
                "updated_at": "2026-11-03T00:00:00Z",
                "comments": [],
            }
        ],
    }
    requirements = tracker.extract_requirements(source)
    for item in requirements:
        item.update(primary="D1", targets=["D1"])
    entry: dict[str, Any] = {
        "id": "D1",
        "kind": "delivery",
        "title": "交付",
        "parent": "G1",
        "number": 2,
        "url": "https://github.com/owner/repo/issues/2",
        "priority": "P0",
        "stage": "一期",
        "owner": "team",
        "initial_progress": {"implemented": "历史", "integrated": "无", "verified": "未验证"},
        "evidence": [],
        "dependencies": [],
    }
    program = {
        **entry,
        "id": "PROGRAM",
        "kind": "program",
        "parent": None,
        "number": 3,
        "url": "https://github.com/owner/repo/issues/3",
    }
    goal = {
        **entry,
        "id": "G1",
        "kind": "goal",
        "parent": "PROGRAM",
        "number": 4,
        "url": "https://github.com/owner/repo/issues/4",
    }
    manifest = {
        "schema_version": 2,
        "repository": repo,
        "baseline_sha": "abc",
        "baseline_at": "2026-11-03T00:00:00Z",
        "migration_source": "source.json",
        "entries": [entry, program, goal],
        "requirements": requirements,
        "legacy_issues": [
            {
                "number": 1,
                "url": "https://github.com/owner/repo/issues/1",
                "title": "旧卡",
                "replacements": ["D1"],
                "body_sha256": source["sources"][0]["body_sha256"],
            }
        ],
        "legacy_gates": [],
        "conflicts": [],
        "next_queue": ["D1"],
    }
    issues = [
        {
            "number": 2,
            "html_url": entry["url"],
            "body": (
                "<!-- speechnexus-delivery:D1:v2 -->\n优先级：P1\n排期：当前一期\n"
                "交付负责人：当前人\n实现：当前实现\n集成：当前集成\n验证：当前验证"
            ),
            "state": "open",
            "labels": [],
        },
        {
            "number": 1,
            "html_url": "https://github.com/owner/repo/issues/1",
            "state": "closed",
            "state_reason": "not_planned",
            "labels": [],
        },
    ]
    issues.extend(
        [
            {
                "number": e["number"],
                "html_url": e["url"],
                "body": f"<!-- speechnexus-delivery:{e['id']}:v2 -->",
                "state": "open",
                "labels": [],
            }
            for e in (program, goal)
        ]
    )
    return manifest, source, issues


def test_render_preserves_history_without_claiming_live_status() -> None:
    m, s, issues = fixture()
    result = tracker.render_v2(m, issues, "离线", s, "source.json")
    assert "离线" in result and "不可" in result and "not_planned" in result
    assert "状态未知" in result and "可验证" in result


@pytest.mark.parametrize(
    "mutation",
    [
        "missing",
        "tamper",
        "duplicate",
        "cycle",
        "repository",
        "marker",
        "replacement",
        "missing_remote",
    ],
)
def test_invalid_inputs_rejected(mutation: str) -> None:
    m, s, issues = fixture()
    if mutation == "missing":
        m["requirements"].pop()
    elif mutation == "tamper":
        s["sources"][0]["body"] += "x"
    elif mutation == "duplicate":
        m["entries"].append(copy.deepcopy(m["entries"][0]))
    elif mutation == "cycle":
        m["entries"][0]["dependencies"] = [{"id": "D1", "artifact": "x", "condition": "x"}]
    elif mutation == "repository":
        issues[0]["html_url"] = "https://github.com/other/repo/issues/2"
    elif mutation == "marker":
        issues[0]["body"] = "<!-- speechnexus-delivery:wrong:v2 -->"
    elif mutation == "replacement":
        m["legacy_issues"][0]["replacements"] = []
    elif mutation == "missing_remote":
        issues.pop()
    with pytest.raises(ValueError):
        tracker.render_v2(m, issues, "实时", s, "source.json")


def test_cli_validation_failure_preserves_previous_output(tmp_path: Path) -> None:
    import json
    import subprocess
    import sys

    manifest, source, issues = fixture()
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest))
    (tmp_path / "source.json").write_text(json.dumps(source))
    issues[0]["body"] = "<!-- speechnexus-delivery:wrong:v2 -->"
    snapshot = tmp_path / "snapshot.json"
    snapshot.write_text(json.dumps(issues))
    output = tmp_path / "output.md"
    output.write_text("old")
    result = subprocess.run(
        [
            sys.executable,
            str(PATH.with_name("refresh-implementation-tracker.py")),
            "--manifest",
            str(manifest_path),
            "--snapshot",
            str(snapshot),
            "--output",
            str(output),
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert output.read_text() == "old"


def test_discussion_requires_known_captured_url() -> None:
    m, s, issues = fixture()
    m["requirements"].append(
        {
            "id": "DISC-1",
            "source_issue": 1,
            "kind": "DISC",
            "section": "讨论",
            "text": "补充",
            "legacy_checked": False,
            "source_url": "https://github.com/owner/repo/issues/1#issuecomment-123",
            "primary": "D1",
            "targets": ["D1"],
        }
    )
    with pytest.raises(ValueError, match="discussion source"):
        tracker.render_v2(m, issues, "离线", s, "source.json")


def test_current_fields_come_from_remote_and_change_immediately() -> None:
    m, s, issues = fixture()
    first = tracker.render_v2(m, issues, "离线", s, "source.json")
    assert all(x in first for x in ("P1", "当前一期", "当前人", "当前实现", "当前集成", "当前验证"))
    issues[0]["body"] = issues[0]["body"].replace("P1", "P2").replace("当前人", "新人")
    second = tracker.render_v2(m, issues, "离线", s, "source.json")
    assert "P2" in second and "新人" in second and "当前人" not in second
    assert "迁移时点" in second and "历史" in second


def test_missing_or_duplicate_remote_fields_are_unknown() -> None:
    m, s, issues = fixture()
    issues[0]["body"] = "<!-- speechnexus-delivery:D1:v2 -->\n优先级：P0\n优先级：P1\n实现：新"
    result = tracker.render_v2(m, issues, "离线", s, "source.json")
    row = next(line for line in result.splitlines() if line.startswith("| [D1 #2]"))
    assert row.count("未知") >= 5 and "team" not in row


@pytest.mark.parametrize(
    "mutation",
    ["two_programs", "goal_without_program", "delivery_wrong_parent", "program_dependency"],
)
def test_invalid_hierarchy(mutation: str) -> None:
    m, s, issues = fixture()
    if mutation == "two_programs":
        m["entries"][2]["kind"] = "program"
    elif mutation == "goal_without_program":
        m["entries"][2]["parent"] = "D1"
    elif mutation == "delivery_wrong_parent":
        m["entries"][0]["parent"] = "PROGRAM"
    else:
        m["entries"][1]["dependencies"] = [{"id": "D1", "artifact": "x", "condition": "x"}]
    with pytest.raises(ValueError):
        tracker.render_v2(m, issues, "离线", s, "source.json")


def test_gate_requires_exact_source_requirements_and_unique_source() -> None:
    m, s, issues = fixture()
    ids = [r["id"] for r in m["requirements"]]
    gate = {
        "source_issue": 1,
        "code": "G0",
        "requirements": ids,
        "predecessors": [],
        "release": "pending_user_decision",
    }
    m["legacy_gates"] = [gate]
    tracker.render_v2(m, issues, "离线", s, "source.json")
    gate["requirements"] = ids[:-1]
    with pytest.raises(ValueError):
        tracker.render_v2(m, issues, "离线", s, "source.json")
    gate["requirements"] = ids
    m["legacy_gates"].append({**gate, "code": "G1"})
    with pytest.raises(ValueError):
        tracker.render_v2(m, issues, "离线", s, "source.json")


def test_open_done_label_is_conflict() -> None:
    m, s, issues = fixture()
    issues[0]["labels"] = [{"name": "status:done"}]
    result = tracker.render_v2(m, issues, "离线", s, "source.json")
    row = next(line for line in result.splitlines() if line.startswith("| [D1 #2]"))
    assert "状态未知" in row and "冲突" in row


def test_historical_requirement_shows_current_interpretation_next_to_original() -> None:
    manifest, source, issues = fixture()
    requirement = manifest["requirements"][-1]
    original = requirement["text"]
    requirement["interpretation"] = "历史问题已修复，勿重复派工"
    requirement["disposition"] = "已交付"
    result = tracker.render_v2(manifest, issues, "离线", source, "source.json")
    row = next(line for line in result.splitlines() if requirement["id"] in line)
    assert "已交付 | 历史问题已修复，勿重复派工 | " + original in row
    assert requirement["text"] == original


def test_new_open_issue_is_visible_as_incomplete_coverage() -> None:
    manifest, source, issues = fixture()
    issues.extend(
        [
            {
                "number": 5,
                "title": "新增待办",
                "state": "open",
                "labels": [],
                "html_url": "https://github.com/owner/repo/issues/5",
            },
            {
                "number": 6,
                "title": "无关已结历史",
                "state": "closed",
                "html_url": "https://github.com/owner/repo/issues/6",
            },
        ]
    )
    result = tracker.render_v2(manifest, issues, "离线", source, "source.json")
    assert "本矩阵覆盖待补齐" in result
    assert "[\u00235](https://github.com/owner/repo/issues/5) 新增待办" in result
    assert "无关已结历史" not in result


@pytest.mark.parametrize("mutation", ["wrong_url", "wrong_repository", "missing_url"])
def test_untracked_issue_requires_correct_source(mutation: str) -> None:
    manifest, source, issues = fixture()
    extra: dict[str, Any] = {
        "number": 5,
        "state": "open",
        "labels": [],
        "html_url": "https://github.com/owner/repo/issues/5",
    }
    if mutation == "wrong_url":
        extra["html_url"] = "https://github.com/other/project/issues/5"
    elif mutation == "wrong_repository":
        extra["repository_url"] = "https://api.github.com/repos/other/project"
    else:
        extra.pop("html_url")
    issues.append(extra)
    with pytest.raises(ValueError, match="Remote"):
        tracker.render_v2(manifest, issues, "离线", source, "source.json")


def test_legacy_cli_default_does_not_overwrite_current_matrix(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import json
    import sys

    refresh_path = PATH.with_name("refresh-implementation-tracker.py")
    refresh_spec = importlib.util.spec_from_file_location("refresh_legacy", refresh_path)
    assert refresh_spec and refresh_spec.loader
    refresh = importlib.util.module_from_spec(refresh_spec)
    refresh_spec.loader.exec_module(refresh)
    current = tmp_path / "delivery-tracking.md"
    current.write_text("current")
    legacy = tmp_path / "docs/plans/2026-10-23-implementation-tracking.md"
    legacy.parent.mkdir(parents=True)
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps({"schema_version": 1, "repository": "owner/repo", "entries": []})
    )
    monkeypatch.setattr(refresh, "ROOT", tmp_path)
    monkeypatch.setattr(refresh, "DEFAULT_OUTPUT", current)
    monkeypatch.setattr(refresh, "load_issues", lambda *_args: [])
    monkeypatch.setattr(refresh, "render", lambda *_args: "historical")
    monkeypatch.setattr(sys, "argv", [str(refresh_path), "--manifest", str(manifest)])
    refresh.main()
    assert current.read_text() == "current"
    assert legacy.read_text() == "historical"
