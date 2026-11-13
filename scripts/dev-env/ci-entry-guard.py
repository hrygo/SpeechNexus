#!/usr/bin/env python3
"""Audit the reference-acceptance entry point from inside its CI job.

Why this exists: `scripts/dev-env/reference-acceptance.py` is the operator's only
answer to "can this reference deployment run", and its `--dry-run` never executes
the state-changing steps. Three guards run here so a corrupted entry point fails a
PR instead of only an apply run:

* ``sites``  — static audit. Every ``<ctx>.mutate(...)`` site must be judged, and
  every ``<ctx>.read(...)`` must stay read-only (no ``compose run/up`` and no
  ``sh -c``).
* ``probe``  — behavioural probe. Imports the entry as a module and calls its
  pure judgement functions with inputs that must fail; this is what catches
  "emptied apply-path judgement" such as ``migration_verify`` returning ``None``
  forever.
* ``log``    — the documented, machine-readable dry-run output contract plus the
  declared deferral set, so the job cannot silently widen what "PASS" means.

Contract and registration locations:

* machine-readable output: ``docs/operations/reference-acceptance.md``
  («机器可读输出» section);
* the two registration tables are the ``DECLARED_DEFERRALS`` and
  ``REGISTERED_UNAUDITED`` constants below; every failure message prints the
  table and its line so a maintainer can register a legitimate new shape with one
  line and a mandatory reason.

Honest boundary: this file proves *structure*, not deployment behaviour. It
cannot see a judgement whose condition was inverted but still reads ``.ok``, it
does not execute Docker/database paths, and it only reasons about the forms
listed in ``ACCEPTED_JUDGEMENTS``.
"""

from __future__ import annotations

import argparse
import ast
import importlib.util
import re
import sys
import tempfile
import types
from collections.abc import Callable, Iterable, Mapping, Sequence
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).resolve().parents[2]
SELF = Path(__file__).resolve()
ENTRY_DEFAULT = ROOT / "scripts/dev-env/reference-acceptance.py"
CONTRACT_DOC = "docs/operations/reference-acceptance.md"

# The dry-run step order the operator runbook documents. It is deliberately a
# second copy: the entry's own STEPS tuple and the CI log must both match it, so a
# reordering in the entry (the B3 regression) cannot pass by agreeing with itself.
EXPECTED_STEP_ORDER = ("tools", "migrate", "roles", "buckets", "stack", "gate", "health")

# Deferred inputs the dry-run may report. Keyed by a stable slug; the value is the
# set of words that identify that deferral. A log line is registered when it
# contains every word of exactly one entry, so rewording the explanation stays
# green while a different deferral still has to be registered here with one line.
# Declare words, not sentences: a sentence would make the wording a merge gate.
DECLARED_DEFERRALS: dict[str, tuple[str, ...]] = {
    "gateway-requires-running-stack": ("gateway", "healthy"),
}

# Sites the static audit cannot decide, registered explicitly with a mandatory
# reason. Key format: ``<function>:<unparse of the site>[0:60]``.
REGISTERED_UNAUDITED: dict[str, str] = {}

ACCEPTED_JUDGEMENTS = (
    "1) `var = ctx.mutate(...)` 后在同一个函数内用条件位置判定 `.ok`/`.code`",
    "   （if / while / assert / match / IfExp / BoolOp / Compare / 推导式 if / Not 均可）；",
    "2) 先绑局部再判定：`code = var.code` / `flag = var.ok`，再在条件位置使用该局部；",
    "3) 位置实参或关键字实参交给辅助函数，且该函数对应的形参被读 `.ok`",
    "   （本模块或仓库其它模块的 `def <name>(...)` 均可，按形参名匹配）；",
    "4) 直接 `if ctx.mutate(...).ok:` / `if ctx.mutate(...).code:`；",
    "5) 实在无法静态判定的形态，在 REGISTERED_UNAUDITED 登记一行并写明理由。",
)

READ_ONLY_RULE = (
    "`ctx.read(...)` 只允许只读命令：argv 里出现 compose 的 run/up/start/down/restart/rm "
    "或 sh/bash/zsh -c 一律判红（`uv run …` 例外）。dry-run 会真的执行 read 里的命令。"
)


class AuditError(Exception):
    """A guard failed with a message that is meant to become a ``::error::``."""


def _report(problems: Sequence[str]) -> int:
    for problem in problems:
        for line in problem.splitlines():
            print(f"::error::{line}")
    return 1


def _constant_line(source: Path, name: str) -> int:
    """Return the 1-based line where ``name =`` starts, for a stable pointer."""
    for index, line in enumerate(source.read_text(encoding="utf-8").splitlines(), start=1):
        if line.startswith(name) or line.lstrip().startswith(f"{name}:"):
            return index
    return 0


def _registration_hint(table: str) -> str:
    line = _constant_line(SELF, table)
    return f"登记处: {SELF.relative_to(ROOT)}:{line} 的表 {table}（一行登记 + 必填理由）"


# ---------------------------------------------------------------------------
# Static audit (`sites`)
# ---------------------------------------------------------------------------


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


def _parent_map(nodes: Iterable[ast.AST]) -> dict[int, ast.AST]:
    parents: dict[int, ast.AST] = {}
    for node in nodes:
        for child in ast.iter_child_nodes(node):
            parents[id(child)] = node
    return parents


def _in_judgement(node: ast.AST, parents: Mapping[int, ast.AST]) -> bool:
    """Whether ``node`` decides control flow (test/subject), not just sits in a body."""
    child = node
    current = parents.get(id(node))
    while current is not None:
        if (
            isinstance(current, (ast.If, ast.While, ast.Assert, ast.IfExp))
            and child is current.test
        ):
            return True
        if isinstance(current, ast.Match) and child is current.subject:
            return True
        child, current = current, parents.get(id(current))
    return False


class _ModuleIndex:
    """Function -> (ordered formal params, the params whose ``.ok`` is read).

    Resolution is per parameter name, not per function: handing the result to a
    helper that reads some *other* parameter's ``.ok`` is not a judgement. When the
    callee is not defined in the audited file it is looked up across the repo, so a
    judgement moved into another module stays green.
    """

    def __init__(self, search_root: Path) -> None:
        self.search_root = search_root
        self._local: dict[str, tuple[list[str], set[str]]] = {}
        self._remote: dict[str, tuple[list[str], set[str]] | None] = {}

    def index_local(self, tree: ast.Module) -> None:
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                self._local[node.name] = _ok_parameters(node)

    def parameters(self, name: str) -> tuple[list[str], set[str]] | None:
        if name in self._local:
            return self._local[name]
        if name not in self._remote:
            self._remote[name] = self._scan_repo(name)
        return self._remote[name]

    def _scan_repo(self, name: str) -> tuple[list[str], set[str]] | None:
        needle = f"def {name}("
        for path in sorted(self.search_root.rglob("*.py")):
            if any(
                part in {".git", ".venv", "node_modules", ".runtime", "__pycache__"}
                for part in path.parts
            ):
                continue
            try:
                content = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            if needle not in content:
                continue
            try:
                tree = ast.parse(content)
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
                    order, ok_names = _ok_parameters(node)
                    if ok_names:
                        return order, ok_names
        return None


def _ok_parameters(function: ast.FunctionDef | ast.AsyncFunctionDef) -> tuple[list[str], set[str]]:
    """Ordered positional-or-keyword params plus the ones whose ``.ok`` is read."""
    order = [argument.arg for argument in (*function.args.posonlyargs, *function.args.args)]
    order.extend(argument.arg for argument in function.args.kwonlyargs)
    names = set(order)
    reading: set[str] = set()
    for node in _own_nodes(function):
        if (
            isinstance(node, ast.Attribute)
            and node.attr == "ok"
            and isinstance(node.value, ast.Name)
            and node.value.id in names
        ):
            reading.add(node.value.id)
    return order, reading


def _attribute_on(node: ast.AST, name: str) -> bool:
    return (
        isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == name
    )


def _judged_name(
    name: str, nodes: Sequence[ast.AST], parents: Mapping[int, ast.AST], index: _ModuleIndex
) -> bool:
    """Whether ``name`` is judged in a condition position, following one-step aliases."""
    aliases = {name}
    for _ in range(4):
        grown = False
        for node in nodes:
            target: ast.expr | None = None
            value: ast.expr | None = None
            if isinstance(node, ast.AnnAssign):
                target, value = node.target, node.value
            elif isinstance(node, ast.Assign) and len(node.targets) == 1:
                target, value = node.targets[0], node.value
            if not isinstance(target, ast.Name) or value is None:
                continue
            source: str | None = None
            if isinstance(value, ast.Name) and value.id in aliases:
                source = value.id
            elif isinstance(value, ast.Attribute) and isinstance(value.value, ast.Name):
                if value.value.id in aliases:
                    source = value.value.id
            elif isinstance(value, ast.Call):
                source = None
            if source is not None and target.id not in aliases:
                aliases.add(target.id)
                grown = True
        if not grown:
            break
    for node in nodes:
        if not isinstance(node, ast.Name) or node.id not in aliases:
            continue
        parent = parents.get(id(node))
        if (
            isinstance(parent, ast.Attribute)
            and parent.attr in {"ok", "code"}
            and _in_judgement(parent, parents)
        ):
            return True
        if _in_judgement(node, parents):
            return True
        if _handed_to_judging_helper(node, parent, index):
            return True
    return False


def _handed_to_judging_helper(node: ast.expr, parent: ast.AST | None, index: _ModuleIndex) -> bool:
    """A local bound to the result still counts when a judging helper receives it."""
    if not isinstance(parent, ast.Call):
        return False
    callee = parent.func
    name = (
        callee.id
        if isinstance(callee, ast.Name)
        else (callee.attr if isinstance(callee, ast.Attribute) else "")
    )
    if not name:
        return False
    resolved = index.parameters(name)
    if resolved is None:
        return False
    order, ok_names = resolved
    plain = [argument for argument in parent.args if not isinstance(argument, ast.Starred)]
    for position, argument in enumerate(plain):
        if argument is node and position < len(order):
            return order[position] in ok_names
    for keyword in parent.keywords:
        if keyword.value is node and keyword.arg is not None:
            return keyword.arg in ok_names
    return False


def _site_judged_by_call(
    site: ast.Call, nodes: Sequence[ast.AST], index: _ModuleIndex
) -> tuple[bool, str]:
    """Whether the mutate result is handed to a helper that judges that argument."""
    parents = _parent_map(nodes)
    parent = parents.get(id(site))
    if not isinstance(parent, ast.Call):
        return False, "not an argument"
    callee = parent.func
    name = (
        callee.id
        if isinstance(callee, ast.Name)
        else (callee.attr if isinstance(callee, ast.Attribute) else "")
    )
    if not name:
        return False, "调用目标不是具名函数，无法按形参匹配"
    resolved = index.parameters(name)
    if resolved is None:
        return False, f"helper {name} 未解析出读 `.ok` 的形参（若是新助手，确认它在仓库内可解析）"
    order, ok_names = resolved
    plain_args = [argument for argument in parent.args if not isinstance(argument, ast.Starred)]
    for position, argument in enumerate(parent.args):
        if argument is not site:
            continue
        if isinstance(argument, ast.Starred):
            return False, "以 *args 展开传递，无法按形参名匹配"
        if position >= len(plain_args):
            return False, "与 *args 混用，无法按形参名匹配"
        if position >= len(order):
            return False, "位置实参超出已声明形参"
        formal = order[position]
        return formal in ok_names, f"位置实参 -> 形参 {formal}"
    for keyword in parent.keywords:
        if keyword.value is site:
            if keyword.arg is None:
                return False, "以 **kwargs 传递，无法按形参名匹配"
            return keyword.arg in ok_names, f"关键字实参 -> 形参 {keyword.arg}"
    return False, "不是本调用的直接实参"


def _classify_site(
    site: ast.Call,
    function_name: str,
    nodes: Sequence[ast.AST],
    parents: Mapping[int, ast.AST],
    index: _ModuleIndex,
) -> tuple[str, bool, str]:
    """Return (kind, judged, detail)."""
    parent = parents.get(id(site))
    judged, detail = False, ""
    if isinstance(parent, ast.AnnAssign) and isinstance(parent.target, ast.Name):
        return (
            "annotated assignment",
            _judged_name(parent.target.id, nodes, parents, index),
            parent.target.id,
        )
    if isinstance(parent, ast.Assign):
        if len(parent.targets) == 1 and isinstance(parent.targets[0], ast.Name):
            name = parent.targets[0].id
            return ("assignment", _judged_name(name, nodes, parents, index), name)
        return ("assignment to non-name target", False, ast.unparse(parent.targets[0])[:40])
    if isinstance(parent, ast.Attribute) and parent.attr in {"ok", "code"}:
        return ("inline attribute", _in_judgement(parent, parents), f".{parent.attr}")
    if isinstance(parent, ast.Call):
        judged, detail = _site_judged_by_call(site, nodes, index)
        return ("argument", judged, detail)
    if isinstance(parent, ast.Return):
        return ("return", False, "结果从函数返回，调用方的判定不在本函数内")
    if isinstance(parent, ast.Expr):
        return ("discarded", False, "结果被丢弃")
    if isinstance(parent, ast.Starred):
        return ("starred", False, "以 *args 展开传递")
    return ("other", False, f"父节点 {type(parent).__name__} 不是可识别形态")


_COMPOSE_WRITE_TOKENS = frozenset({"run", "up", "start", "down", "restart", "rm", "create"})
_SHELLS = frozenset({"sh", "bash", "zsh", "dash"})


def _read_violations(call: ast.Call, function_name: str) -> str | None:
    """Refuse a ``ctx.read`` whose argv carries a state-changing command."""
    tokens = [
        node.value
        for node in ast.walk(call)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    ]
    if not tokens:
        return None
    words = {token.strip().lower() for token in tokens if token.strip()}
    shell_with_command = bool(words & _SHELLS) and "-c" in words
    # `uv run …` is how the roles step reads rendered GRANT statements; that pairing
    # is exempt. Any other `run` (compose run, docker run) is exactly the write this
    # rule forbids, and `read` really does execute its argv during the dry-run.
    write = sorted(word for word in _COMPOSE_WRITE_TOKENS if word in words)
    if "uv" in words:
        write = [word for word in write if word != "run"]
    if not write and not shell_with_command:
        return None
    found = ", ".join(write) or f"{', '.join(sorted(words & _SHELLS))} -c"
    return (
        f"{function_name}:{call.lineno} `ctx.read(...)` 携带写命令（{found}）。\n{READ_ONLY_RULE}"
    )


def audit_sites(
    entry: Path, search_root: Path, *, require_sites: bool = True
) -> tuple[list[str], str]:
    source = entry.read_text(encoding="utf-8")
    try:
        tree = ast.parse(source)
    except SyntaxError as error:
        return [f"{entry}: 无法解析入口脚本：{error}"], "0 sites"
    index = _ModuleIndex(search_root)
    index.index_local(tree)
    problems: list[str] = []
    checked = registered = 0
    functions = [
        node for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]
    seen: set[int] = set()
    for function in functions:
        nodes = _own_nodes(function)
        parents = _parent_map(nodes)
        for node in nodes:
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            attribute = func.attr if isinstance(func, ast.Attribute) else None
            receiver = (
                func.value.id
                if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name)
                else None
            )
            if attribute == "read":
                violation = _read_violations(node, function.name)
                if violation:
                    problems.append(violation)
                continue
            if attribute != "mutate" or receiver is None:
                continue
            if id(node) in seen:
                problems.append(f"{function.name}:{node.lineno} 同一站点被重复计数")
                continue
            seen.add(id(node))
            kind, judged, detail = _classify_site(node, function.name, nodes, parents, index)
            key = f"{function.name}:{ast.unparse(node)[:60]}"
            reason = REGISTERED_UNAUDITED.get(key)
            if reason is not None:
                if not reason.strip():
                    problems.append(
                        f"{function.name}:{node.lineno} REGISTERED_UNAUDITED[{key!r}] 的理由为空"
                    )
                registered += 1
                continue
            if judged:
                checked += 1
                continue
            problems.append(
                f"{function.name}:{node.lineno} {kind} 未被判定（{detail}）；"
                f"`ctx.mutate(...)` 的结果必须被判定。\n"
                f"接受写法：\n" + "\n".join(ACCEPTED_JUDGEMENTS) + "\n"
                f"{_registration_hint('REGISTERED_UNAUDITED')}\n"
                f'登记行: REGISTERED_UNAUDITED[{key!r}] = "<必填理由>"\n'
                f"契约: {CONTRACT_DOC}"
            )
    if not seen and require_sites:
        problems.append("入口里没有 `ctx.mutate(...)` 站点；扫描会变成空转，先确认入口仍执行写命令")
    used = {
        f"{fn.name}:{ast.unparse(node)[:60]}"
        for fn in functions
        for node in _own_nodes(fn)
        if isinstance(node, ast.Call)
    }
    for key in REGISTERED_UNAUDITED:
        if key not in used:
            print(f"::warning::REGISTERED_UNAUDITED 的登记 {key!r} 不再匹配任何站点，可以删除")
    summary = f"mutating sites: {checked} judged, {registered} registered, {len(seen)} total"
    return problems, summary


# ---------------------------------------------------------------------------
# Behavioural probe (`probe`)
# ---------------------------------------------------------------------------


def _load_entry(entry: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location("reference_acceptance_under_test", entry)
    if spec is None or spec.loader is None:
        raise AuditError(f"无法加载入口模块: {entry}")
    module = importlib.util.module_from_spec(spec)
    # dataclasses resolve ``cls.__module__`` while the module is still executing
    # (Python 3.14+), so the module must already be importable by name.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _probe_checks(entry: Path) -> list[tuple[str, Callable[[], None]]]:
    module = _load_entry(entry)

    def migration_verify_behind() -> None:
        assert module.migration_verify("head", "old") is not None, "迁移落后必须判失败"
        assert module.migration_verify("head", "head\nhead") is not None, (
            "多行 alembic_version 必须判失败"
        )
        assert module.migration_verify("head", "") is not None, "空 alembic_version 必须判失败"
        assert module.migration_verify("head", "head\n") is None, "正好在 head 必须通过"

    def gate_verdict_ok() -> None:
        result = module.CommandResult(
            ("x",), 0, "job dispatch startup gate: OK\nok: db\npending: engine", ""
        )
        ok_lines, pending = module.gate_verdict(result)
        assert ok_lines == ["db"], ok_lines
        assert pending == ["engine"], pending

    def gate_verdict_fail() -> None:
        failed = module.CommandResult(("x",), 0, "job dispatch startup gate: OK\nFAIL: broken", "")
        try:
            module.gate_verdict(failed)
        except module.StepFailureError:
            pass
        else:
            raise AssertionError("FAIL 行必须判失败")

    def gate_verdict_nonzero_exit() -> None:
        nonzero = module.CommandResult(("x",), 1, "job dispatch startup gate: OK", "")
        try:
            module.gate_verdict(nonzero)
        except module.StepFailureError:
            pass
        else:
            raise AssertionError("闸门退出码非零时不能读成通过")

    def gate_fail_beats_ok() -> None:
        both = module.CommandResult(("x",), 0, "job dispatch startup gate: OK\nFAIL: nope", "")
        try:
            module.gate_verdict(both)
        except module.StepFailureError:
            pass
        else:
            raise AssertionError("FAIL 行与 OK 行同时出现时必须判失败")

    def env_gaps_detects_blanks() -> None:
        assert module.env_gaps({"A": " ", "B": "x"}, ["A", "B"]) == ["A"]
        assert module.env_gaps({"A": "x"}, ["A"]) == []

    def dsn_identity_rejects() -> None:
        assert module.parse_dsn("mysql://u:p@h/db") is None
        assert module.parse_dsn("u:p@h/db") is None
        assert module.parse_dsn("postgresql://u:p@h:5432/db") == ("u", "p", "h:5432")
        context = module.Context(
            env={},
            env_file=entry.parent / "unused.env",
            compose_file=entry.parent / "unused.yaml",
            project="probe",
            runner=module.Runner(),
            dry_run=True,
            gate_mode="compose",
        )
        try:
            module.dsn_identity(context.env, "MISSING_DSN", consumer="probe")
        except module.StepFailureError:
            pass
        else:
            raise AssertionError("空 DSN 必须判失败")

    def migration_graph_single_head() -> None:
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
                pass
            else:
                raise AssertionError("两个 head 必须判失败")

    def bucket_names_derives() -> None:
        context = types.SimpleNamespace(
            env={
                "SPEECHNEXUS_OBJECT_STORAGE_BUCKET": "same",
                "SPEECHNEXUS_TRANSCRIPTION_INPUT_BUCKET": "same",
            }
        )
        assert module.bucket_names(context) == ["same"]
        empty = types.SimpleNamespace(env={})
        try:
            module.bucket_names(empty)
        except module.StepFailureError:
            pass
        else:
            raise AssertionError("三个桶名键全空必须判失败")

    return [
        ("migration_verify", migration_verify_behind),
        ("gate_verdict_ok", gate_verdict_ok),
        ("gate_verdict_fail", gate_verdict_fail),
        ("gate_verdict_nonzero_exit", gate_verdict_nonzero_exit),
        ("gate_fail_beats_ok", gate_fail_beats_ok),
        ("env_gaps", env_gaps_detects_blanks),
        ("dsn_identity", dsn_identity_rejects),
        ("migration_heads", migration_graph_single_head),
        ("bucket_names", bucket_names_derives),
    ]


def probe(entry: Path) -> tuple[list[str], str]:
    problems: list[str] = []
    ran = 0
    for name, check in _probe_checks(entry):
        try:
            check()
        except AssertionError as error:
            location = entry if entry.is_relative_to(ROOT) else entry.name
            problems.append(
                f"probe {name}: {error}\n"
                f"该判定函数被掏空或判据反转（入口: {location}）；修好后重跑该步骤。"
            )
        except Exception as error:
            problems.append(f"probe {name}: {type(error).__name__}: {error}")
        ran += 1
    return problems, f"probe checks: {ran - len(problems)}/{ran} passed"


# ---------------------------------------------------------------------------
# Output contract (`log`)
# ---------------------------------------------------------------------------

_STEP_LINE = re.compile(r"^\[(\d+)/(\d+)\] (\w+) — ", re.MULTILINE)
_HEADER = re.compile(r"^steps=(\d+) order=([a-z_, ]+) mode=dry-run gate=(\w+)$", re.MULTILINE)
_CONCLUSION = re.compile(r"^\s+结论: (PLAN|PASS|FAIL) — ", re.MULTILINE)
_RECORDS = re.compile(r"^步骤记录: (.+)$", re.MULTILINE)
_DEFERRAL = re.compile(r"^\s+待输入: (.+)$", re.MULTILINE)
_VERDICT = re.compile(r"^reference acceptance: (DRY-RUN PASS|PASS|FAIL)[^\n]*$", re.MULTILINE)
_PENDING_COUNT = re.compile(r"pending=(\d+)")


def check_log(log: Path, step_order: Sequence[str]) -> tuple[list[str], str]:
    text = log.read_text(encoding="utf-8")
    problems: list[str] = []
    header = _HEADER.search(text)
    if header is None:
        problems.append(
            "缺少机器可读首行 `steps=<N> order=<csv> mode=dry-run gate=<mode>`；"
            f"契约见 {CONTRACT_DOC}（机器可读输出）。"
        )
    else:
        declared = [name.strip() for name in header.group(2).split(",")]
        if declared != list(step_order):
            problems.append(
                f"首行 order={declared} 与契约 {list(step_order)} 不一致；"
                f"同步 {CONTRACT_DOC} 的步骤顺序与 ci-entry-guard.py 的 EXPECTED_STEP_ORDER。"
            )
        if int(header.group(1)) != len(step_order):
            problems.append(f"首行 steps={header.group(1)} 与契约步骤数 {len(step_order)} 不一致")
    seen = [match.group(3) for match in _STEP_LINE.finditer(text)]
    if seen != list(step_order):
        problems.append(
            f"步骤行序列 {seen} 与契约 {list(step_order)} 不一致；"
            f"契约见 {CONTRACT_DOC}（`[i/N] <ident> — <title>` 首行）。"
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
    claimed = sorted({outcome for outcome in conclusions if outcome != "PLAN"})
    if claimed:
        problems.append(
            f"dry-run 的步骤结论必须是 PLAN，实际出现 {claimed}；"
            "dry-run 不执行写命令，声明 PASS 即虚报"
        )
    records = _RECORDS.search(text)
    if records is None:
        problems.append(f"缺少 `步骤记录: <ident>=<outcome>` 行；契约见 {CONTRACT_DOC}")
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
        matches = [
            slug
            for slug, words in DECLARED_DEFERRALS.items()
            if all(word.casefold() in deferred.casefold() for word in words)
        ]
        if not matches:
            unregistered.append(deferred)
        elif len(matches) > 1:
            ambiguous.append(f"{deferred} -> {matches}")
    if ambiguous:
        problems.append(
            "一条延后项同时匹配多个登记，无法判断是哪一条；给相关登记补一个区分词（不是整句）：\n- "
            + "\n- ".join(ambiguous)
            + f"\n{_registration_hint('DECLARED_DEFERRALS')}"
        )
    if unregistered:
        registered = "\n".join(
            f"- {slug} 需要同时出现这些词: {list(words)}"
            for slug, words in DECLARED_DEFERRALS.items()
        )
        problems.append(
            "未登记的延后项（新延后输入必须登记，不能静默放宽 PASS 的含义）：\n- "
            + "\n- ".join(unregistered)
            + f"\n已登记:\n{registered}"
            + f"\n{_registration_hint('DECLARED_DEFERRALS')}\n"
            + '登记行: DECLARED_DEFERRALS["<slug>"] = ("<词1>", "<词2>")  # 只放稳定词，不放整句\n'
            + f"契约: {CONTRACT_DOC}"
        )
    verdict = _VERDICT.search(text)
    if verdict is None:
        problems.append(
            f"缺少结论行 `reference acceptance: DRY-RUN PASS steps=N`；契约见 {CONTRACT_DOC}"
        )
    else:
        if verdict.group(1) != "DRY-RUN PASS":
            problems.append(f"dry-run 结论必须是 `DRY-RUN PASS`，实际 `{verdict.group(1)}`")
        counted = _PENDING_COUNT.search(verdict.group(0))
        pending_total = int(counted.group(1)) if counted else 0
        if pending_total != len(deferrals):
            problems.append(
                f"结论行 pending={pending_total} 与日志中 {len(deferrals)} 条 `待输入:` 不一致"
            )
    return problems, f"deferrals: {len(deferrals)}"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _entry_order(entry: Path) -> list[str]:
    module = _load_entry(entry)
    steps = module.STEPS
    return [step.ident for step in steps]


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Guard the reference-acceptance entry point from CI"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    sites_parser = subparsers.add_parser(
        "sites", help="audit mutate judgement and read-only boundary"
    )
    sites_parser.add_argument("--entry", type=Path, default=ENTRY_DEFAULT)
    sites_parser.add_argument("--search-root", type=Path, default=ROOT)

    probe_parser = subparsers.add_parser(
        "probe", help="exercise the entry's pure judgement functions"
    )
    probe_parser.add_argument("--entry", type=Path, default=ENTRY_DEFAULT)

    log_parser = subparsers.add_parser("log", help="verify the documented dry-run output contract")
    log_parser.add_argument("--log", type=Path, required=True)
    log_parser.add_argument("--entry", type=Path, default=ENTRY_DEFAULT)

    arguments = parser.parse_args(argv)
    try:
        if arguments.command == "sites":
            problems, summary = audit_sites(arguments.entry, arguments.search_root)
        elif arguments.command == "probe":
            problems, summary = probe(arguments.entry)
        else:
            order = _entry_order(arguments.entry)
            if order != list(EXPECTED_STEP_ORDER):
                problems = [
                    f"入口 STEPS 顺序 {order} 与契约 {list(EXPECTED_STEP_ORDER)} 不一致；"
                    f"同步 {CONTRACT_DOC} 与 ci-entry-guard.py 的 EXPECTED_STEP_ORDER。"
                ]
                summary = "step order: mismatch"
            else:
                problems, summary = check_log(arguments.log, EXPECTED_STEP_ORDER)
    except AuditError as error:
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
