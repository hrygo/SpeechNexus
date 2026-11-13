#!/usr/bin/env python3
"""Structural-integrity guard for the reference-acceptance entry point.

What this guard is: a **structure** check over the operator entry point, the
workflow that runs it, and its own registration tables. It answers one question:
*did a mutating command, a read-only argv, a probed judgement function, a
workflow invocation, or a registration row disappear or drift without the
anchored tables being updated in the same change?*

What this guard is not: it does not prove that a condition is correct. An earlier
revision classified conditions (`.ok` / `.code` in an `if`) and claimed to catch a
deleted guard; that classification produced false reds for every equivalent
writing and false greens for bare-object truthiness, and it still could not see an
inverted condition. Semantics stay with ``tests/ci``'s fake-Runner cases (the
evidence column below names them) and with a real apply run.

Registered tables (mirrored into ``docs/operations/reference-acceptance.md`` inside
the anchor block; ``tables --write`` regenerates it):

* ``MUTATE_SITES``      - every ``<recv>.mutate(...)`` command, its judgement, its evidence
* ``READ_ARGUMENTS``    - helpers/variables allowed to build a ``<recv>.read(...)`` argv
* ``WRITE_TOKENS``      - argv words that make a read argv state-changing
* ``PROBE_CASES``       - pure judgement functions exercised with failing inputs
* ``DECLARED_DEFERRALS``- deferred inputs the dry-run may report
* ``SELFTEST_CASES``    - fixtures that must keep passing *and* failing as designed

Every table is compared with the documentation block on each invocation, so
weakening one of them (deleting a case, shrinking the write-token set, dropping a
site row) is a red build unless the documentation changes in the same diff. The
``MUTATE_SITES`` rows are read against the code too: the evidence must be a
collected ``TestCase`` method that asserts something and is not skipped or
truncated, and the judgement cell must name a definition the entry really has.
"""

from __future__ import annotations

import argparse
import ast
import importlib.util
import re
import sys
import tempfile
import types
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).resolve().parents[2]
SELF = Path(__file__).resolve()
ENTRY_DEFAULT = ROOT / "scripts/dev-env/reference-acceptance.py"
WORKFLOW_DEFAULT = ROOT / ".github/workflows/ci.yml"
CONTRACT_DOC = ROOT / "docs/operations/reference-acceptance.md"
ANCHOR_START = "<!-- ci-entry-guard:tables:start -->"
ANCHOR_END = "<!-- ci-entry-guard:tables:end -->"

# The dry-run step order the operator runbook documents. It is deliberately a
# second copy of the entry's own STEPS order: a reordering in the entry (B3) cannot
# pass by agreeing with itself.
EXPECTED_STEP_ORDER = ("tools", "migrate", "roles", "buckets", "stack", "gate", "health")

# `unittest discover -s tests/ci` collects `test*.py` only, so a same-named
# `TestCase` in a helper module (`error_codes.py`, ...) never runs and cannot be
# quoted as evidence.
COLLECTED_TEST_GLOB = "test*.py"


@dataclass(frozen=True)
class SiteRow:
    """One mutating command that must exist, in this function, with this argv."""

    key: str
    judgement: str
    evidence: str


MUTATE_SITES: tuple[SiteRow, ...] = (
    SiteRow(
        "step_migrate:start",
        "`step_migrate` 的 postgres up 退出码（幂等）",
        "test:test_behind_migration_stops_before_roles_buckets_and_stack",
    ),
    SiteRow(
        "step_migrate:migrate",
        "`migration_verify`：migrate 退出码与 alembic_version 单行比对",
        "test:test_migrate_step_builds_before_running",
    ),
    SiteRow(
        "step_roles:provision_argv()",
        "`step_roles`：provision-job-roles 退出码 + 已装 ACL 逐条比对",
        "test:test_roles_step_creates_once_then_reports_idempotent",
    ),
    SiteRow(
        "step_buckets:start",
        "`step_buckets`：minio 启动到 healthy",
        "test:test_bucket_names_follow_the_init_script_rules",
    ),
    SiteRow(
        "step_buckets:initialize",
        "`step_buckets`：minio-init 退出码与建桶输出行数",
        "test:test_bucket_names_follow_the_init_script_rules",
    ),
    SiteRow(
        "step_buckets:verify",
        "`bucket_names`：独立只读核对桶数量与版本化",
        "test:test_bucket_names_follow_the_init_script_rules",
    ),
    SiteRow("step_gate:argv", "`gate_verdict`：闸门 ok/FAIL 判定", "probe:gate_verdict-fail"),
    SiteRow(
        "step_stack:ctx.compose('up', '-d', '--build', '--wait', 'api', 'gateway')",
        "`step_stack`：compose up 退出码判失败",
        "test:test_up_exit_nonzero_with_healthy_ps_is_red",
    ),
)

# Callables allowed inside a `read` argv, and registered as a whole argv. A read
# argv that is neither a literal tuple/list nor one of these rows is red. `spread`
# rows may be spliced with `*`: the segment is read-only by registration, because
# its words are not visible to the guard.
READ_ARGUMENTS: tuple[tuple[str, str, str], ...] = (
    ("psql_argv", "builder", "只读 psql -Atc SELECT/目录查询；写语句不在本函数内构造"),
    ("provision_argv", "builder", "--print-statements 只读渲染 GRANT 语句"),
    (
        "compose_argv",
        "spread",
        "`docker compose -p … --env-file … -f …` 前缀；展开后仍拼只读子命令",
    ),
    ("str", "value", "把仓库路径/端口渲染成单个 argv 词，不产生子命令"),
)

# argv words that turn a `read` into a state change. `uv run` is the one allowed
# pair; every other `run` (compose run, docker run) is a write.
WRITE_TOKENS: tuple[str, ...] = ("run", "up", "start", "down", "restart", "rm", "create")
SHELL_TOKENS: tuple[str, ...] = ("sh", "bash", "zsh", "dash")
INTERPRETER_TOKENS: tuple[str, ...] = ("python", "python3", "perl", "ruby", "node")


@dataclass(frozen=True)
class ProbeRow:
    """A pure judgement function that must keep rejecting its failing inputs."""

    case: str
    function: str
    expectation: str
    must_raise: bool = True


PROBE_CASES: tuple[ProbeRow, ...] = (
    ProbeRow(
        "migration_verify-behind",
        "migration_verify",
        "迁移落后/多行/空 必须失败，正好在 head 通过",
        False,
    ),
    ProbeRow("gate_verdict-ok", "gate_verdict", "OK 行解析出 ok/pending 两类条目"),
    ProbeRow("gate_verdict-fail", "gate_verdict", "FAIL 行必须抛 StepFailureError"),
    ProbeRow("gate_verdict-exit", "gate_verdict", "闸门退出码非零不得读成通过"),
    ProbeRow("gate_verdict-fail-wins", "gate_verdict", "FAIL 与 OK 同时出现必须判失败"),
    ProbeRow("env_gaps", "env_gaps", "空白键入列表、非空键不入列", False),
    ProbeRow("dsn_identity", "dsn_identity", "空 DSN 与非 postgresql DSN 必须失败"),
    ProbeRow("migration_heads", "migration_heads", "单链取唯一 head，双 head 必须失败"),
    ProbeRow("bucket_names", "bucket_names", "去重；三个桶名键全空必须失败"),
)


@dataclass(frozen=True)
class DeferralRow:
    """A deferred input the dry-run may report, identified by stable words."""

    slug: str
    words: tuple[str, ...]
    exclude: tuple[str, ...] = ()


# Row counts are pinned so a table cannot shrink silently: deleting a row means
# lowering a pin here *and* changing the documentation anchor in the same diff.
PINNED_COUNTS: Mapping[str, int] = {
    "MUTATE_SITES": 8,
    "READ_ARGUMENTS": 4,
    "WRITE_TOKENS": 7,
    "PROBE_CASES": 9,
    "DECLARED_DEFERRALS": 1,
    "SELFTEST_CASES": 20,
}

DECLARED_DEFERRALS: tuple[DeferralRow, ...] = (
    DeferralRow(
        "gateway-requires-running-stack",
        ("gateway", "healthy"),
        ("lane", "配置", "镜像", "模型", "engine", "引擎"),
    ),
)


@dataclass(frozen=True)
class SelftestCase:
    """A fixture that pins the guard's own rules; weakening a rule breaks a case."""

    case: str
    source: str
    expect_red: bool
    note: str
    baseline: tuple[SiteRow, ...] = ()
    require_sites: bool = False


SELFTEST_CASES: tuple[SelftestCase, ...] = (
    SelftestCase(
        "vacuity-empty-module",
        "def nothing(ctx):\n    return 1\n",
        True,
        "没有 mutate 站点必须红（反空转）",
        require_sites=True,
    ),
    SelftestCase(
        "discarded-result",
        "def step(ctx):\n    ctx.mutate(('docker', 'compose', 'up', '-d'))\n",
        True,
        "结果被丢弃必须红",
    ),
    SelftestCase(
        "unregistered-site",
        "def step(ctx):\n"
        "    started = ctx.mutate(('docker', 'compose', 'rm', '-f'))\n"
        "    return started\n",
        True,
        "清单里没有的写命令必须红",
    ),
    SelftestCase(
        "receiver-agnostic",
        "def step(ctx):\n"
        "    started = ctx.runner.mutate(('docker', 'compose', 'up', '-d'))\n"
        "    if started.ok:\n        return 'ok'\n",
        True,
        "换接收者（ctx.runner.mutate）也算站点，未登记即红",
    ),
    SelftestCase(
        "truthiness-bare",
        "def step(ctx):\n"
        "    applied = ctx.mutate(('docker', 'compose', 'up', '-d'))\n"
        "    if applied:\n        return 'ok'\n",
        True,
        "裸对象真值必须红（CommandResult 无 __bool__）",
        baseline=(
            SiteRow("step:('docker', 'compose', 'up', '-d')", "fixture", "probe:gate_verdict-fail"),
        ),
        require_sites=True,
    ),
    SelftestCase(
        "truthiness-negated",
        "def step(ctx):\n"
        "    started = ctx.mutate(('docker', 'compose', 'up', '-d'))\n"
        "    if not started:\n        raise RuntimeError('failed')\n",
        True,
        "`if not started:` 必须红（恒真=守卫被架空）",
        baseline=(
            SiteRow("step:('docker', 'compose', 'up', '-d')", "fixture", "probe:gate_verdict-fail"),
        ),
        require_sites=True,
    ),
    SelftestCase(
        "truthiness-is-not-none",
        "def step(ctx):\n"
        "    applied = ctx.mutate(('docker', 'compose', 'up', '-d'))\n"
        "    if applied is not None:\n        return 'ok'\n",
        True,
        "`is not None` 判定必须红（结果永不为 None）",
        baseline=(
            SiteRow("step:('docker', 'compose', 'up', '-d')", "fixture", "probe:gate_verdict-fail"),
        ),
        require_sites=True,
    ),
    SelftestCase(
        "truthiness-ok-is-fine",
        "def step(ctx):\n"
        "    applied = ctx.mutate(('docker', 'compose', 'up', '-d'))\n"
        "    if not applied.ok:\n        raise RuntimeError('failed')\n",
        False,
        "`.ok` 形态必须绿（结构上不是恒真条件）",
        baseline=(
            SiteRow("step:('docker', 'compose', 'up', '-d')", "fixture", "probe:gate_verdict-fail"),
        ),
        require_sites=True,
    ),
    SelftestCase(
        "read-literal-write",
        "def step(ctx):\n    ctx.read(('docker', 'compose', 'down'))\n",
        True,
        "read 里出现 compose down 必须红",
    ),
    SelftestCase(
        "read-shell-c",
        "def step(ctx):\n    ctx.read(('sh', '-c', 'touch /tmp/x'))\n",
        True,
        "read 里出现 sh -c 必须红",
    ),
    SelftestCase(
        "read-interpreter-c",
        "def step(ctx):\n    ctx.read(('python3', '-c', 'open(\"/tmp/x\", \"w\")'))\n",
        True,
        "read 里出现解释器 -c 必须红",
    ),
    SelftestCase(
        "read-uv-run-allowed",
        "def step(ctx):\n    ctx.read(('uv', 'run', '--locked', 'python', 'x.py'))\n",
        False,
        "uv run 是唯一允许的 run 配对",
    ),
    SelftestCase(
        "read-uv-run-docker-not-exempt",
        "def step(ctx):\n    ctx.read(('uv', 'run', 'docker', 'compose', 'run', '-d', 'api'))\n",
        True,
        "uv run 不能豁免后面的 compose run",
    ),
    SelftestCase(
        "read-registered-builder",
        "def step(ctx):\n    ctx.read(psql_argv(ctx, 'SELECT 1'))\n",
        False,
        "登记过的只读 argv 构造器可接受",
    ),
    SelftestCase(
        "read-unknown-builder",
        "def step(ctx):\n    ctx.read(shoot())\n",
        True,
        "未登记的 argv 构造器必须红",
    ),
    SelftestCase(
        "read-variable-argv",
        "def step(ctx):\n    argv = ('sh', '-c', 'touch /tmp/x')\n    ctx.read(argv)\n",
        True,
        "read 的 argv 藏在变量里必须红（旧版静默放过）",
    ),
    SelftestCase(
        "read-starred-unregistered",
        "def step(ctx):\n    argv = ('sh', '-c', 'touch /tmp/x')\n    ctx.read((*argv,))\n",
        True,
        "`*` 展开一个未登记的只读段必须红（旧版静默跳过整个 argv）",
    ),
    SelftestCase(
        "read-starred-registered",
        "def step(ctx):\n    ctx.read((*ctx.compose_argv, 'ps', '-a'))\n",
        False,
        "`*` 展开 READ_ARGUMENTS 里登记过的只读 argv 段可接受",
    ),
    SelftestCase(
        "discarded-to-underscore",
        "def step(ctx):\n    _ = ctx.mutate(('docker', 'compose', 'up', '-d'))\n",
        True,
        "把结果赋给 `_` 也是丢弃（旧版只认裸语句）",
    ),
    SelftestCase(
        "registered-site-passes",
        "def step(ctx):\n"
        "    started = ctx.mutate(ctx.compose('up', '-d', '--build', '--wait', 'api', 'gateway'))\n"
        "    return started\n",
        False,
        "清单内的站点形态必须绿",
        baseline=(
            SiteRow(
                "step:ctx.compose('up', '-d', '--build', '--wait', 'api', 'gateway')",
                "fixture",
                "probe:gate_verdict-fail",
            ),
        ),
        require_sites=True,
    ),
)


def _report(problems: Sequence[str]) -> int:
    for problem in problems:
        for line in problem.splitlines():
            print(f"::error::{line}")
    return 1


def _line_of(path: Path, needle: str) -> int:
    """Line of the module-level `NEEDLE: ...` assignment, so hints point at real code."""
    if not path.is_file():
        return 0
    for index, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if line.startswith(needle + ":"):
            return index
    return 0


def _hint(where: str, needle: str, note: str) -> str:
    line = _line_of(Path(where), needle) if Path(where).is_file() else "?"
    return f"登记处: {where}:{line} 的 {needle}（{note}）"


# ---------------------------------------------------------------------------
# Registries and their documentation anchor
# ---------------------------------------------------------------------------


def render_tables() -> str:
    lines = [
        "### 结构守卫的登记表",
        "",
        "本块由 `python3 scripts/dev-env/ci-entry-guard.py tables --write` 生成，"
        "与 `scripts/dev-env/ci-entry-guard.py` 的常量逐行对应；改动任一登记都必须同时更新另一侧，"
        "否则作业红。",
        "",
        "#### 行数锚点（任何一张表缩水都要同时改这里的数字）",
        "",
        "| 表 | 行数 |",
        "| --- | --- |",
    ]
    for name, count in PINNED_COUNTS.items():
        lines.append(f"| `{name}` | {count} |")
    lines += [
        "",
        "#### MUTATE_SITES（每条写命令必须存在，且能指出谁在别处判定它）",
        "",
        "| 站点 | 判定在哪 | 证据 |",
        "| --- | --- | --- |",
    ]
    for site in MUTATE_SITES:
        lines.append(f"| `{site.key}` | {site.judgement} | `{site.evidence}` |")
    lines += [
        "",
        "#### READ_ARGUMENTS（允许出现在 `read` argv 里的助手、词包装与 `*` 展开段）",
        "",
        "| 助手 | 形态 | 为什么只读 |",
        "| --- | --- | --- |",
    ]
    for name, kind, reason in READ_ARGUMENTS:
        labels = {
            "builder": "整体 argv 构造器",
            "value": "argv 词包装",
            "spread": "可被 `*` 展开的只读 argv 段",
        }
        lines.append(f"| `{name}` | {labels.get(kind, kind)} | {reason} |")
    lines += [
        "",
        "#### WRITE_TOKENS（`read` 里出现即判红的 argv 词）",
        "",
        "`" + "`, `".join(WRITE_TOKENS) + "`；`uv run` 是唯一允许的 `run` 配对；"
        "`"
        + "`, `".join(SHELL_TOKENS)
        + "`（及 `"
        + "`, `".join(INTERPRETER_TOKENS)
        + "`）紧跟 `-c` 一律判红。",
        "",
        "#### PROBE_CASES（必须用失败输入调用的纯判定函数）",
        "",
        "| 用例 | 入口函数 | 失败形态 | 断言 |",
        "| --- | --- | --- | --- |",
    ]
    for probe_row in PROBE_CASES:
        shape = "raise" if probe_row.must_raise else "返回失败值"
        lines.append(
            f"| `{probe_row.case}` | `{probe_row.function}` | {shape} | {probe_row.expectation} |"
        )
    lines += [
        "",
        "#### DECLARED_DEFERRALS（dry-run 允许报告的延后输入）",
        "",
        "| slug | 必须同时出现的词 | 出现即视为别的延后项 |",
        "| --- | --- | --- |",
    ]
    for deferral in DECLARED_DEFERRALS:
        words = "、".join(f"`{word}`" for word in deferral.words)
        exclude = "、".join(f"`{word}`" for word in deferral.exclude) or "—"
        lines.append(f"| `{deferral.slug}` | {words} | {exclude} |")
    lines += [
        "",
        "#### SELFTEST_CASES（守卫自身的自检语料）",
        "",
        "| 用例 | 说明 | 期望 |",
        "| --- | --- | --- |",
    ]
    for case in SELFTEST_CASES:
        expected = "红" if case.expect_red else "绿"
        lines.append(f"| `{case.case}` | {case.note} | {expected} |")
    return "\n".join(lines)


def check_counts() -> list[str]:
    actual = {
        "MUTATE_SITES": len(MUTATE_SITES),
        "READ_ARGUMENTS": len(READ_ARGUMENTS),
        "WRITE_TOKENS": len(WRITE_TOKENS),
        "PROBE_CASES": len(PROBE_CASES),
        "DECLARED_DEFERRALS": len(DECLARED_DEFERRALS),
        "SELFTEST_CASES": len(SELFTEST_CASES),
    }
    problems: list[str] = []
    for name, pinned in PINNED_COUNTS.items():
        if actual[name] != pinned:
            problems.append(
                f"{name} 现在有 {actual[name]} 行，锚点写的是 {pinned} 行；"
                "删/加登记必须同时改 PINNED_COUNTS 与文档锚点块，并在 PR 里说明为什么"
            )
    for name in actual:
        if name not in PINNED_COUNTS:
            problems.append(f"{name} 没有行数锚点；把它加进 PINNED_COUNTS")
    return problems


def _display(path: Path) -> str:
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


def check_anchor(path: Path = CONTRACT_DOC) -> list[str]:
    if not path.is_file():
        return [f"契约文档不存在: {_display(path)}"]
    text = path.read_text(encoding="utf-8")
    if ANCHOR_START not in text or ANCHOR_END not in text:
        return [
            f"{_display(path)} 缺少登记表锚点（{ANCHOR_START} … {ANCHOR_END}）；"
            "跑 `python3 scripts/dev-env/ci-entry-guard.py tables --write` 生成"
        ]
    body = text.split(ANCHOR_START, 1)[1].split(ANCHOR_END, 1)[0].strip()
    expected = render_tables().strip()
    if body != expected:
        return [
            f"{_display(path)} 的登记表与守卫脚本不一致；"
            "改登记时同步文档，或跑 `python3 scripts/dev-env/ci-entry-guard.py tables --write`。\n"
            "不一致的第一行: " + _first_difference(body, expected)
        ]
    return []


def _first_difference(actual: str, expected: str) -> str:
    actual_lines, expected_lines = actual.splitlines(), expected.splitlines()
    for index in range(max(len(actual_lines), len(expected_lines))):
        got = actual_lines[index] if index < len(actual_lines) else "<缺失>"
        want = expected_lines[index] if index < len(expected_lines) else "<多余>"
        if got != want:
            return f"文档 `{got}` / 期望 `{want}`"
    return "内容相同但空白不同"


def write_anchor(path: Path = CONTRACT_DOC) -> None:
    text = path.read_text(encoding="utf-8")
    if ANCHOR_START in text and ANCHOR_END in text:
        before = text.split(ANCHOR_START, 1)[0]
        after = text.split(ANCHOR_END, 1)[1]
        path.write_text(
            f"{before}{ANCHOR_START}\n{render_tables()}\n{ANCHOR_END}{after}", encoding="utf-8"
        )
        return
    path.write_text(
        text.rstrip() + f"\n\n{ANCHOR_START}\n{render_tables()}\n{ANCHOR_END}\n", encoding="utf-8"
    )


# ---------------------------------------------------------------------------
# Entry-point audit (`sites`)
# ---------------------------------------------------------------------------


def _function_nodes(tree: ast.Module) -> list[ast.FunctionDef | ast.AsyncFunctionDef]:
    return [
        node for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]


def _own_nodes(function: ast.AST) -> list[ast.AST]:
    """Nodes of one function body; nested defs are audited as their own function."""
    collected: list[ast.AST] = []
    stack: list[ast.AST] = list(function.body)  # type: ignore[attr-defined]
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        collected.append(node)
        stack.extend(ast.iter_child_nodes(node))
    return collected


def _call_name(call: ast.Call) -> str:
    func = call.func
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return ""


def _context_call(call: ast.Call) -> str:
    """`read`/`mutate` on the entry's context object, whatever wraps it.

    The receiver chain must start at ``ctx``/``context``, so ``handle.read(1 << 20)``
    and ``response.read()`` are not mistaken for entry calls, while
    ``ctx.runner.mutate(...)`` still counts as a mutating site.
    """
    func = call.func
    if not isinstance(func, ast.Attribute) or func.attr not in {"read", "mutate"}:
        return ""
    root: ast.expr = func.value
    while isinstance(root, ast.Attribute):
        root = root.value
    if isinstance(root, ast.Name) and root.id in {"ctx", "context"}:
        return func.attr
    return ""


def site_key(function: str, call: ast.Call) -> str:
    """Stable identity of one mutating call: enclosing function + its argv."""
    argv = ast.unparse(call.args[0]) if call.args else "<no-argv>"
    return f"{function}:{argv[:80]}"


def _argv_tokens(argument: ast.expr) -> tuple[list[str], list[str]]:
    """Flatten an argv expression into literal words plus what could not be resolved."""
    words: list[str] = []
    unresolved: list[str] = []
    elements = argument.elts if isinstance(argument, (ast.Tuple, ast.List)) else [argument]
    for element in elements:
        if isinstance(element, ast.Constant) and isinstance(element.value, str):
            words.append(element.value)
        elif isinstance(element, ast.Starred):
            # `*x` splices words the guard cannot read, so the spliced name has to be
            # a registered read-only segment. Silently skipping the element (the old
            # behaviour) let `ctx.read((*argv,))` carry any command at all.
            spread = _spread_name(element.value)
            row = _argument_rows().get(spread) if spread else None
            if row is None or row[0] != "spread":
                unresolved.append(ast.unparse(element))
        elif isinstance(element, ast.Call):
            name = _call_name(element)
            if name in _value_wrappers():
                continue
            unresolved.append(f"builder {name}")
        elif isinstance(element, ast.Name):
            unresolved.append(f"variable {element.id}")
        elif isinstance(element, ast.Attribute):
            unresolved.append(f"attribute {ast.unparse(element)}")
        else:
            unresolved.append(ast.unparse(element))
    return words, unresolved


def _spread_name(value: ast.expr) -> str:
    """Name of the tuple spliced by `*`, so a registered segment can be recognised."""
    if isinstance(value, ast.Attribute):
        return value.attr
    if isinstance(value, ast.Name):
        return value.id
    return ""


def _argument_rows() -> dict[str, tuple[str, str]]:
    return {name: (kind, reason) for name, kind, reason in READ_ARGUMENTS}


def _value_wrappers() -> frozenset[str]:
    return frozenset(name for name, (kind, _reason) in _argument_rows().items() if kind == "value")


def _write_words(words: Sequence[str]) -> list[str]:
    """Words that make a read argv state-changing, honouring the `uv run` pair."""
    flagged: list[str] = []
    for index, word in enumerate(words):
        lowered = word.lower()
        if lowered in WRITE_TOKENS:
            previous = words[index - 1].lower() if index else ""
            if lowered == "run" and previous == "uv":
                continue
            flagged.append(word)
        if lowered in SHELL_TOKENS or lowered in INTERPRETER_TOKENS:
            following = words[index + 1] if index + 1 < len(words) else ""
            if following == "-c":
                flagged.append(f"{word} -c")
    return flagged


def audit_sources(
    source: str,
    *,
    label: str = "<source>",
    baseline: Sequence[SiteRow] = MUTATE_SITES,
    require_sites: bool = True,
) -> list[str]:
    try:
        tree = ast.parse(source)
    except SyntaxError as error:
        return [f"{label}: 无法解析：{error}"]
    problems: list[str] = []
    found: dict[str, str] = {}
    seen: set[int] = set()
    for function in _function_nodes(tree):
        nodes = _own_nodes(function)
        parents = {id(child): node for node in nodes for child in ast.iter_child_nodes(node)}
        for node in nodes:
            if not isinstance(node, ast.Call):
                continue
            call = _context_call(node)
            if call == "mutate":
                if id(node) in seen:
                    problems.append(f"{label}:{node.lineno} 同一站点被重复计数")
                    continue
                seen.add(id(node))
                key = site_key(function.name, node)
                found[key] = function.name
                parent = parents.get(id(node))
                if isinstance(parent, ast.Expr):
                    problems.append(
                        f"{label}:{node.lineno} `{key}` 的结果被丢弃；写命令的退出码没有接收者"
                    )
                elif isinstance(parent, ast.Assign) and any(
                    isinstance(target, ast.Name) and target.id == "_" for target in parent.targets
                ):
                    problems.append(
                        f"{label}:{node.lineno} `{key}` 的结果被赋给 `_` 后丢弃；"
                        "写命令的退出码没有接收者（`_ =` 与裸表达式一样算丢弃）"
                    )
                continue
            if call != "read":
                continue
            argument = node.args[0] if node.args else None
            if argument is None:
                problems.append(f"{label}:{node.lineno} `read` 没有 argv")
                continue
            if isinstance(argument, ast.Call):
                builder = _call_name(argument)
                row = _argument_rows().get(builder)
                if row is None or row[0] != "builder":
                    problems.append(
                        f"{label}:{node.lineno} `read` 的 argv 由未登记的助手 `{builder}` 构造；"
                        "在 READ_ARGUMENTS 登记它并写明为什么只读\n"
                        + _hint(
                            str(SELF.relative_to(ROOT)), "READ_ARGUMENTS", "一行登记 + 只读理由"
                        )
                    )
                continue
            words, unresolved = _argv_tokens(argument)
            if unresolved:
                problems.append(
                    f"{label}:{node.lineno} `read` 的 argv 无法静态确定"
                    f"（{', '.join(sorted(set(unresolved)))}）；"
                    "改成字面量元组，或在 READ_ARGUMENTS 登记 argv 构造助手\n"
                    + _hint(str(SELF.relative_to(ROOT)), "READ_ARGUMENTS", "一行登记 + 只读理由")
                )
            flagged = _write_words(words)
            if flagged:
                problems.append(
                    f"{label}:{node.lineno} `read` 携带写命令"
                    f"（{', '.join(sorted(set(flagged)))}）；"
                    "dry-run 会真的执行 read 里的命令，写命令必须走 mutate"
                )
    problems += _truthiness_problems(tree, label)
    baseline_by_key = {row.key: row for row in baseline}
    for key in sorted(set(baseline_by_key) - set(found)):
        registered = baseline_by_key[key]
        problems.append(
            f"{label} 登记的写命令消失了: `{key}`（{registered.judgement}）；"
            "删命令必须同时删登记并说明谁来守护\n"
            + _hint(str(SELF.relative_to(ROOT)), "MUTATE_SITES", "删登记要同时改文档块")
        )
    for key in sorted(set(found) - set(baseline_by_key)):
        problems.append(
            f"{label} 出现未登记的写命令: `{key}`；在 MUTATE_SITES 登记它、写明判定在哪、"
            "并给出存在的测试或 probe 用例\n"
            + _hint(str(SELF.relative_to(ROOT)), "MUTATE_SITES", "一行登记 + 判定位置 + 证据")
        )
    if require_sites and not found:
        problems.append(
            f"{label} 里没有任何 `<recv>.mutate(...)` 站点；守卫会变成空转，"
            "先确认入口仍执行写命令（本次检查不接受空清单）"
        )
    return problems


def _result_names(tree: ast.Module) -> set[str]:
    """Names bound from a mutating call, whose type has no truthiness and is never None."""
    names: set[str] = set()
    for function in _function_nodes(tree):
        for node in _own_nodes(function):
            target: ast.expr | None
            value: ast.expr | None
            if isinstance(node, ast.Assign) and len(node.targets) == 1:
                target, value = node.targets[0], node.value
            elif isinstance(node, ast.AnnAssign):
                target, value = node.target, node.value
            else:
                continue
            if (
                isinstance(target, ast.Name)
                and isinstance(value, ast.Call)
                and _context_call(value) == "mutate"
            ):
                names.add(target.id)
    return names


def _mentions(node: ast.AST, names: set[str]) -> bool:
    return any(isinstance(inner, ast.Name) and inner.id in names for inner in ast.walk(node))


def _truthiness_problems(tree: ast.Module, label: str) -> list[str]:
    """`CommandResult` has no `__bool__` and is never None, so these tests are constant.

    This is a type-shape check, not a judgement check: it does not care whether the
    branch is right, only that the condition is not a constant.
    """
    names = _result_names(tree)
    if not names:
        return []
    found: list[str] = []
    for node in ast.walk(tree):
        test: ast.expr | None = None
        if isinstance(node, (ast.Assert, ast.If, ast.IfExp, ast.While)):
            test = node.test
        if test is None:
            continue
        candidate = test
        if isinstance(candidate, ast.UnaryOp) and isinstance(candidate.op, ast.Not):
            candidate = candidate.operand
        line = getattr(node, "lineno", 0)
        if isinstance(candidate, ast.Name) and candidate.id in names:
            found.append(
                f"{label}:{line} 条件直接用了 `{candidate.id}` 的真值；"
                f"`{candidate.id}` 是 `CommandResult`（无 `__bool__`），该条件恒真/恒假。"
                "改成 `.ok`/`.code` 判定"
            )
        elif (
            isinstance(candidate, ast.Compare)
            and isinstance(candidate.left, ast.Name)
            and candidate.left.id in names
            and len(candidate.ops) == 1
            and isinstance(candidate.ops[0], (ast.Is, ast.IsNot))
            and isinstance(candidate.comparators[0], ast.Constant)
            and candidate.comparators[0].value is None
        ):
            found.append(
                f"{label}:{line} 用 `{candidate.left.id} is None` 判定 `CommandResult`；"
                "它由 `ctx.mutate` 直接返回，永远不是 None，该条件恒为真/假。改用 `.ok`/`.code`"
            )
    return found


def audit_evidence() -> list[str]:
    """Every registration must point at one assertion that really runs on the CI platform."""
    problems: list[str] = []
    probe_cases = {row.case for row in PROBE_CASES}
    for row in MUTATE_SITES:
        if row.evidence.startswith("probe:"):
            case = row.evidence.split(":", 1)[1]
            if case not in probe_cases:
                problems.append(
                    f"MUTATE_SITES 的 `{row.key}` 指向不存在的 probe 用例 `{case}`；"
                    "把它加到 PROBE_CASES，或改成存在的 tests/ci 用例"
                )
            continue
        if not row.evidence.startswith("test:"):
            problems.append(
                f"MUTATE_SITES 的 `{row.key}` 证据 `{row.evidence}` 不是可核对的形态"
                "（`test:<tests/ci 用例名>`、`test:<模块>::<类>::<方法名>` 或 `probe:<case>`）"
            )
            continue
        locator = _evidence_locator(row.evidence.split(":", 1)[1])
        if locator is None:
            problems.append(
                f"MUTATE_SITES 的 `{row.key}` 证据 `{row.evidence}` 的定位写法无法解析；"
                "用 `test:<方法名>`，同名方法出现在多处时用 `test:<模块>::<类>::<方法名>`"
            )
            continue
        module, klass_name, name = locator
        found = _find_tests(name, module=module, klass_name=klass_name)
        if not found:
            problems.append(
                f"MUTATE_SITES 的 `{row.key}` 指向不存在（或不在 `TestCase` 类里、不在 "
                f"{COLLECTED_TEST_GLOB} 模块里）的 tests/ci 用例 `{name}`；"
                "删测试必须同时改登记，否则「谁在守护」是空话"
            )
            continue
        if len(found) > 1:
            places = "、".join(f"{_display(match.path)}:{match.klass.lineno}" for match in found)
            problems.append(
                f"MUTATE_SITES 的 `{row.key}` 证据 `{name}` 在 {len(found)} 个 `TestCase` 里"
                f"同名出现（{places}）；守卫不按 glob 顺序挑，证据必须唯一定位："
                "把登记写成 `test:<模块>::<类>::<方法名>`，或给方法改名"
            )
            continue
        match = found[0]
        where = f"{_display(match.path)}:{match.klass.lineno}"
        if len(match.members) > 1:
            lines = "、".join(str(member.lineno) for member in match.members)
            problems.append(
                f"MUTATE_SITES 的 `{row.key}` 证据 `{name}` 在 `{match.klass.name}` 里定义了 "
                f"{len(match.members)} 次（行 {lines}）；守卫读到第一个、运行时用最后一个，"
                "同名重复定义必须红"
            )
        node = match.members[0]
        problems += _evidence_rebinding_problems(row.key, name, match.klass)
        problems += _evidence_skip_problems(row.key, name, where, match.tree, match.klass, node)
        problems += _evidence_body_problems(
            row.key, name, f"{_display(match.path)}:{node.lineno}", node
        )
    return problems


def _module_classes(tree: ast.Module) -> dict[str, ast.ClassDef]:
    """Class definitions of one module, by name, for resolving its own bases."""
    return {node.name: node for node in ast.walk(tree) if isinstance(node, ast.ClassDef)}


def _base_name(base: ast.expr) -> str:
    """The class name a base expression names, without module or type arguments."""
    return ast.unparse(base).split(".")[-1].split("[")[0].strip()


def _class_ancestry(tree: ast.Module, klass: ast.ClassDef) -> list[ast.ClassDef]:
    """`klass` plus its same-module base classes, nearest first."""
    by_name = _module_classes(tree)
    ancestry: list[ast.ClassDef] = []
    queue = [klass]
    while queue:
        current = queue.pop(0)
        if any(current is known for known in ancestry):
            continue
        ancestry.append(current)
        for base in current.bases:
            parent = by_name.get(_base_name(base))
            if parent is not None:
                queue.append(parent)
    return ancestry


def _is_test_case(tree: ast.Module, klass: ast.ClassDef) -> bool:
    """`unittest` runs this class only if some class in its own bases is a TestCase."""
    return any(
        "TestCase" in ast.unparse(base)
        for owner in _class_ancestry(tree, klass)
        for base in owner.bases
    )


@dataclass(frozen=True)
class _EvidenceMatch:
    """One `TestCase` class that defines an evidence method, with all same-named defs."""

    path: Path
    tree: ast.Module
    klass: ast.ClassDef
    members: tuple[ast.FunctionDef, ...]


# Parsing every collected module once per registration made `sites` cost 8 parses of
# the whole `tests/ci` tree. The key carries the file stamp so a rewritten fixture in
# a temp directory is re-parsed instead of served stale.
_MODULE_INDEX_CACHE: dict[
    tuple[str, int, int],
    tuple[ast.Module, dict[str, tuple[tuple[ast.ClassDef, tuple[ast.FunctionDef, ...]], ...]]],
] = {}


def _module_evidence_index(
    path: Path,
) -> tuple[ast.Module, dict[str, tuple[tuple[ast.ClassDef, tuple[ast.FunctionDef, ...]], ...]]]:
    """The module's AST and its `method name -> [(TestCase, definitions)]` index."""
    stat = path.stat()
    key = (str(path), stat.st_mtime_ns, stat.st_size)
    cached = _MODULE_INDEX_CACHE.get(key)
    if cached is not None:
        return cached
    tree = ast.parse(path.read_text(encoding="utf-8"))
    grouped: dict[str, list[tuple[ast.ClassDef, tuple[ast.FunctionDef, ...]]]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef) or not _is_test_case(tree, node):
            continue
        members: dict[str, list[ast.FunctionDef]] = {}
        for member in node.body:
            if isinstance(member, ast.FunctionDef) and member.name.startswith("test"):
                members.setdefault(member.name, []).append(member)
        for name, definitions in members.items():
            grouped.setdefault(name, []).append((node, tuple(definitions)))
    index = {name: tuple(entries) for name, entries in grouped.items()}
    _MODULE_INDEX_CACHE[key] = (tree, index)
    return tree, index


def _evidence_locator(payload: str) -> tuple[str, str, str] | None:
    """(module, class, method) of one evidence cell; module/class empty when unqualified."""
    parts = payload.split("::")
    if len(parts) == 1:
        return ("", "", parts[0]) if parts[0].startswith("test") else None
    if len(parts) == 3 and all(part.strip() for part in parts) and parts[2].startswith("test"):
        return parts[0], parts[1], parts[2]
    return None


def _find_tests(name: str, *, module: str = "", klass_name: str = "") -> list[_EvidenceMatch]:
    """Every `TestCase` in a collected module that defines `name`, never just the first."""
    if not name.startswith("test"):
        return []
    matches: list[_EvidenceMatch] = []
    for path in sorted((ROOT / "tests/ci").glob(COLLECTED_TEST_GLOB)):
        if module and path.name != module:
            continue
        tree, index = _module_evidence_index(path)
        for klass, members in index.get(name, ()):
            if klass_name and klass.name != klass_name:
                continue
            matches.append(_EvidenceMatch(path, tree, klass, members))
    return matches


def _decorator_signature(decorator: ast.expr) -> tuple[str, list[ast.expr], str]:
    """(lower-cased name, call arguments, source) of one decorator expression."""
    source = ast.unparse(decorator).splitlines()[0]
    if isinstance(decorator, ast.Call):
        return _call_name(decorator).casefold(), list(decorator.args), source
    if isinstance(decorator, ast.Attribute):
        return decorator.attr.casefold(), [], source
    if isinstance(decorator, ast.Name):
        return decorator.id.casefold(), [], source
    return "", [], source


# The acceptance job runs on `ubuntu-24.04`, so a condition that is provably false
# there cannot stop an evidence case from running in CI. The value is fixed here
# rather than read from the interpreter running the guard, so a macOS workstation and
# the CI runner reach the same verdict.
CI_PLATFORM = "linux"
CI_PLATFORM_STRINGS = {
    "sys.platform": "linux",
    "platform.system()": "Linux",
    "os.name": "posix",
}


def _platform_comparison(operator: ast.cmpop, left: str, right: str) -> bool | None:
    """`==`/`!=` over two platform strings; any other operator stays undecided."""
    if isinstance(operator, ast.Eq):
        return left == right
    if isinstance(operator, ast.NotEq):
        return left != right
    return None


def _platform_string(node: ast.expr) -> str | None:
    """The CI-platform value of a platform probe, or None when it is not one."""
    return CI_PLATFORM_STRINGS.get(ast.unparse(node).strip())


def _ci_condition_value(node: ast.expr, constants: Mapping[str, bool]) -> bool | None:
    """Value of a skip condition on the CI platform, or None when that is not provable.

    Deliberately narrow: literals, module-level names bound to a provable value, `not`,
    `and`/`or`, `==`/`!=` against `sys.platform`/`platform.system()`/`os.name`, and
    `startswith`/`endswith` on them. Anything else stays unknown and keeps the old
    "cannot confirm it runs" verdict.
    """
    if isinstance(node, ast.Constant) and isinstance(node.value, bool):
        return node.value
    if isinstance(node, ast.Name):
        return constants.get(node.id)
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        inner = _ci_condition_value(node.operand, constants)
        return None if inner is None else not inner
    if isinstance(node, ast.BoolOp):
        values = [_ci_condition_value(value, constants) for value in node.values]
        if isinstance(node.op, ast.And):
            if any(value is False for value in values):
                return False
            return True if all(value is True for value in values) else None
        if any(value is True for value in values):
            return True
        return False if all(value is False for value in values) else None
    if isinstance(node, ast.Compare) and len(node.ops) == 1 and len(node.comparators) == 1:
        left, right = node.left, node.comparators[0]
        left_platform, right_platform = _platform_string(left), _platform_string(right)
        if (
            left_platform is not None
            and isinstance(right, ast.Constant)
            and isinstance(right.value, str)
        ):
            return _platform_comparison(node.ops[0], left_platform, right.value)
        if (
            right_platform is not None
            and isinstance(left, ast.Constant)
            and isinstance(left.value, str)
        ):
            return _platform_comparison(node.ops[0], left.value, right_platform)
        return None
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in {"startswith", "endswith"}
        and len(node.args) == 1
        and isinstance(node.args[0], ast.Constant)
        and isinstance(node.args[0].value, str)
    ):
        platform = _platform_string(node.func.value)
        suffix = node.args[0].value
        if platform is not None and isinstance(suffix, str):
            return (
                platform.startswith(suffix)
                if node.func.attr == "startswith"
                else platform.endswith(suffix)
            )
    return None


def _module_constants(tree: ast.Module) -> dict[str, bool]:
    """Module-level names bound once to a value the CI platform makes provable.

    A name assigned anywhere else (a `global` rebind in `setUpModule`, a local with the
    same name) is left out: the module-level value would no longer be the one `unittest`
    evaluates, so proving it would prove the wrong thing.
    """
    assignments: dict[str, int] = {}
    for node in ast.walk(tree):
        targets: list[ast.expr] = []
        if isinstance(node, ast.Assign):
            targets = list(node.targets)
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
        for target in targets:
            if isinstance(target, ast.Name):
                assignments[target.id] = assignments.get(target.id, 0) + 1
    constants: dict[str, bool] = {}
    for statement in tree.body:
        if isinstance(statement, ast.Assign) and len(statement.targets) == 1:
            target = statement.targets[0]
            if isinstance(target, ast.Name) and assignments.get(target.id) == 1:
                value = _ci_condition_value(statement.value, constants)
                if value is not None:
                    constants[target.id] = value
        elif (
            isinstance(statement, ast.AnnAssign)
            and isinstance(statement.target, ast.Name)
            and statement.value is not None
            and assignments.get(statement.target.id) == 1
        ):
            value = _ci_condition_value(statement.value, constants)
            if value is not None:
                constants[statement.target.id] = value
    return constants


def _skip_decorator_reason(decorator: ast.expr, constants: Mapping[str, bool]) -> str:
    """Why this decorator makes the test not count as evidence, or "" when it does not."""
    name, arguments, source = _decorator_signature(decorator)
    if name == "expectedfailure":
        return f"`{source}`（断言失败也会被计为通过）"
    if name == "skip":
        return f"`{source}`（无条件跳过）"
    if name not in {"skipif", "skipunless"}:
        return f"`{source}`（装饰器名字里带 skip）" if "skip" in name else ""
    condition = arguments[0] if arguments else None
    if condition is None:
        return f"`{source}`（没有可判定的条件，无法确认这个用例会运行）"
    value = _ci_condition_value(condition, constants)
    if value is None:
        return (
            f"`{source}`（skip 条件不是字面量，也不是在 CI 平台（{CI_PLATFORM}）上可判定的 "
            "`sys.platform`/`platform.system()`/`os.name` 形态，无法确认这个用例会运行；"
            "证据用例不要带无法判定的条件跳过）"
        )
    skipped = value if name == "skipif" else not value
    if not skipped:
        return ""
    return f"`{source}`（在 CI 平台（{CI_PLATFORM}）上条件为 {value}，该用例不会运行）"


def _skips_here(inner: ast.AST) -> bool:
    """A `skipTest(...)` call or a `raise ...SkipTest` statement."""
    if (
        isinstance(inner, ast.Call)
        and isinstance(inner.func, ast.Attribute)
        and inner.func.attr.casefold() == "skiptest"
    ):
        return True
    return (
        isinstance(inner, ast.Raise)
        and inner.exc is not None
        and "SkipTest" in ast.unparse(inner.exc)
    )


def _raise_or_call_marks(node: ast.AST) -> list[str]:
    """`skipTest` calls and `raise ...SkipTest` statements inside one statement subtree."""
    return [
        f"`{ast.unparse(inner).splitlines()[0]}`" for inner in ast.walk(node) if _skips_here(inner)
    ]


def _reachable_skip_marks(
    statements: Sequence[ast.stmt], constants: Mapping[str, bool]
) -> list[str]:
    """Skip marks that can still execute on the CI platform, following `if` conditions."""
    marks: list[str] = []
    for statement in statements:
        marks += _reachable_marks_in(statement, constants)
    return marks


def _reachable_marks_in(node: ast.AST, constants: Mapping[str, bool]) -> list[str]:
    """Marks of one statement; a branch the CI platform provably skips is not entered."""
    if isinstance(node, ast.If):
        value = _ci_condition_value(node.test, constants)
        if value is True:
            return _reachable_skip_marks(node.body, constants)
        if value is False:
            return _reachable_skip_marks(node.orelse, constants)
        return _reachable_skip_marks(node.body, constants) + _reachable_skip_marks(
            node.orelse, constants
        )
    if isinstance(node, (ast.For, ast.AsyncFor, ast.While)):
        return _reachable_skip_marks(node.body, constants) + _reachable_skip_marks(
            node.orelse, constants
        )
    if isinstance(node, (ast.With, ast.AsyncWith)):
        return _reachable_skip_marks(node.body, constants)
    if isinstance(node, ast.Try):
        marks: list[str] = []
        for block in (node.body, node.orelse, node.finalbody):
            marks += _reachable_skip_marks(block, constants)
        for handler in node.handlers:
            marks += _reachable_skip_marks(handler.body, constants)
        return marks
    return _raise_or_call_marks(node)


def _evidence_rebinding_problems(key: str, name: str, klass: ast.ClassDef) -> list[str]:
    """A class-body assignment to the evidence name replaces the method `unittest` runs."""
    for statement in klass.body:
        targets: list[ast.expr] = []
        if isinstance(statement, ast.Assign):
            targets = list(statement.targets)
        elif isinstance(statement, ast.AnnAssign):
            targets = [statement.target]
        if not any(isinstance(target, ast.Name) and target.id == name for target in targets):
            continue
        source = ast.unparse(statement).splitlines()[0]
        return [
            f"MUTATE_SITES 的 `{key}` 证据 `{name}` 在 `{klass.name}` 的类体里被重新绑定"
            f"（`{source[:70]}`）；运行时执行的是绑定后的对象，守卫读到的定义不再是证据"
        ]
    return []


def _evidence_skip_problems(
    key: str,
    name: str,
    where: str,
    tree: ast.Module,
    klass: ast.ClassDef,
    node: ast.FunctionDef,
) -> list[str]:
    """Method-level, class-level and fixture-level skips all turn evidence into a no-op."""
    constants = _module_constants(tree)
    reasons: list[str] = []
    for decorator in node.decorator_list:
        reason = _skip_decorator_reason(decorator, constants)
        if reason:
            reasons.append(f"方法上的 {reason}")
    for owner in _class_ancestry(tree, klass):
        for decorator in owner.decorator_list:
            reason = _skip_decorator_reason(decorator, constants)
            if reason:
                reasons.append(f"类 `{owner.name}` 上的 {reason}")
        for member in owner.body:
            if not isinstance(member, ast.FunctionDef):
                continue
            if member.name not in {"setUp", "setUpClass"}:
                continue
            for mark in _reachable_skip_marks(member.body, constants):
                reasons.append(f"`{owner.name}.{member.name}` 里的 {mark}")
    for mark in _reachable_skip_marks(node.body, constants):
        reasons.append(f"正文里的 {mark}")
    if not reasons:
        return []
    return [
        f"MUTATE_SITES 的 `{key}` 证据 `{name}`（{where}）会被跳过或不算失败："
        f"{'；'.join(reasons)}；这样的用例每次都绿，不能作为证据"
    ]


TRIVIAL_ASSERT_CALLS = (
    "assertTrue",
    "assert_",
    "assertFalse",
    "assertEqual",
    "assertIs",
    "assertNotEqual",
    "assertIsNot",
    "assertIsNone",
    "assertIsNotNone",
)


def _is_assertion(node: ast.AST) -> bool:
    if isinstance(node, ast.Assert):
        return True
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr.startswith("assert")
    )


def _trivial_assertion_source(node: ast.AST) -> str:
    """Reason text when the assertion can never fail, else ""."""
    if isinstance(node, ast.Assert):
        if isinstance(node.test, ast.Constant) and bool(node.test.value):
            return ast.unparse(node).splitlines()[0]
        return ""
    if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
        return ""
    attribute = node.func.attr
    if attribute not in TRIVIAL_ASSERT_CALLS:
        return ""
    values = [
        argument.value if isinstance(argument, ast.Constant) else argument for argument in node.args
    ]
    literals = [isinstance(argument, ast.Constant) for argument in node.args]
    source = ast.unparse(node).splitlines()[0]
    trivial = False
    if attribute in {"assertTrue", "assert_"} and literals == [True]:
        trivial = bool(values[0])
    elif attribute == "assertFalse" and literals == [True]:
        trivial = not values[0]
    elif attribute in {"assertEqual", "assertIs"} and literals == [True, True]:
        trivial = values[0] == values[1]
    elif attribute in {"assertNotEqual", "assertIsNot"} and literals == [True, True]:
        trivial = values[0] != values[1]
    elif literals == [True] and attribute in {"assertIsNone", "assertIsNotNone"}:
        trivial = (values[0] is None) == (attribute == "assertIsNone")
    return source if trivial else ""


def _swallowing_try_sources(node: ast.FunctionDef) -> list[str]:
    """`try` blocks whose handlers would swallow an AssertionError raised inside them."""
    swallow = {"assertionerror", "exception", "baseexception"}
    sources: list[str] = []
    for inner in ast.walk(node):
        if not isinstance(inner, ast.Try):
            continue
        asserts = [
            visitor
            for statement in inner.body
            for visitor in ast.walk(statement)
            if _is_assertion(visitor) and not _trivial_assertion_source(visitor)
        ]
        if not asserts:
            continue
        for handler in inner.handlers:
            if handler.type is None or any(
                token in ast.unparse(handler.type).casefold() for token in swallow
            ):
                sources.append(ast.unparse(inner).splitlines()[0])
                break
    return sources


def _truncation_sources(node: ast.FunctionDef) -> list[str]:
    """Early exits that make the rest of the evidence body dead code."""
    sources: list[str] = []
    for statement in node.body[:-1]:
        if isinstance(statement, (ast.Return, ast.Raise)):
            sources.append(ast.unparse(statement).splitlines()[0])
            break
    for inner in ast.walk(node):
        if not isinstance(inner, ast.If):
            continue
        if not (isinstance(inner.test, ast.Constant) and bool(inner.test.value)):
            continue
        if any(isinstance(statement, (ast.Return, ast.Raise)) for statement in inner.body):
            sources.append(ast.unparse(inner).splitlines()[0])
            break
    return sources


def _evidence_body_problems(key: str, name: str, where: str, node: ast.FunctionDef) -> list[str]:
    """A named test is not evidence if it exits early, asserts nothing, or cannot fail."""
    problems: list[str] = []
    truncations = _truncation_sources(node)
    if truncations:
        problems.append(
            f"MUTATE_SITES 的 `{key}` 证据 `{name}`（{where}）在前面 `{truncations[0]}` 早退，"
            "剩下的正文是死代码；掏空证据必须红"
        )
    assertions = [
        inner for statement in node.body for inner in ast.walk(statement) if _is_assertion(inner)
    ]
    meaningful = [item for item in assertions if not _trivial_assertion_source(item)]
    if not assertions:
        problems.append(
            f"MUTATE_SITES 的 `{key}` 证据 `{name}`（{where}）的正文没有任何断言；"
            "被登记为证据的用例必须自己判红，不能只是被导入"
        )
    elif not meaningful:
        trivial = _trivial_assertion_source(assertions[0])
        problems.append(
            f"MUTATE_SITES 的 `{key}` 证据 `{name}`（{where}）的断言恒真（`{trivial}`），"
            "永远不会失败；掏空证据必须红"
        )
    swallowing = _swallowing_try_sources(node)
    if swallowing:
        problems.append(
            f"MUTATE_SITES 的 `{key}` 证据 `{name}`（{where}）把断言包在会吞 "
            f"`AssertionError` 的 try/except 里（`{swallowing[0]}`）；"
            "被吞掉的断言不是证据"
        )
    return problems


def _entry_defined_names(entry: Path) -> set[str]:
    """Names the entry defines: functions, classes, and bound attributes/assignments."""
    names: set[str] = set()
    tree = ast.parse(entry.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    names.add(target.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
    return names


def check_judgements(entry: Path) -> list[str]:
    """The `判定在哪` column must point at a definition the entry really has."""
    problems: list[str] = []
    defined = _entry_defined_names(entry)
    seen: set[str] = set()
    for row in MUTATE_SITES:
        if row.key in seen:
            problems.append(
                f"MUTATE_SITES 的 `{row.key}` 登记了多次；同一站点只能有一行判定，"
                "重复行会让「判定在哪」两说并存"
            )
            continue
        seen.add(row.key)
        pointers = re.findall(r"`([^`]+)`", row.judgement)
        if not pointers:
            problems.append(
                f"MUTATE_SITES 的 `{row.key}` 的「判定在哪」没有反引号指向入口定义的名字"
                "（写成 `判在` 的那个函数，例如 `step_gate`）；"
                "判定位置必须可核对，不能只写一句描述"
            )
            continue
        for pointer in pointers:
            if pointer not in defined:
                problems.append(
                    f"MUTATE_SITES 的 `{row.key}` 的「判定在哪」写着 `{pointer}`，"
                    f"但 {entry.name} 没有定义它；入口改名/删函数时必须同步这列"
                )
    return problems


def selftest() -> list[str]:
    problems: list[str] = []
    for case in SELFTEST_CASES:
        found = audit_sources(
            case.source,
            label=case.case,
            baseline=case.baseline,
            require_sites=case.require_sites,
        )
        actual_red = bool(found)
        if actual_red != case.expect_red:
            expected = "红" if case.expect_red else "绿"
            problems.append(
                f"自检用例 `{case.case}` 期望{expected}，"
                f"实际{'红' if actual_red else '绿'}：{case.note}\n"
                "自检语料是守卫自身的锚点；改规则时同步更新用例与文档表 SELFTEST_CASES"
            )
    return problems


def run_sites(entry: Path) -> tuple[list[str], str]:
    problems = check_anchor() + check_counts() + audit_evidence() + selftest()
    problems += check_judgements(entry)
    problems += audit_sources(entry.read_text(encoding="utf-8"), label=entry.name)
    return problems, f"sites: {len(MUTATE_SITES)} registered"


# ---------------------------------------------------------------------------
# Behavioural probe (`probe`)
# ---------------------------------------------------------------------------


def _load_entry(entry: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location("reference_acceptance_under_test", entry)
    if spec is None or spec.loader is None:
        raise SystemExit(f"::error::无法加载入口模块: {entry}")
    module = importlib.util.module_from_spec(spec)
    # dataclasses resolve ``cls.__module__`` while the module executes (3.14+).
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _probe_implementations(module: ModuleType) -> dict[str, Sequence[object]]:
    """case -> (callable, expectations) implementations, matched by PROBE_CASES."""

    def command_result(code: int, stdout: str) -> object:
        return module.CommandResult(("x",), code, stdout, "")

    def migration_verify_behind() -> None:
        assert module.migration_verify("head", "old") is not None, "迁移落后必须判失败"
        assert module.migration_verify("head", "head\nhead") is not None, (
            "多行 alembic_version 必须判失败"
        )
        assert module.migration_verify("head", "") is not None, "空 alembic_version 必须判失败"
        assert module.migration_verify("head", "head\n") is None, "正好在 head 必须通过"

    def gate_verdict_ok() -> None:
        ok_lines, pending = module.gate_verdict(
            command_result(0, "job dispatch startup gate: OK\nok: db\npending: engine")
        )
        assert ok_lines == ["db"], ok_lines
        assert pending == ["engine"], pending

    def gate_verdict_fail() -> None:
        try:
            module.gate_verdict(command_result(0, "job dispatch startup gate: OK\nFAIL: broken"))
        except module.StepFailureError:
            return
        raise AssertionError("FAIL 行必须判失败")

    def gate_verdict_exit() -> None:
        try:
            module.gate_verdict(command_result(1, "job dispatch startup gate: OK"))
        except module.StepFailureError:
            return
        raise AssertionError("闸门退出码非零时不能读成通过")

    def gate_verdict_fail_wins() -> None:
        try:
            module.gate_verdict(command_result(0, "job dispatch startup gate: OK\nFAIL: nope"))
        except module.StepFailureError:
            return
        raise AssertionError("FAIL 行与 OK 行同时出现时必须判失败")

    def env_gaps() -> None:
        assert module.env_gaps({"A": " ", "B": "x"}, ["A", "B"]) == ["A"]
        assert module.env_gaps({"A": "x"}, ["A"]) == []

    def dsn_identity() -> None:
        assert module.parse_dsn("mysql://u:p@h/db") is None
        assert module.parse_dsn("postgresql://u:p@h:5432/db") == ("u", "p", "h:5432")
        try:
            module.dsn_identity({}, "MISSING_DSN", consumer="probe")
        except module.StepFailureError:
            return
        raise AssertionError("空 DSN 必须判失败")

    def migration_heads() -> None:
        with tempfile.TemporaryDirectory(prefix="probe-migrations-") as directory:
            versions = Path(directory)
            (versions / "a.py").write_text(
                'revision = "a"\ndown_revision = None\n', encoding="utf-8"
            )
            (versions / "b.py").write_text(
                'revision = "b"\ndown_revision = "a"\n', encoding="utf-8"
            )
            head, _parents = module.migration_heads(versions)
            assert head == "b", head
            (versions / "c.py").write_text(
                'revision = "c"\ndown_revision = None\n', encoding="utf-8"
            )
            try:
                module.migration_heads(versions)
            except module.StepFailureError:
                return
            raise AssertionError("两个 head 必须判失败")

    def bucket_names() -> None:
        assert module.bucket_names(
            types.SimpleNamespace(
                env={
                    "SPEECHNEXUS_OBJECT_STORAGE_BUCKET": "same",
                    "SPEECHNEXUS_TRANSCRIPTION_INPUT_BUCKET": "same",
                }
            )
        ) == ["same"], "同名桶必须去重成一条"
        try:
            module.bucket_names(types.SimpleNamespace(env={}))
        except module.StepFailureError:
            return
        raise AssertionError("三个桶名键全空必须判失败")

    return {
        "migration_verify-behind": (migration_verify_behind, ()),
        "gate_verdict-ok": (gate_verdict_ok, ()),
        "gate_verdict-fail": (gate_verdict_fail, ()),
        "gate_verdict-exit": (gate_verdict_exit, ()),
        "gate_verdict-fail-wins": (gate_verdict_fail_wins, ()),
        "env_gaps": (env_gaps, ()),
        "dsn_identity": (dsn_identity, ()),
        "migration_heads": (migration_heads, ()),
        "bucket_names": (bucket_names, ()),
    }


def _function_raises(entry: Path, name: str) -> bool:
    tree = ast.parse(entry.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return any(isinstance(inner, ast.Raise) for inner in ast.walk(node))
    return False


def run_probe(entry: Path) -> tuple[list[str], str]:
    problems = check_anchor()
    module = _load_entry(entry)
    implementations = _probe_implementations(module)
    declared = {row.case: row for row in PROBE_CASES}
    for case in sorted(set(declared) - set(implementations)):
        problems.append(f"PROBE_CASES 登记了 `{case}` 但没有实现；删实现要同时删登记与文档表")
    for case in sorted(set(implementations) - set(declared)):
        problems.append(
            f"probe 实现 `{case}` 不在 PROBE_CASES 里登记；新增用例必须同时登记（含文档表）"
        )
    for case, row in declared.items():
        if case not in implementations:
            continue
        if not hasattr(module, row.function):
            problems.append(
                f"probe `{case}` 找不到入口函数 `{row.function}`；入口改名/删除后要同步 "
                "PROBE_CASES 与文档表，再确认语义由谁守护"
            )
            continue
        if row.must_raise and not _function_raises(entry, row.function):
            problems.append(
                f"PROBE_CASES 登记的判定函数 `{row.function}` 声明用 `raise` 拒绝失败输入，"
                "但函数体已经不含任何 `raise`；判据可能被换成了静默返回"
            )
        implementation = implementations[case][0]
        try:
            implementation()  # type: ignore[operator]
        except AssertionError as error:
            reason = str(error) or row.expectation
            problems.append(
                f"probe `{case}` 失败: {reason}\n"
                f"该函数（{row.function}）不再拒绝必须失败的输入；先确认这是缺陷再改 probe"
            )
        except TypeError as error:
            problems.append(
                f"probe `{case}` 与入口签名不匹配: {error}\n"
                f"签名变化时同步 probe 用例、PROBE_CASES 与文档表（`{row.function}`）"
            )
    return problems, f"probe: {len(declared)} cases"


# ---------------------------------------------------------------------------
# Workflow wiring (`wiring`)
# ---------------------------------------------------------------------------

_JOB_HEADER = "  reference_acceptance:"
_STEP_PREFIX = "      - name: "


def _job_block(workflow: str) -> str:
    lines = workflow.splitlines()
    try:
        start = next(index for index, line in enumerate(lines) if line.rstrip() == _JOB_HEADER)
    except StopIteration:
        return ""
    for index in range(start + 1, len(lines)):
        line = lines[index]
        if re.match(r"^  [a-zA-Z_][\w-]*:\s*$", line):
            return "\n".join(lines[start:index])
    return "\n".join(lines[start:])


def _step_blocks(job: str) -> list[tuple[str, str]]:
    blocks: list[tuple[str, str]] = []
    current_name: str | None = None
    current: list[str] = []
    for line in job.splitlines():
        if line.startswith(_STEP_PREFIX):
            if current_name is not None:
                blocks.append((current_name, "\n".join(current)))
            current_name = line[len(_STEP_PREFIX) :].strip().strip('"')
            current = []
            continue
        if current_name is not None:
            current.append(line)
    if current_name is not None:
        blocks.append((current_name, "\n".join(current)))
    return blocks


def _run_scripts(step: str) -> list[str]:
    scripts: list[str] = []
    lines = step.splitlines()
    index = 0
    while index < len(lines):
        if re.match(r"^        run: \|\s*$", lines[index]):
            body: list[str] = []
            index += 1
            while index < len(lines):
                line = lines[index]
                if line.strip() and not line.startswith("          "):
                    break
                body.append(line[10:] if line.startswith("          ") else "")
                index += 1
            scripts.append("\n".join(body).strip("\n"))
            continue
        index += 1
    return scripts


# Statements that only set up the shell before the guard runs. Requiring the guard
# command to be the very first line made a correct `set -euo pipefail` prelude red.
# The separator is `\s|$` rather than `\b`: `:` and the space after it are both
# non-word characters, so `\b` can never match the standard `: "${VAR:?}"` guard.
GUARD_PRELUDE = re.compile(r"^(set|export|cd|umask|unset|:)(\s|$)")


def _executed_lines(script: str) -> list[str]:
    """Command lines of one `run:` block, in order, without blanks and comments."""
    return [
        line.strip()
        for line in script.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


def run_wiring(workflow_path: Path) -> tuple[list[str], str]:
    problems = check_anchor() + check_counts()
    job = _job_block(workflow_path.read_text(encoding="utf-8"))
    if not job:
        return [
            f"`{workflow_path.relative_to(ROOT)}` 里找不到 `{_JOB_HEADER.strip()}` 作业"
        ], "wiring: 0"
    if "timeout-minutes: 5" not in job:
        problems.append("reference_acceptance 作业缺少 `timeout-minutes: 5` 上限")
    steps = _step_blocks(job)
    guard_invocations: dict[str, str] = {}
    for name, step in steps:
        for script in _run_scripts(step):
            lines = _executed_lines(script)
            for position, line in enumerate(lines):
                for candidate in ("sites", "probe", "log", "wiring"):
                    if f"ci-entry-guard.py {candidate}" not in line:
                        continue
                    if not line.startswith("python3 scripts/dev-env/ci-entry-guard.py"):
                        problems.append(
                            f"步骤 `{name}` 的 `{candidate}` 不是直接执行的命令"
                            f"（写成 `{line[:70]}`）；`echo` 提到命令不算执行"
                        )
                    else:
                        earlier = [
                            other for other in lines[:position] if not GUARD_PRELUDE.match(other)
                        ]
                        if earlier:
                            problems.append(
                                f"步骤 `{name}` 里 `{candidate}` 前面还有会先执行的命令 "
                                f"`{earlier[0][:70]}`；只有 "
                                "`set`/`export`/`cd`/`umask`/`unset`/`: ` 这类环境前置可以出现在"
                                "守卫命令之前"
                            )
                    if candidate in guard_invocations:
                        problems.append(
                            f"子命令 `{candidate}` 在步骤 `{name}` 与 "
                            f"`{guard_invocations[candidate]}` 重复执行"
                        )
                    guard_invocations[candidate] = name
    expected = {"sites": "", "probe": "", "log": "", "wiring": ""}
    for command in expected:
        if command not in guard_invocations:
            problems.append(
                f"作业没有以第一条被执行的命令运行 `ci-entry-guard.py {command}`；"
                "四条守卫子命令都必须真实执行（缺一条就等于关掉一类结构检查）"
            )
    control = dict(steps).get("Reject a corrupted environment instead of passing quietly", "")
    if not control:
        problems.append(
            "缺少负对照步骤 `Reject a corrupted environment instead of passing quietly`"
        )
    else:
        for marker in ("reference-acceptance.py --dry-run --env-file", "FAIL step=tools"):
            if marker not in control:
                problems.append(f"负对照步骤缺少 `{marker}`")
    return problems, f"wiring: {len(guard_invocations)} guard steps"


# ---------------------------------------------------------------------------
# Output contract (`log`)
# ---------------------------------------------------------------------------

_STEP_LINE = re.compile(r"^\[(\d+)/(\d+)\] (\w+) — ", re.MULTILINE)
_HEADER = re.compile(
    r"^steps=(\d+) order=([\w, ]+) mode=(dry-run|apply) gate=(compose|direct)$", re.MULTILINE
)
_CONCLUSION = re.compile(r"^\s+结论: (PLAN|PASS|FAIL) — ", re.MULTILINE)
_DEFERRAL = re.compile(r"^\s+待输入: (.+)$", re.MULTILINE)
_RECORDS = re.compile(r"^步骤记录: (.+)$", re.MULTILINE)
_VERDICT = re.compile(r"^reference acceptance: (DRY-RUN PASS|PASS|FAIL)[^\n]*$", re.MULTILINE)
_PENDING_COUNT = re.compile(r"pending=(\d+)")


def _deferral_matches(deferred: str) -> list[str]:
    folded = deferred.casefold()
    return [
        row.slug
        for row in DECLARED_DEFERRALS
        if all(word.casefold() in folded for word in row.words)
        and not any(word.casefold() in folded for word in row.exclude)
    ]


def check_log(
    log: Path, step_order: Sequence[str], *, expect_mode: str = "dry-run"
) -> tuple[list[str], str]:
    text = log.read_text(encoding="utf-8")
    problems: list[str] = []
    header = _HEADER.search(text)
    if header is None:
        problems.append(
            "缺少机器可读首行 `steps=<N> order=<csv> mode=<dry-run|apply> gate=<mode>`；"
            "契约见 docs/operations/reference-acceptance.md 的「机器可读输出（CI 契约）」"
        )
    else:
        declared = [name.strip() for name in header.group(2).split(",")]
        if declared != list(step_order):
            problems.append(
                f"首行 order={declared} 与契约 {list(step_order)} 不一致；"
                "同步文档的步骤顺序与 ci-entry-guard.py 的 EXPECTED_STEP_ORDER"
            )
        if int(header.group(1)) != len(step_order):
            problems.append(f"首行 steps={header.group(1)} 与契约步骤数 {len(step_order)} 不一致")
        if header.group(3) != expect_mode:
            problems.append(f"首行 mode={header.group(3)} 与本次检查的 {expect_mode} 不一致")
    seen = [match.group(3) for match in _STEP_LINE.finditer(text)]
    if seen != list(step_order):
        problems.append(
            f"步骤行序列 {seen} 与契约 {list(step_order)} 不一致；"
            "契约是 `[i/N] <ident> — <title>` 首行"
        )
    totals = {int(match.group(2)) for match in _STEP_LINE.finditer(text)}
    if totals and totals != {len(step_order)}:
        problems.append(f"步骤行分母 {sorted(totals)} 与契约步骤数 {len(step_order)} 不一致")
    conclusions = _CONCLUSION.findall(text)
    if "FAIL" in conclusions:
        problems.append("日志里出现 `结论: FAIL`，但入口以 0 退出")
    if len(conclusions) < len(step_order):
        problems.append(
            f"只有 {len(conclusions)} 条 `结论:` 行，契约要求每步一条（{len(step_order)}）"
        )
    if expect_mode == "dry-run":
        claimed = sorted({outcome for outcome in conclusions if outcome != "PLAN"})
        if claimed:
            problems.append(
                f"dry-run 的步骤结论必须是 PLAN，实际出现 {claimed}；"
                "dry-run 不执行写命令，声明 PASS 即虚报"
            )
    records = _RECORDS.search(text)
    if records is None:
        problems.append("缺少 `步骤记录: <ident>=<outcome>` 行")
    else:
        pairs = [item.split("=", 1) for item in records.group(1).split() if "=" in item]
        recorded = [(ident, outcome) for ident, outcome in pairs]
        if [ident for ident, _outcome in recorded] != list(step_order):
            problems.append(
                f"`步骤记录` 的步骤 id 序列 {[ident for ident, _ in recorded]} 与契约 "
                f"{list(step_order)} 不一致"
            )
        mismatched = [
            f"{ident}={outcome}"
            for (ident, outcome), conclusion in zip(recorded, conclusions, strict=False)
            if outcome != conclusion
        ]
        if mismatched:
            problems.append(f"`步骤记录` 与逐条 `结论:` 不一致: {mismatched}")
    deferrals = [match.group(1).strip() for match in _DEFERRAL.finditer(text)]
    unregistered: list[str] = []
    ambiguous: list[str] = []
    for deferred in deferrals:
        matches = _deferral_matches(deferred)
        if not matches:
            unregistered.append(deferred)
        elif len(matches) > 1:
            ambiguous.append(f"{deferred} -> {matches}")
    if ambiguous:
        problems.append(
            "一条延后项同时匹配多个登记；给相关登记补区分词或 exclude"
            "（登记表见文档块 DECLARED_DEFERRALS）"
        )
    if unregistered:
        declared_rows = "\n".join(
            f"- `{deferral.slug}`: 必须同时出现 {list(deferral.words)}，"
            f"且不含 {list(deferral.exclude) or '（无排除词）'}"
            for deferral in DECLARED_DEFERRALS
        )
        problems.append(
            "未登记的延后项（新延后输入必须登记，不能静默放宽 PASS 的含义）：\n- "
            + "\n- ".join(unregistered)
            + f"\n已登记:\n{declared_rows}\n"
            + _hint(str(SELF.relative_to(ROOT)), "DECLARED_DEFERRALS", "一行登记 + 稳定词 + 排除词")
        )
    verdict = _VERDICT.search(text)
    if verdict is None:
        problems.append("缺少结论行 `reference acceptance: DRY-RUN PASS steps=N`")
    else:
        expected_verdict = "DRY-RUN PASS" if expect_mode == "dry-run" else "PASS"
        if verdict.group(1) != expected_verdict:
            problems.append(
                f"{expect_mode} 结论必须是 `{expected_verdict}`，实际 `{verdict.group(1)}`"
            )
        counted = _PENDING_COUNT.search(verdict.group(0))
        pending_total = int(counted.group(1)) if counted else 0
        if pending_total != len(deferrals):
            problems.append(
                f"结论行 pending={pending_total} 与日志中 {len(deferrals)} 条 `待输入:` 不一致"
            )
    return problems, f"log: {len(deferrals)} deferrals"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _run_step_order(entry: Path) -> list[str]:
    module = _load_entry(entry)
    return [step.ident for step in module.STEPS]


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Structure guard for the reference-acceptance entry"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    sites = subparsers.add_parser("sites", help="mutating sites, read-only argv, registries")
    sites.add_argument("--entry", type=Path, default=ENTRY_DEFAULT)

    probe_parser = subparsers.add_parser("probe", help="exercise registered judgement functions")
    probe_parser.add_argument("--entry", type=Path, default=ENTRY_DEFAULT)

    log_parser = subparsers.add_parser("log", help="verify the documented dry-run output contract")
    log_parser.add_argument("--log", type=Path, required=True)
    log_parser.add_argument("--entry", type=Path, default=ENTRY_DEFAULT)
    log_parser.add_argument(
        "--expect-mode",
        choices=("dry-run", "apply"),
        default="dry-run",
        help="日志来自哪种运行；合同格式两种都要能解析",
    )

    wiring = subparsers.add_parser("wiring", help="verify the workflow still runs every guard")
    wiring.add_argument("--workflow", type=Path, default=WORKFLOW_DEFAULT)

    tables = subparsers.add_parser("tables", help="print or write the registered tables")
    tables.add_argument(
        "--write", action="store_true", help="rewrite the documentation anchor block"
    )
    tables.add_argument("--doc", type=Path, default=CONTRACT_DOC)

    arguments = parser.parse_args(argv)
    try:
        if arguments.command == "sites":
            problems, summary = run_sites(arguments.entry)
        elif arguments.command == "probe":
            problems, summary = run_probe(arguments.entry)
        elif arguments.command == "wiring":
            problems, summary = run_wiring(arguments.workflow)
        elif arguments.command == "tables":
            if arguments.write:
                write_anchor(arguments.doc)
                print(f"tables: 已写入 {_display(arguments.doc)}")
                return 0
            print(render_tables())
            return 0
        else:
            order = _run_step_order(arguments.entry)
            if order != list(EXPECTED_STEP_ORDER):
                problems = [
                    f"入口 STEPS 顺序 {order} 与契约 {list(EXPECTED_STEP_ORDER)} 不一致；"
                    "同步文档的步骤顺序与 EXPECTED_STEP_ORDER"
                ]
                summary = "log: step order mismatch"
            else:
                log_problems, summary = check_log(
                    arguments.log, EXPECTED_STEP_ORDER, expect_mode=arguments.expect_mode
                )
                problems = check_anchor() + check_counts() + log_problems
    except SystemExit as error:
        print(f"::error::{error}")
        return 1
    except Exception as error:
        print(f"::error::{arguments.command} 守卫崩溃: {type(error).__name__}: {error}")
        return 1
    if problems:
        _report(problems)
        return 1
    print(f"{arguments.command}: OK ({summary})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
