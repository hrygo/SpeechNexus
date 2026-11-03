"""Validate and render the read-only delivery tracker v2 baseline."""

from __future__ import annotations

import hashlib
import re
from typing import Any


def extract_requirements(source: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract immutable requirements from captured issue bodies."""
    atoms: list[dict[str, Any]] = []
    for issue in sorted(source["sources"], key=lambda item: item["number"]):
        lines = issue["body"].splitlines()
        section = ""
        counters: dict[str, int] = {}
        index = 0
        while index < len(lines):
            line = lines[index]
            if re.match(r"^#{1,6} ", line):
                section = line.lstrip("# ").strip()
            match = re.match(r"^- \[([ xX])\]\s*(.*)", line)
            kind = ""
            checked = False
            end = index + 1
            if match:
                kind = "AC" if "验收" in section or "AC" in section else "TASK"
                while end < len(lines) and not re.match(r"^(?:- \[[ xX]\]|#{1,6} )", lines[end]):
                    end += 1
                text = "\n".join(lines[index:end]).strip()
                checked = match.group(1).lower() == "x"
            elif (
                section in ("目标", "范围", "非目标", "目标与范围")
                and line.strip()
                and not line.startswith("<!--")
            ):
                kind = "SCOPE"
                while (
                    end < len(lines)
                    and lines[end].strip()
                    and not re.match(r"^(?:- \[[ xX]\]|#{1,6} )", lines[end])
                ):
                    end += 1
                text = "\n".join(lines[index:end])
            elif issue["number"] == 259 and re.match(r"^\| \d+ \|", line):
                kind = "GAP"
                text = line
                checked = "~~" in line or "✅" in line
            if kind:
                counters[kind] = counters.get(kind, 0) + 1
                atoms.append(
                    {
                        "id": f"LEG-{issue['number']:04d}-{kind}-{counters[kind]:02d}",
                        "source_issue": issue["number"],
                        "kind": kind,
                        "section": section,
                        "text": text,
                        "legacy_checked": checked,
                        "source_url": issue["url"],
                    }
                )
            index = end
    return atoms


def _unique(values: list[Any], label: str) -> None:
    if len(values) != len(set(values)):
        raise ValueError(f"Duplicate {label}")


def _cycle(graph: dict[str, list[str]], label: str) -> None:
    visited: set[str] = set()
    active: set[str] = set()

    def visit(ident: str) -> None:
        if ident in active:
            raise ValueError(f"{label} cycle: {ident}")
        if ident in visited:
            return
        active.add(ident)
        for other in graph[ident]:
            if other not in graph:
                raise ValueError(f"Unknown {label}: {other}")
            visit(other)
        active.remove(ident)
        visited.add(ident)

    for key in graph:
        visit(key)


def validate_v2(manifest: dict[str, Any], source: dict[str, Any]) -> None:
    if manifest.get("schema_version") != 2 or source.get("schema_version") != 1:
        raise ValueError("Unsupported schema version")
    repository = manifest["repository"]
    if source["repository"] != repository or source["base_sha"] != manifest["baseline_sha"]:
        raise ValueError("Source repository or baseline mismatch")
    entries = manifest["entries"]
    by_id = {e["id"]: e for e in entries}
    _unique([e["id"] for e in entries], "entry id")
    _unique([e["number"] for e in entries], "entry number")
    _unique([e["url"] for e in entries], "entry URL")
    for entry in entries:
        if entry["kind"] not in {"program", "goal", "delivery"}:
            raise ValueError(f"Invalid kind: {entry['id']}")
        if entry["priority"] not in {"P0", "P1", "P2"}:
            raise ValueError(f"Invalid priority: {entry['id']}")
        if not isinstance(entry["number"], int) or entry["number"] <= 0:
            raise ValueError(f"Missing issue number: {entry['id']}")
        if entry["url"] != f"https://github.com/{repository}/issues/{entry['number']}":
            raise ValueError(f"Issue URL mismatch: {entry['id']}")
        for key in ("title", "stage", "owner"):
            if not entry[key]:
                raise ValueError(f"Missing {key}: {entry['id']}")
        if set(entry["initial_progress"]) != {"implemented", "integrated", "verified"}:
            raise ValueError(f"Invalid progress: {entry['id']}")
        for dep in entry["dependencies"]:
            if not dep["artifact"] or not dep["condition"]:
                raise ValueError(f"Missing dependency artifact or condition: {entry['id']}")
        for evidence in entry["evidence"]:
            if not all(evidence.get(key) for key in ("url", "scope", "note")):
                raise ValueError(f"Incomplete evidence: {entry['id']}")
    programs = [entry for entry in entries if entry["kind"] == "program"]
    if len(programs) != 1 or programs[0]["parent"] is not None:
        raise ValueError("Exactly one parentless program is required")
    if programs[0]["dependencies"]:
        raise ValueError("Program cannot depend on its descendants")
    for entry in entries:
        if entry["kind"] == "program":
            continue
        parent = by_id.get(entry["parent"])
        expected_kind = "program" if entry["kind"] == "goal" else "goal"
        if parent is None or parent["kind"] != expected_kind:
            raise ValueError(f"Invalid parent kind: {entry['id']}")
    _cycle({e["id"]: [e["parent"]] if e["parent"] else [] for e in entries}, "parent")
    _cycle({e["id"]: [d["id"] for d in e["dependencies"]] for e in entries}, "dependency")
    sources = source["sources"]
    _unique(source["old_open_numbers"], "old open number")
    _unique([s["number"] for s in sources], "source number")
    if set(source["old_open_numbers"]) != {s["number"] for s in sources}:
        raise ValueError("Old issue source coverage mismatch")
    for item in sources:
        if item["url"] != f"https://github.com/{repository}/issues/{item['number']}":
            raise ValueError("Source URL repository mismatch")
        if hashlib.sha256(item["body"].encode("utf-8")).hexdigest() != item["body_sha256"]:
            raise ValueError(f"Source body hash mismatch: {item['number']}")
        _unique([c["url"] for c in item["comments"]], "source comment URL")
        for comment in item["comments"]:
            if not comment["url"].startswith(item["url"] + "#issuecomment-"):
                raise ValueError("Comment source mismatch")
            if not re.fullmatch(r"[0-9a-f]{64}", comment["body_sha256"]):
                raise ValueError("Invalid comment hash")
    source_by_number = {s["number"]: s for s in sources}
    requirements = manifest["requirements"]
    _unique([r["id"] for r in requirements], "requirement id")
    extracted = {r["id"]: r for r in extract_requirements(source)}
    actual = {r["id"]: r for r in requirements if r["kind"] != "DISC"}
    if set(actual) != set(extracted):
        raise ValueError("Extracted requirement coverage mismatch")
    for ident, expected in extracted.items():
        if any(actual[ident].get(key) != value for key, value in expected.items()):
            raise ValueError(f"Requirement changed: {ident}")
    unions: dict[int, set[str]] = {n: set() for n in source_by_number}
    for requirement in requirements:
        number = requirement["source_issue"]
        if number not in source_by_number:
            raise ValueError(f"Unknown requirement source: {number}")
        targets = requirement["targets"]
        if not targets or requirement["primary"] not in targets:
            raise ValueError(f"Invalid targets: {requirement['id']}")
        _unique(targets, "requirement target")
        if not set(targets) <= by_id.keys():
            raise ValueError(f"Unknown target: {requirement['id']}")
        if requirement["kind"] == "DISC":
            item = source_by_number[number]
            urls = {item["url"], *(c["url"] for c in item["comments"])}
            if requirement["source_url"] not in urls or not requirement["text"]:
                raise ValueError(f"Unknown discussion source: {requirement['id']}")
        unions[number].update(targets)
    legacy = manifest["legacy_issues"]
    _unique([e["number"] for e in legacy], "legacy number")
    if {e["number"] for e in legacy} != set(source_by_number):
        raise ValueError("Legacy source coverage mismatch")
    if {e["number"] for e in legacy} & {e["number"] for e in entries}:
        raise ValueError("Legacy and delivery numbers overlap")
    for item in legacy:
        original = source_by_number[item["number"]]
        if any(item[key] != original[key] for key in ("url", "title", "body_sha256")):
            raise ValueError(f"Legacy source mismatch: {item['number']}")
        _unique(item["replacements"], "legacy replacement")
        if set(item["replacements"]) != unions[item["number"]]:
            raise ValueError(f"Legacy replacement mismatch: {item['number']}")
    _unique(manifest["next_queue"], "queue entry")
    if not set(manifest["next_queue"]) <= by_id.keys():
        raise ValueError("Unknown queue entry")
    gates = manifest["legacy_gates"]
    _unique([g["code"] for g in gates], "legacy gate")
    _unique([g["source_issue"] for g in gates], "legacy gate source")
    graph: dict[str, list[str]] = {}
    for gate in gates:
        if (
            gate["source_issue"] not in source_by_number
            or gate["release"] != "pending_user_decision"
        ):
            raise ValueError("Invalid legacy gate")
        _unique(gate["requirements"], "legacy gate requirement")
        expected_requirements = {
            r["id"] for r in requirements if r["source_issue"] == gate["source_issue"]
        }
        if set(gate["requirements"]) != expected_requirements:
            raise ValueError("Legacy gate requirement coverage mismatch")
        graph[gate["code"]] = gate["predecessors"]
    _cycle(graph, "legacy gate")
    for conflict in manifest["conflicts"]:
        if conflict["target"] not in by_id or not conflict["description"]:
            raise ValueError("Invalid conflict")
    _unique([c["id"] for c in manifest["conflicts"]], "conflict id")


def _cell(value: Any) -> str:
    return str(value).replace("|", "\\|").replace("\n", "<br>")


def _state(issue: dict[str, Any]) -> str:
    state = issue.get("state")
    if state == "closed":
        reason = issue.get("state_reason") or "unknown"
        if reason == "completed":
            return "已关闭（completed；仍须核对验收证据）"
        return f"已关闭（{reason}；不计完成）"
    if state != "open":
        return "状态未知"
    labels = [
        label["name"] for label in issue.get("labels", []) if label["name"].startswith("status:")
    ]
    valid = {"planned", "ready", "in-progress", "integration", "qualification", "blocked", "done"}
    if len(labels) != 1 or labels[0].removeprefix("status:") not in valid:
        return "状态未知（status 标签缺失、冲突或无效）"
    if labels[0] == "status:done":
        return "状态未知（open 与 status:done 冲突）"
    return f"open / {labels[0]}"


def _current_field(issue: dict[str, Any], label: str) -> str:
    body: str = issue.get("body") or ""
    values = re.findall(rf"^{re.escape(label)}：([^\r\n]*)$", body, re.MULTILINE)
    if len(values) != 1 or not values[0].strip():
        return "未知"
    value = values[0].strip()
    if label == "优先级" and value not in {"P0", "P1", "P2"}:
        return "未知"
    return str(value)


def render_v2(
    manifest: dict[str, Any],
    issues: list[dict[str, Any]],
    mode: str,
    source: dict[str, Any],
    source_path: str,
    snapshot_at: str | None = None,
) -> str:
    validate_v2(manifest, source)
    _unique([i["number"] for i in issues], "remote issue number")
    remote = {i["number"]: i for i in issues}
    repository = manifest["repository"]
    for issue in issues:
        if issue.get("html_url") != f"https://github.com/{repository}/issues/{issue['number']}":
            raise ValueError(f"Remote URL mismatch: {issue['number']}")
        if issue.get("repository_url") not in (None, f"https://api.github.com/repos/{repository}"):
            raise ValueError(f"Remote repository mismatch: {issue['number']}")
    for entry in [*manifest["entries"], *manifest["legacy_issues"]]:
        number = entry["number"]
        if number not in remote:
            raise ValueError(f"Issue missing from remote: {number}")
        issue = remote[number]
        if issue.get("html_url") != entry["url"]:
            raise ValueError(f"Remote URL mismatch: {number}")
        if "kind" in entry:
            marker = f"<!-- speechnexus-delivery:{entry['id']}:v2 -->"
            markers = re.findall(r"<!--\s*speechnexus-delivery:[^\n]*?-->", issue.get("body") or "")
            if markers != [marker]:
                raise ValueError(f"Remote marker mismatch: {entry['id']}")
    by_id = {e["id"]: e for e in manifest["entries"]}

    def ref(ident: str) -> str:
        entry = by_id[ident]
        return f"[{ident} #{entry['number']}]({entry['url']})"

    lines = [
        "# SpeechNexus 产品交付跟踪",
        "",
        f"来源：{_cell(mode)}；快照时间：{snapshot_at or '未知'}。",
        "离线快照不可当作实时状态；GitHub issue 当前状态仅以本次读取或所保存快照为准。",
        f"历史基线：`{_cell(manifest['baseline_sha'])}`，捕获于 "
        f"{_cell(source['captured_at'])}，基线时间 {_cell(manifest['baseline_at'])}；"
        f"[完整原文基线]({_cell(source_path)})。",
        "历史 checked、旧 issue 的迁移关闭和映射不构成新验收；"
        "旧 Gate 待用户决定，不能由旧卡关闭推定通过。",
        "",
        "## 总控与目标",
        "",
        "| 项目 | GitHub 状态 |",
        "| --- | --- |",
    ]
    for entry in manifest["entries"]:
        if entry["kind"] != "delivery":
            lines.append(
                f"| {ref(entry['id'])} {_cell(entry['title'])} | "
                f"{_state(remote[entry['number']])} |"
            )
    tracked = {entry["number"] for entry in [*manifest["entries"], *manifest["legacy_issues"]]}
    untracked = sorted(
        (
            issue
            for issue in issues
            if issue.get("state") == "open" and issue["number"] not in tracked
        ),
        key=lambda issue: issue["number"],
    )
    lines += ["", "## 尚未纳入稳定映射的 open issue", ""]
    if untracked:
        lines.append(
            "以下新记录尚未登记目标、依赖及要求映射；本矩阵覆盖待补齐，不能据此宣称待办已完整承接。"
        )
        lines += ["", "| issue | GitHub 状态 |", "| --- | --- |"]
        for issue in untracked:
            url = f"https://github.com/{repository}/issues/{issue['number']}"
            lines.append(
                f"| [#{issue['number']}]({url}) {_cell(issue.get('title') or '')} | "
                f"{_state(issue)} |"
            )
    else:
        lines.append("本次快照未发现未登记的 open issue。")
    lines += [
        "",
        "## 交付项",
        "",
        "| 交付 | 当前排期/优先级/负责人 | GitHub 状态 | 依赖与条件 | 当前实现/集成/验证 "
        "| 迁移时点历史进展 | 迁移时点历史证据 |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for entry in sorted(
        (e for e in manifest["entries"] if e["kind"] == "delivery"),
        key=lambda e: (
            _current_field(remote[e["number"]], "优先级"),
            _current_field(remote[e["number"]], "排期"),
            e["id"],
        ),
    ):
        deps = (
            "；".join(
                f"{ref(d['id'])}: {_cell(d['artifact'])} / {_cell(d['condition'])}（"
                f"{_state(remote[by_id[d['id']]['number']])}）"
                for d in entry["dependencies"]
            )
            or "无已登记依赖"
        )
        progress = " / ".join(
            f"{name}: {_cell(entry['initial_progress'][key])}"
            for key, name in (("implemented", "实现"), ("integrated", "集成"), ("verified", "验证"))
        )
        evidence = (
            "；".join(
                f"[{_cell(e['scope'])}]({e['url']})：{_cell(e['note'])}" for e in entry["evidence"]
            )
            or "未登记"
        )
        issue = remote[entry["number"]]
        current = " / ".join(
            f"{label}: {_cell(_current_field(issue, label))}" for label in ("实现", "集成", "验证")
        )
        lines.append(
            f"| {ref(entry['id'])} {_cell(entry['title'])} | "
            f"{_cell(_current_field(issue, '排期'))} / {_cell(_current_field(issue, '优先级'))} / "
            f"{_cell(_current_field(issue, '交付负责人'))} | {_state(issue)} "
            f"| {deps} | {current} | {progress} | {evidence} |"
        )
    lines += [
        "",
        "## 原始要求对应",
        "",
        "| 原要求 | 原 issue | 新交付 | 历史勾选 | 类别 | 当前解释 | 原文 |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for r in manifest["requirements"]:
        targets = "、".join(ref(t) + ("（主）" if t == r["primary"] else "") for t in r["targets"])
        lines.append(
            f"| {_cell(r['id'])} | [#{r['source_issue']}]({r['source_url']}) | {targets} | "
            f"{'是' if r['legacy_checked'] else '否'} | {_cell(r.get('disposition') or '')} | "
            f"{_cell(r.get('interpretation') or '')} | {_cell(r['text'])} |"
        )
    lines += ["", "## 旧 issue 状态", "", "| issue | GitHub 状态 | 承接 |", "| --- | --- | --- |"]
    for item in manifest["legacy_issues"]:
        lines.append(
            f"| [#{item['number']}]({item['url']}) {_cell(item['title'])} | "
            f"{_state(remote[item['number']])} | "
            f"{'、'.join(ref(t) for t in item['replacements'])} |"
        )
    lines += [
        "",
        "## 历史 Gate",
        "",
        "| Gate | 原 issue | 前置 | 要求 | 放行 |",
        "| --- | --- | --- | --- | --- |",
    ]
    for gate in manifest["legacy_gates"]:
        lines.append(
            f"| {_cell(gate['code'])} | #{gate['source_issue']} | "
            f"{_cell('、'.join(gate['predecessors']))} | "
            f"{_cell('、'.join(gate['requirements']))} | 待用户决定 |"
        )
    lines += ["", "## 待决定事项", ""]
    lines.extend(
        f"- {_cell(c['id'])} / {ref(c['target'])}：{_cell(c['description'])}"
        for c in manifest["conflicts"]
    )
    lines += [
        "",
        "## 建议队列",
        "",
        "、".join(ref(i) for i in manifest["next_queue"]) or "未登记",
        "",
    ]
    return "\n".join(lines)
