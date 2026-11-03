"""Validate current issue relationships and render a GitHub-derived delivery view."""

from __future__ import annotations

import re
from typing import Any


def _unique(values: list[Any], label: str) -> None:
    if len(values) != len(set(values)):
        raise ValueError(f"Duplicate {label}")


def _acyclic(graph: dict[str, list[str]], label: str) -> None:
    visited: set[str] = set()
    active: set[str] = set()

    def visit(ident: str) -> None:
        if ident in active:
            raise ValueError(f"{label} cycle: {ident}")
        if ident in visited:
            return
        active.add(ident)
        for other in graph[ident]:
            visit(other)
        active.remove(ident)
        visited.add(ident)

    for ident in graph:
        visit(ident)


def validate_manifest(manifest: dict[str, Any]) -> dict[str, dict[str, Any]]:
    if (
        set(manifest) != {"schema_version", "repository", "entries"}
        or manifest["schema_version"] != 3
    ):
        raise ValueError("Expected a current issue registry (schema_version 3)")
    if not isinstance(manifest["repository"], str) or not re.fullmatch(
        r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", manifest["repository"]
    ):
        raise ValueError("Invalid repository")
    entries = manifest["entries"]
    if not isinstance(entries, list) or not entries:
        raise ValueError("Missing entries")
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {
            "id",
            "number",
            "kind",
            "parent",
            "dependencies",
        }:
            raise ValueError("Invalid entry fields")
        if not isinstance(entry["id"], str) or not re.fullmatch(r"[A-Z][A-Z0-9-]*", entry["id"]):
            raise ValueError("Invalid entry id")
        if type(entry["number"]) is not int or entry["number"] <= 0:
            raise ValueError("Invalid issue number")
        if entry["kind"] not in {"program", "goal", "delivery"}:
            raise ValueError("Invalid entry kind")
        if entry["parent"] is not None and not isinstance(entry["parent"], str):
            raise ValueError("Invalid parent")
        if not isinstance(entry["dependencies"], list):
            raise ValueError("Invalid dependencies")
        for dep in entry["dependencies"]:
            if not isinstance(dep, dict) or set(dep) != {"id", "artifact", "condition"}:
                raise ValueError("Invalid dependency fields")
            if any(not isinstance(dep[k], str) or not dep[k].strip() for k in dep):
                raise ValueError("Missing dependency input or condition")
        _unique([d["id"] for d in entry["dependencies"]], "dependency")
    _unique([e["id"] for e in entries], "entry id")
    _unique([e["number"] for e in entries], "issue number")
    by_id = {e["id"]: e for e in entries}
    programs = [e for e in entries if e["kind"] == "program"]
    if len(programs) != 1 or programs[0]["parent"] is not None or programs[0]["dependencies"]:
        raise ValueError("Expected one parentless program without dependencies")
    for entry in entries:
        if entry["kind"] != "program":
            expected = "program" if entry["kind"] == "goal" else "goal"
            parent = by_id.get(entry["parent"])
            if parent is None or parent["kind"] != expected:
                raise ValueError(f"Invalid parent kind: {entry['id']}")
        if any(d["id"] not in by_id for d in entry["dependencies"]):
            raise ValueError(f"Unknown dependency: {entry['id']}")
    _acyclic({e["id"]: [e["parent"]] if e["parent"] else [] for e in entries}, "parent")
    _acyclic({e["id"]: [d["id"] for d in e["dependencies"]] for e in entries}, "dependency")
    return by_id


def _cell(value: Any) -> str:
    return str(value).replace("|", "\\|").replace("\n", "<br>")


def _field(issue: dict[str, Any], label: str) -> str:
    values = re.findall(rf"^{re.escape(label)}：([^\r\n]*)$", issue.get("body") or "", re.MULTILINE)
    if len(values) != 1 or not values[0].strip():
        return "未知"
    value = values[0].strip()
    if label == "优先级" and value not in {"P0", "P1", "P2"}:
        return "未知"
    return str(value)


def _state(issue: dict[str, Any]) -> str:
    if issue.get("state") == "closed":
        reason = issue.get("state_reason") or "unknown"
        suffix = "；不计完成" if reason != "completed" else "；验收见正文证据"
        return f"closed / {reason}{suffix}"
    if issue.get("state") != "open":
        return "状态未知"
    labels = [
        label["name"]
        for label in (issue.get("labels") or [])
        if label["name"].startswith("status:")
    ]
    allowed = {"planned", "ready", "in-progress", "integration", "qualification", "blocked"}
    if len(labels) != 1 or labels[0].removeprefix("status:") not in allowed:
        return "状态未知（标签缺失、冲突或与 open 不一致）"
    return f"open / {labels[0]}"


def render(
    manifest: dict[str, Any],
    issues: list[dict[str, Any]],
    mode: str,
    snapshot_at: str | None = None,
) -> str:
    by_id = validate_manifest(manifest)
    _unique([i["number"] for i in issues], "remote issue number")
    repository = manifest["repository"]
    for issue in issues:
        number = issue["number"]
        if type(number) is not int or number <= 0:
            raise ValueError("Invalid remote issue number")
        if issue.get("html_url") != f"https://github.com/{repository}/issues/{number}":
            raise ValueError(f"Remote URL mismatch: {number}")
        if issue.get("repository_url") not in (None, f"https://api.github.com/repos/{repository}"):
            raise ValueError(f"Remote repository mismatch: {number}")
    remote = {i["number"]: i for i in issues}
    for entry in by_id.values():
        tracked_issue = remote.get(entry["number"])
        if tracked_issue is None:
            raise ValueError(f"Issue missing from remote: {entry['number']}")
        markers = re.findall(
            r"<!--\s*speechnexus-delivery:[^\n]*?-->", tracked_issue.get("body") or ""
        )
        if markers != [f"<!-- speechnexus-delivery:{entry['id']}:v3 -->"]:
            raise ValueError(f"Remote marker mismatch: {entry['id']}")

    def ref(ident: str) -> str:
        entry = by_id[ident]
        issue = remote[entry["number"]]
        return f"[{_cell(issue.get('title') or ident)} #{entry['number']}]({issue['html_url']})"

    lines = [
        "# SpeechNexus 产品交付跟踪",
        "",
        f"来源：{_cell(mode)}；快照时间：{snapshot_at or '未知'}。",
        "GitHub 正文与生命周期为当前事实来源；离线快照不可视为实时状态，未知字段不补猜测。",
        "依赖按具名输入接收，关联卡关闭不自动构成前置；完整范围与 AC、实际证据见对应 issue。",
        "",
        "## 总控与产品目标",
        "",
        "| 项目 | GitHub 状态 | 当前验证 |",
        "| --- | --- | --- |",
    ]
    for entry in by_id.values():
        if entry["kind"] != "delivery":
            issue = remote[entry["number"]]
            lines.append(
                f"| {ref(entry['id'])} | {_state(issue)} | {_cell(_field(issue, '验证'))} |"
            )
    lines += [
        "",
        "## 当前交付",
        "",
        "| 交付 / 产品目标 | 优先级 / 排期 / 负责人 | GitHub 状态 | "
        "当前事实与缺口 | 必要输入 | 证据 |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    deliveries = sorted(
        (e for e in by_id.values() if e["kind"] == "delivery"),
        key=lambda e: (_field(remote[e["number"]], "优先级"), e["number"]),
    )
    for entry in deliveries:
        issue = remote[entry["number"]]
        facts = "<br>".join(
            f"{k}：{_cell(_field(issue, k))}" for k in ("实现", "集成", "验证", "阻塞")
        )
        inputs = (
            "<br>".join(
                f"{ref(d['id'])}：{_cell(d['artifact'])}；{_cell(d['condition'])}"
                for d in entry["dependencies"]
            )
            or "无已登记输入"
        )
        assignment = " / ".join(_cell(_field(issue, k)) for k in ("优先级", "排期", "交付负责人"))
        lines.append(
            f"| {ref(entry['id'])}<br>目标：{ref(entry['parent'])} | {assignment} | "
            f"{_state(issue)} | {facts} | {inputs} | {_cell(_field(issue, '证据'))} |"
        )
    tracked = {e["number"] for e in by_id.values()}
    extras = sorted(
        (i for i in issues if i.get("state") == "open" and i["number"] not in tracked),
        key=lambda i: i["number"],
    )
    lines += ["", "## 尚未登记的 open issue", ""]
    if extras:
        lines.append("以下记录覆盖待补齐，应先分类；普通反馈保留原始报告，不强制转换为实施任务。")
        lines.extend(
            f"- [{_cell(i.get('title') or '')} #{i['number']}]({i['html_url']})：{_state(i)}"
            for i in extras
        )
    else:
        lines.append("本次快照未发现未登记的 open issue。")
    program = next(e for e in by_id.values() if e["kind"] == "program")
    lines += ["", "## 下一队列", "", _field(remote[program["number"]], "下一队列"), ""]
    return "\n".join(lines)
