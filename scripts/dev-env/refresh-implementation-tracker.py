"""Refresh a local implementation matrix from GitHub; never mutate remote issues."""

from __future__ import annotations

import argparse
import json
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_MANIFEST = ROOT / "docs/plans/2026-10-23-implementation-issues.json"
DEFAULT_OUTPUT = ROOT / "docs/plans/2026-10-23-implementation-tracking.md"
STATUS_NAMES = {
    "ready": "可开工",
    "planned": "待实施",
    "in-progress": "实施中",
    "integration": "待联调",
    "qualification": "待资格验证",
    "blocked": "阻塞",
    "done": "完成",
}


def load_issues(repository: str, snapshot: Path | None) -> list[dict[str, Any]]:
    if snapshot:
        result: Any = json.loads(snapshot.read_text())
    else:
        result = []
        page = 1
        while True:
            command = [
                "gh",
                "api",
                f"repos/{repository}/issues?state=all&per_page=100&page={page}",
            ]
            response = subprocess.run(command, check=True, capture_output=True, text=True)
            page_issues = json.loads(response.stdout)
            result.extend(page_issues)
            if len(page_issues) < 100:
                break
            page += 1
    if not isinstance(result, list):
        raise ValueError("GitHub snapshot must contain an issue list or a list of pages")
    pages = result if result and isinstance(result[0], list) else [result]
    return [issue for page in pages for issue in page if "pull_request" not in issue]


def validate_manifest(manifest: dict[str, Any]) -> dict[str, dict[str, Any]]:
    entries = manifest["entries"]
    by_id = {entry["id"]: entry for entry in entries}
    if len(by_id) != len(entries) or len({entry["number"] for entry in entries}) != len(entries):
        raise ValueError("Tracking IDs and issue numbers must be unique")
    for entry in entries:
        references = list(entry.get("native_blocked_by", []))
        references.extend(dep["id"] for dep in entry.get("artifact_dependencies", []))
        references.extend(entry.get("integration_partners", []))
        if entry.get("parent"):
            references.append(entry["parent"])
        if any(reference not in by_id for reference in references):
            raise ValueError(f"Unknown issue reference in {entry['id']}")
    coverage_ids: set[str] = set()
    for coverage in manifest.get("delivery_coverage", []):
        if coverage["id"] in coverage_ids or not coverage["packages"] or not coverage["gates"]:
            raise ValueError(f"Invalid delivery coverage: {coverage['id']}")
        coverage_ids.add(coverage["id"])
        for ident in coverage["packages"]:
            if ident not in by_id or by_id[ident]["kind"] != "work-package":
                raise ValueError(f"Invalid work-package coverage in {coverage['id']}: {ident}")
        for ident in coverage["gates"]:
            if ident not in by_id or by_id[ident]["kind"] != "gate":
                raise ValueError(f"Invalid Gate coverage in {coverage['id']}: {ident}")
    visited: set[str] = set()
    active: set[str] = set()

    def visit(ident: str) -> None:
        if ident in active:
            raise ValueError(f"Native blocking cycle at {ident}")
        if ident in visited:
            return
        active.add(ident)
        for dependency in by_id[ident].get("native_blocked_by", []):
            visit(dependency)
        active.remove(ident)
        visited.add(ident)

    for ident in by_id:
        visit(ident)
    return by_id


def completed(issue: dict[str, Any]) -> bool:
    return issue["state"] == "closed" and issue.get("state_reason") == "completed"


def status(issue: dict[str, Any]) -> str:
    if completed(issue):
        return "完成"
    if issue["state"] == "closed":
        return f"已关闭（{issue.get('state_reason') or '原因待核对'}；不计完成）"
    states = [
        label["name"].removeprefix("status:")
        for label in issue.get("labels", [])
        if label["name"].startswith("status:")
    ]
    if len(states) != 1 or states[0] not in STATUS_NAMES or states[0] == "done":
        return "状态待核对（需一个有效 status 标签）"
    return STATUS_NAMES[states[0]]


def render(manifest: dict[str, Any], issues: list[dict[str, Any]], mode: str) -> str:
    by_id = validate_manifest(manifest)
    remote = {issue["number"]: issue for issue in issues}
    missing = [entry["number"] for entry in by_id.values() if entry["number"] not in remote]
    if missing:
        raise ValueError(f"Issues missing from remote snapshot: {missing}")
    expected_repository = f"https://api.github.com/repos/{manifest['repository']}"
    for entry in by_id.values():
        issue = remote[entry["number"]]
        repository_url = issue.get("repository_url")
        issue_url = issue.get("html_url")
        if repository_url is None and issue_url is None:
            raise ValueError(f"Issue source missing for {entry['id']}")
        if repository_url is not None and repository_url != expected_repository:
            raise ValueError(f"Issue repository mismatch for {entry['id']}")
        if issue_url is not None and issue_url != entry["url"]:
            raise ValueError(f"Issue URL mismatch for {entry['id']}")

    def ref(ident: str) -> str:
        entry = by_id[ident]
        return f"[{ident} #{entry['number']}]({entry['url']})"

    def accepted(ident: str) -> bool:
        entry = by_id[ident]
        return completed(remote[entry["number"]]) and all(
            accepted(dependency) for dependency in entry.get("native_blocked_by", [])
        )

    def state(entry: dict[str, Any]) -> str:
        if (
            entry["kind"] == "gate"
            and completed(remote[entry["number"]])
            and not accepted(entry["id"])
        ):
            return "已关闭待核对（前置未通过；不计 Gate 通过）"
        return status(remote[entry["number"]])

    def checked(ident: str) -> str:
        return "x" if accepted(ident) else " "

    packages = [entry for entry in by_id.values() if entry["kind"] == "work-package"]
    gates = [entry for entry in by_id.values() if entry["kind"] == "gate"]
    epics = [entry for entry in by_id.values() if entry["kind"] == "epic"]
    count_packages = sum(completed(remote[entry["number"]]) for entry in packages)
    count_gates = sum(accepted(entry["id"]) for entry in gates)
    timestamp = datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds")
    source_url = f"https://github.com/{manifest['repository']}/blob/main"
    lines = [
        "<!-- speechnexus-implementation:PROGRAM:v1 -->",
        "# SpeechNexus 三团队整体实施跟踪",
        "",
        f"总控：{ref('PROGRAM')}。快照：{timestamp}（Asia/Shanghai，{mode}）。",
        "GitHub issue 状态、唯一 status 标签与显式验收证据为事实来源；本矩阵是可刷新的快照。",
        "",
        f"**工作包完成：{count_packages}/{len(packages)}；验收 Gate 通过："
        f"{count_gates}/{len(gates)}。** Epic/检查项不混入工作包完成率；"
        "duplicate/not_planned 等非完成关闭不计成果，前置未通过的误关 Gate 不计通过。",
        "",
        "## 指挥与环境",
        "",
        "| 团队 | 环境 | 主责 |",
        "| --- | --- | --- |",
        "| A | 云开发环境 | 平台/协调/迁移/公开 API；代码和接口集成 |",
        "| B | MegaCube GPU | 网关/控制器/模型/真实资格；GPU 排队与隔离 Linux 集成拓扑 |",
        "| C | MacBook | workbench 后端/Web/浏览器媒体/高级业务与体验 |",
        "",
        "用户统一派工与关口放行；每轮共同 integration_base_sha，三队返回证据，A 汇总。"
        "代码/制品经确定版本交接，不共享本地目录；提交/推送与运行态遵循具体任务授权。",
        "",
        "## 当前派工入口",
        "",
    ]
    ready = [entry for entry in packages if state(entry) == "可开工"]
    lines += [f"- {ref(entry['id'])} — Team {entry['team']}：{entry['title']}" for entry in ready]
    if not ready:
        lines.append("当前没有标记为可开工的工作包，请先核对依赖和状态标签。")
    lines += [
        "",
        "首轮默认 A00/B00/C00；B00 真实探针仍受环境/制品/语料/窗口约束。"
        "B01/B02/C01/C02/C04 的 fake/调查子项可先行，不能据此标整个包 ready 或已实施。",
        "",
        "## 六阶段 Epic 与独立关口",
        "",
        "| 阶段 Epic | Gate/状态 | 唯一主归属工作包 |",
        "| --- | --- | --- |",
    ]
    for epic in epics:
        gate = by_id[epic["id"].replace("S", "G", 1)]
        owned = [ref(entry["id"]) for entry in packages if entry["parent"] == epic["id"]]
        lines.append(
            f"| {ref(epic['id'])} {epic['title']} | {ref(gate['id'])} {state(gate)} | "
            f"{'、'.join(owned)} |"
        )
    lines += [
        "",
        "原生层级为总控→6 Epic→工作包/Gate。工作包按完整范围唯一归属，"
        "跨阶段检查项在下面单独核对；阶段推进看 Gate，不等待前一 Epic 全部关闭。",
        "",
        "### Gate 证据与原生阻塞",
        "",
        "| Gate | 必须有的证据 | 真正的整包/关口前置 |",
        "| --- | --- | --- |",
    ]
    for gate in gates:
        dependencies = "、".join(ref(ident) for ident in gate["native_blocked_by"])
        lines.append(f"| {ref(gate['id'])} | {gate['acceptance']} | {dependencies} |")
    lines += [
        "",
        "除 G0 的 A00/B00/C00 基础输入及 G0→G5 关口顺序外，不把部分制品或联合联调"
        "错误地建成整包 blocked_by。A01/A07/B07/C08 等包持续 open 时，"
        "已具备证据的早期 Gate 可以放行。",
        "",
        "## 25 个工作包与依赖矩阵",
        "",
        "| 工作包 | 团队/主 Epic | 状态 | 开工所需制品（分段，不要求整包先关闭） "
        "| 联合验收伙伴 | Gate 检查项 |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for entry in packages:
        dependencies = (
            "；".join(
                f"{ref(dep['id'])}：{dep['deliverable']}" for dep in entry["artifact_dependencies"]
            )
            or "现有契约/环境可开始；外部条件按正文核对"
        )
        partners = "、".join(ref(ident) for ident in entry["integration_partners"]) or "—"
        checkpoints = "、".join(ref(ident) for ident in entry["checkpoints"])
        lines.append(
            f"| {ref(entry['id'])} {entry['title']} | {entry['team']} / {ref(entry['parent'])} | "
            f"{state(entry)} | {dependencies} | {partners} | {checkpoints} |"
        )
    lines += [
        "",
        "## 跨阶段证据矩阵",
        "",
        "每个格子的证据在对应工作包正文检查项与 Gate 中回填链接/集成 SHA；"
        "此表描述应交付内容，不自动把 issue closed 或 PR 合并转换为证据。",
        "",
        "| Gate | 各队必须交接的具名检查项 |",
        "| --- | --- |",
    ]
    for gate in gates:
        checkpoints = "；".join(
            f"{ref(entry['id'])}：{entry['checkpoints'][gate['id']]}"
            for entry in packages
            if gate["id"] in entry["checkpoints"]
        )
        lines.append(f"| {ref(gate['id'])} | {checkpoints} |")
    if manifest.get("delivery_coverage"):
        lines += [
            "",
            "## 整体产品交付覆盖矩阵",
            "",
            "每项产品能力必须由对应工作包提供证据，在列明的 Gate 验收。"
            "此表用于范围追踪，不能用映射存在证明实施完成。",
            "",
            "| 范围 | 主责 | 实施工作包 | 验收 Gate | 必须交付的证据 |",
            "| --- | --- | --- | --- | --- |",
        ]
        for coverage in manifest["delivery_coverage"]:
            owners = "、".join(ref(ident) for ident in coverage["packages"])
            required_gates = "、".join(ref(ident) for ident in coverage["gates"])
            lines.append(
                f"| {coverage['id']} {coverage['capability']} | {coverage['owner']} | "
                f"{owners} | {required_gates} | {coverage['evidence']} |"
            )
    lines += ["", "## 阶段跟踪清单", ""]
    lines += [f"- [{checked(gate['id'])}] #{gate['number']} — {gate['title']}" for gate in gates]
    lines += [
        "",
        "## 更新、验收与交接规则",
        "",
        "1. 每 issue 只保留一个 status 标签："
        "planned/ready/in-progress/integration/qualification/blocked/done；"
        "ready 由具名输入判断，blocked 写明原因、owner 和解除条件。",
        "2. 工作包只有全部范围和真实验证结束才按 completed 关闭；PR 合并仅进入待联调/待资格，"
        "不能用关闭关键字把整个父包提前关闭。子任务 PR 引用父包并给对应检查项证据。",
        "3. Gate 逐检查项核对当前集成 SHA、契约/模型/镜像/拓扑/语料、命令/标准/结果与未验证项。"
        "前置 Gate 未通过不跳关；总指挥根据三队联合结论放行。",
        "4. 首个资源就接删除与恢复；未知执行保持占用，先有停止/释放证据再退预算。"
        "工作台仅走公开语音协议，LLM 默认关闭/批准内网，业务内容不出网。",
        "5. 单 MegaCube 只支持其已测算力故障域，额外 GPU 节点接管/横扩资格另需资源，"
        "不能用单机逻辑集群或跨主机控制角色代替。",
        "6. 每轮派工、集成和关口变更后刷新本矩阵；刷新脚本只读 GitHub、写本地文件，"
        "不会自动改标签、关闭 issue、同步总控正文或启动实施。",
        "",
        "```bash",
        "python3 scripts/dev-env/refresh-implementation-tracker.py",
        "```",
        "",
        f"仓库编排材料：[整体计划]({source_url}/{manifest['plan_path']})、"
        f"[首轮指令]({source_url}/{manifest['startup_path']})、"
        f"[编排清单]({source_url}/docs/plans/2026-10-23-implementation-issues.json)和"
        f"[刷新脚本]({source_url}/scripts/dev-env/refresh-implementation-tracker.py)。"
        "三队从包含这些材料的同一集成版本启动；远端 issue 正文也提供自包含任务范围。"
        "后续刷新产生的总控正文由维护者核对后显式更新。",
        "",
        "代码事实基准 `390f121`；实际开发从总指挥指定集成 SHA 开始。"
        "创建 issue/原有契约/测试原型不代表新实施完成。",
        "",
    ]
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--snapshot", type=Path, help="Use a saved GitHub response without network")
    parser.add_argument(
        "--root-body", type=Path, help="Also write a body for explicit root issue update"
    )
    args = parser.parse_args()
    manifest = json.loads(args.manifest.read_text())
    try:
        issues = load_issues(manifest["repository"], args.snapshot)
    except subprocess.CalledProcessError as error:
        raise SystemExit(f"GitHub read failed: {error.stderr.strip()}") from error
    body = render(manifest, issues, "保存的 GitHub 快照" if args.snapshot else "实时读取 GitHub")
    args.output.write_text(body)
    if args.root_body:
        args.root_body.write_text(body)
    print(f"Refreshed {len(manifest['entries'])} issue references: {args.output}")


if __name__ == "__main__":
    main()
