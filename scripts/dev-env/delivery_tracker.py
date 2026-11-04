"""Validate requirement ownership and join live issue state into the delivery matrix."""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path
from typing import Any

EPICS = set(range(337, 347))
REQUIREMENT_KEYS = {
    "id",
    "epic",
    "prd",
    "designs",
    "implementation",
    "integration",
    "verification",
    "gap",
    "evidence",
    "issues",
}


def _unique(values: list[Any], label: str) -> None:
    if len(values) != len(set(values)):
        raise ValueError(f"Duplicate {label}")


def _reference(value: Any) -> None:
    if not isinstance(value, str) or not value:
        raise ValueError("Missing document reference")
    path, _, _anchor = value.partition("#")
    if not path or Path(path).is_absolute() or ".." in Path(path).parts or ":" in path:
        raise ValueError(f"Unsafe document reference: {value}")


def validate_manifest(manifest: dict[str, Any]) -> dict[str, dict[str, Any]]:
    if (
        set(manifest)
        != {"schema_version", "repository", "program", "facts_as_of", "epics", "requirements"}
        or manifest["schema_version"] != 4
    ):
        raise ValueError("Expected requirement registry schema_version 4")
    if not isinstance(manifest["repository"], str) or not re.fullmatch(
        r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", manifest["repository"]
    ):
        raise ValueError("Invalid repository")
    if type(manifest["program"]) is not int or manifest["program"] != 336:
        raise ValueError("Expected program #336")
    if not isinstance(manifest["facts_as_of"], str):
        raise ValueError("Missing fact review date")
    date.fromisoformat(manifest["facts_as_of"])
    epics = manifest["epics"]
    if not isinstance(epics, list):
        raise ValueError("Missing epics")
    for epic in epics:
        if not isinstance(epic, dict) or set(epic) != {"number", "title", "scope", "exclusions"}:
            raise ValueError("Invalid epic fields")
        if type(epic["number"]) is not int or epic["number"] not in EPICS:
            raise ValueError("Epic must be #337–346")
        if any(
            not isinstance(epic[k], str) or not epic[k].strip()
            for k in ("title", "scope", "exclusions")
        ):
            raise ValueError("Epic requires a title, scope and exclusions")
    _unique([e["number"] for e in epics], "epic")
    if {e["number"] for e in epics} != EPICS:
        raise ValueError("Expected exactly ten epics #337–346")
    requirements = manifest["requirements"]
    if not isinstance(requirements, list) or not requirements:
        raise ValueError("Missing requirements")
    for req in requirements:
        if not isinstance(req, dict) or set(req) != REQUIREMENT_KEYS:
            raise ValueError("Invalid requirement fields")
        if not isinstance(req["id"], str) or not re.fullmatch(r"[A-Z][A-Z0-9-]*", req["id"]):
            raise ValueError("Invalid requirement id")
        if type(req["epic"]) is not int or req["epic"] not in EPICS:
            raise ValueError(f"Requirement needs one primary epic: {req['id']}")
        _reference(req["prd"])
        if req["prd"] != f"docs/prd.md#{req['id'].lower()}":
            raise ValueError(f"Requirement PRD anchor mismatch: {req['id']}")
        if not isinstance(req["designs"], list) or not req["designs"]:
            raise ValueError("Missing design mapping")
        for ref in req["designs"]:
            _reference(ref)
        _unique(req["designs"], "design reference")
        for field in ("implementation", "integration", "verification", "gap"):
            if not isinstance(req[field], str) or not req[field].strip():
                raise ValueError(f"Missing {field}: {req['id']}")
        if not isinstance(req["evidence"], list):
            raise ValueError("Invalid evidence")
        for evidence in req["evidence"]:
            if not isinstance(evidence, dict) or set(evidence) != {"url", "label", "note"}:
                raise ValueError("Invalid evidence fields")
            if any(not isinstance(v, str) or not v.strip() for v in evidence.values()):
                raise ValueError("Empty evidence field")
            if not evidence["url"].startswith("https://"):
                raise ValueError("Evidence must have an explicit HTTPS source")
        if not isinstance(req["issues"], list) or any(
            type(n) is not int or n <= 0 or n in {336, *EPICS} for n in req["issues"]
        ):
            raise ValueError("Invalid implementation issue")
        _unique(req["issues"], "implementation issue")
    _unique([r["id"] for r in requirements], "requirement")
    if {r["epic"] for r in requirements} != EPICS:
        raise ValueError("Every epic must have requirements")
    owners: dict[int, int] = {}
    for req in requirements:
        for number in req["issues"]:
            if number in owners and owners[number] != req["epic"]:
                raise ValueError("Implementation issue has conflicting epic ownership")
            owners[number] = req["epic"]
    return {r["id"]: r for r in requirements}


def _prd_titles(manifest: dict[str, Any], prd: str) -> dict[str, str]:
    pairs = []
    owners: dict[str, int | None] = {}
    sections: list[int] = []
    current_epic = None
    for line in prd.splitlines():
        section = re.fullmatch(r"## Epic (\d+) · .+", line)
        if section:
            current_epic = int(section[1])
            sections.append(current_epic)
        elif line.startswith("## "):
            current_epic = None
        match = re.fullmatch(r"### ([A-Z][A-Z0-9-]*) · (.+)", line)
        if match:
            pairs.append((match[1], match[2].strip()))
            owners[match[1]] = current_epic
    _unique(sections, "PRD epic section")
    if set(sections) != EPICS:
        raise ValueError("PRD epic coverage mismatch")
    _unique([ident for ident, _ in pairs], "PRD requirement")
    titles = dict(pairs)
    expected = {r["id"] for r in manifest["requirements"]}
    if set(titles) != expected:
        raise ValueError(f"PRD coverage mismatch: {sorted(set(titles) ^ expected)}")
    if any(owners[r["id"]] != r["epic"] for r in manifest["requirements"]):
        raise ValueError("PRD epic ownership mismatch")
    return titles


def validate_documents(manifest: dict[str, Any], root: Path) -> str:
    validate_manifest(manifest)
    root = root.resolve()
    cache: dict[str, str] = {}
    refs = [ref for r in manifest["requirements"] for ref in [r["prd"], *r["designs"]]]
    for ref in refs:
        path, _, anchor = ref.partition("#")
        target = (root / path).resolve()
        if not target.is_relative_to(root) or not target.is_file():
            raise ValueError(f"Document missing or outside repository: {path}")
        if path not in cache:
            cache[path] = target.read_text()
        if anchor:
            text = cache[path]
            explicit = set(re.findall(r'<a\s+id="([^"]+)"', text))
            headings = {
                re.sub(r"\s+", "-", re.sub(r"[^\w\s-]", "", line.lstrip("# ").lower()))
                for line in text.splitlines()
                if line.startswith("#")
            }
            if anchor not in explicit | headings:
                raise ValueError(f"Document anchor missing: {ref}")
    prd = cache["docs/prd.md"]
    _prd_titles(manifest, prd)
    return prd


def _cell(value: str) -> str:
    return value.replace("|", r"\|").replace("\n", "<br>")


def _state(issue: dict[str, Any]) -> str:
    state = issue.get("state")
    if state == "closed":
        reason = issue.get("state_reason") or "unknown"
        meaning = "验收见需求证据" if reason == "completed" else "不计完成"
        return f"closed / {reason}；{meaning}"
    if state != "open":
        return "状态未知"
    labels = [
        label["name"] for label in issue.get("labels") or [] if label["name"].startswith("status:")
    ]
    allowed = {"planned", "ready", "in-progress", "integration", "qualification", "blocked"}
    if len(labels) == 1 and labels[0].removeprefix("status:") in allowed:
        return f"open / {labels[0]}"
    return "open；状态标签待核实"


def render(
    manifest: dict[str, Any],
    issues: list[dict[str, Any]],
    mode: str,
    prd_text: str,
    snapshot_at: str | None = None,
) -> str:
    requirements = validate_manifest(manifest)
    titles = _prd_titles(manifest, prd_text)
    repo = manifest["repository"]
    for issue in issues:
        number = issue.get("number")
        if type(number) is not int or number <= 0:
            raise ValueError("Invalid remote issue number")
        if issue.get("html_url") != f"https://github.com/{repo}/issues/{number}":
            raise ValueError(f"Wrong remote issue source: {number}")
        if issue.get("repository_url") not in (None, f"https://api.github.com/repos/{repo}"):
            raise ValueError(f"Wrong remote repository: {number}")
    _unique([i["number"] for i in issues], "remote issue")
    remote = {i["number"]: i for i in issues}
    numbers = {336, *EPICS, *(n for r in requirements.values() for n in r["issues"])}
    if not numbers <= remote.keys():
        raise ValueError(f"Missing tracked issue: {sorted(numbers - remote.keys())}")
    if remote[336].get("parent_issue_url"):
        raise ValueError("Program must not have a parent")
    for number in EPICS:
        epic = remote[number]
        if epic.get("parent_issue_url") != f"https://api.github.com/repos/{repo}/issues/336":
            raise ValueError(f"Epic #{number} must be a native child of #336")
        if "type:epic" not in {label["name"] for label in epic.get("labels") or []}:
            raise ValueError(f"Epic #{number} is not classified as an epic")
    for req in requirements.values():
        for number in req["issues"]:
            if remote[number].get("parent_issue_url") != (
                f"https://api.github.com/repos/{repo}/issues/{req['epic']}"
            ):
                raise ValueError(f"Implementation #{number} has wrong native parent")

    def ref(number: int) -> str:
        issue = remote[number]
        return f"[{_cell(issue.get('title') or str(number))} #{number}]({issue['html_url']})"

    def doc(value: str, label: str) -> str:
        return f"[{_cell(label)}](../../{value})"

    lines = [
        "# SpeechNexus 需求与交付跟踪",
        "",
        f"GitHub 来源：{_cell(mode)}；快照时间：{snapshot_at or '未知'}。",
        f"实现/集成/验证事实基线核验日期：{manifest['facts_as_of']}；逐行另注的增量核验日期优先，各证据仅证明其实际覆盖范围。",
        "PRD 定义需求，设计规定实现约束；本表关联唯一主 Epic、当前事实、实施卡与验收证据。",
        "未建卡不等于需求遗漏；任务关闭不自动完成需求。结构校验不替代语义 MECE 审查或业务验收。",
        "",
        "## 总控与十个 Epic",
        "",
        f"总控：{ref(336)}。",
        "",
        "| Epic | 拥有的交付 / 边界 | GitHub 状态 | 需求数 |",
        "| --- | --- | --- | --- |",
    ]
    for epic in manifest["epics"]:
        n = epic["number"]
        count = sum(r["epic"] == n for r in requirements.values())
        lines.append(
            f"| {ref(n)} | {_cell(epic['scope'])}<br>边界：{_cell(epic['exclusions'])} | "
            f"{_state(remote[n])} | {count} |"
        )
    lines += [
        "",
        "## 需求、设计与交付关联",
        "",
        "| 需求 / PRD | 主 Epic / 设计 | 当前事实与缺口 | 实施 issue | 依据 |",
        "| --- | --- | --- | --- | --- |",
    ]
    for req in sorted(requirements.values(), key=lambda r: (r["epic"], r["id"])):
        facts = "<br>".join(
            f"{label}：{_cell(req[field])}"
            for label, field in [
                ("实现", "implementation"),
                ("集成", "integration"),
                ("验证", "verification"),
            ]
        )
        if req["gap"] not in f"{req['integration']} {req['verification']}":
            facts += f"<br>缺口：{_cell(req['gap'])}"
        designs = "<br>".join(doc(p, Path(p.split("#")[0]).stem) for p in req["designs"])
        work = "<br>".join(f"{ref(n)}：{_state(remote[n])}" for n in req["issues"]) or "未建卡"
        evidence = (
            "；".join(f"[{_cell(e['label'])}]({e['url']})" for e in req["evidence"])
            or "尚无独立证据"
        )
        lines.append(
            f"| {doc(req['prd'], req['id'] + ' ' + titles[req['id']])} | "
            f"{ref(req['epic'])}<br>{designs} | {facts} | {work} | {evidence} |"
        )
    extras = [i for i in issues if i.get("state") == "open" and i["number"] not in numbers]
    lines += ["", "## 未登记的 open issue", ""]
    if not extras:
        lines.append("本次未发现未登记的 open issue。")
    for issue in sorted(extras, key=lambda i: i["number"]):
        parent = issue.get("parent_issue_url")
        valid = parent in {f"https://api.github.com/repos/{repo}/issues/{n}" for n in EPICS}
        note = "待关联需求" if valid else "父 Epic 未核实；待分类与关联需求"
        lines.append(f"- {ref(issue['number'])}：{note}。")
    lines += [
        "",
        "## 下一步建卡",
        "",
        "按当前目标调用 speechnexus-next-delivery，从未满足的需求选择下一可验收切片；"
        "创建后核验唯一原生父 Epic，再登记关联与刷新本表。",
        "",
    ]
    return "\n".join(lines)
