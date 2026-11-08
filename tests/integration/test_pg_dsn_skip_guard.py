"""Guard against PostgreSQL integration cases degrading a missing DSN into a skip.

``tests/integration/AGENTS.md`` requires PostgreSQL cases to fail, not skip, when
``SPEECHNEXUS_TEST_DATABASE_URL`` is absent. This module scans every integration
test module, plus the shared support fixtures those modules use, that actually
reads that variable and reports a ``pytest.skip`` reachable while the DSN is
missing.

The scan is deliberately scoped to the PostgreSQL DSN. Browser probes, MinIO
inputs and Unix-socket-only Workbench probes use different variables and remain
optional prerequisites; they are neither scanned nor whitelisted here.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

PG_VARIABLE = "SPEECHNEXUS_TEST_DATABASE_URL"
REPO_ROOT = Path(__file__).resolve().parents[2]
SCAN_ROOTS = (
    Path(__file__).resolve().parent,
    REPO_ROOT / "tests" / "support",
)

# Every entry must state why the skip cannot hide a missing PostgreSQL DSN.
# Keys are (repository-relative path, function name). Empty by design: the
# optional prerequisites use other variables and are outside the scan rather
# than exempted from it.
WHITELIST: dict[tuple[str, str], str] = {}

Scope = dict[str, ast.expr]


@dataclass(frozen=True)
class SkipFinding:
    path: str
    line: int
    function: str
    guard_variables: tuple[str, ...]


def _is_os_environ(node: ast.expr) -> bool:
    return (
        isinstance(node, ast.Attribute)
        and node.attr == "environ"
        and isinstance(node.value, ast.Name)
        and node.value.id == "os"
    )


def _env_read_argument(call: ast.Call) -> ast.expr | None:
    """Return the variable-name argument of ``os.environ.get``/``os.getenv``."""
    function = call.func
    if (
        isinstance(function, ast.Attribute)
        and function.attr == "get"
        and _is_os_environ(function.value)
    ):
        return call.args[0] if call.args else None
    if isinstance(function, ast.Name) and function.id == "getenv":
        return call.args[0] if call.args else None
    return None


def _collect_assignments(nodes: list[ast.stmt]) -> Scope:
    """Collect simple name assignments in one scope, ignoring nested functions."""
    assignments: Scope = {}

    def walk(node: ast.AST) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)):
            return
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    assignments[target.id] = node.value
        elif (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.value is not None
        ):
            assignments[node.target.id] = node.value
        for child in ast.iter_child_nodes(node):
            walk(child)

    for node in nodes:
        walk(node)
    return assignments


def _string_values(expr: ast.expr, scope: Scope, depth: int = 0) -> set[str]:
    """Resolve possible string values of an expression without executing it."""
    if depth > 8:
        return set()
    if isinstance(expr, ast.Constant):
        return {expr.value} if isinstance(expr.value, str) else set()
    if isinstance(expr, ast.Name):
        assigned = scope.get(expr.id)
        return _string_values(assigned, scope, depth + 1) if assigned is not None else set()
    if isinstance(expr, (ast.Tuple, ast.List, ast.Set)):
        values: set[str] = set()
        for item in expr.elts:
            values |= _string_values(item, scope, depth + 1)
        return values
    if isinstance(expr, ast.Starred):
        return _string_values(expr.value, scope, depth + 1)
    if isinstance(expr, ast.Dict):
        values = set()
        for value in expr.values:
            if value is not None:
                values |= _string_values(value, scope, depth + 1)
        return values
    if isinstance(expr, ast.Subscript):
        return _string_values(expr.value, scope, depth + 1)
    if isinstance(expr, (ast.ListComp, ast.SetComp, ast.GeneratorExp, ast.DictComp)):
        values = set()
        for generator in expr.generators:
            values |= _string_values(generator.iter, scope, depth + 1)
        return values
    if isinstance(expr, ast.IfExp):
        return _string_values(expr.body, scope, depth + 1) | _string_values(
            expr.orelse, scope, depth + 1
        )
    if isinstance(expr, ast.BoolOp):
        values = set()
        for value in expr.values:
            values |= _string_values(value, scope, depth + 1)
        return values
    return set()


def _env_names(expr: ast.expr, scope: Scope, depth: int = 0) -> set[str]:
    """Resolve the environment-variable names an expression refers to."""
    if depth > 8:
        return set()
    if isinstance(expr, ast.Call):
        argument = _env_read_argument(expr)
        if argument is not None:
            return _string_values(argument, scope, depth + 1)
        names: set[str] = set()
        for argument in expr.args:
            names |= _env_names(argument, scope, depth + 1)
        return names
    if isinstance(expr, ast.Subscript):
        if _is_os_environ(expr.value):
            return _string_values(expr.slice, scope, depth + 1)
        return _string_values(expr.value, scope, depth + 1)
    if isinstance(expr, ast.Name):
        assigned = scope.get(expr.id)
        return _env_names(assigned, scope, depth + 1) if assigned is not None else set()
    if isinstance(expr, ast.Constant):
        return {expr.value} if isinstance(expr.value, str) else set()
    if isinstance(expr, (ast.Tuple, ast.List, ast.Set)):
        names = set()
        for item in expr.elts:
            names |= _env_names(item, scope, depth + 1)
        return names
    if isinstance(expr, ast.Starred):
        return _env_names(expr.value, scope, depth + 1)
    if isinstance(expr, ast.Dict):
        names = set()
        for value in expr.values:
            if value is not None:
                names |= _env_names(value, scope, depth + 1)
        return names
    if isinstance(expr, (ast.ListComp, ast.SetComp, ast.GeneratorExp, ast.DictComp)):
        names = set()
        for generator in expr.generators:
            names |= _string_values(generator.iter, scope, depth + 1)
        return names
    if isinstance(expr, ast.IfExp):
        return _env_names(expr.body, scope, depth + 1) | _env_names(expr.orelse, scope, depth + 1)
    if isinstance(expr, ast.BoolOp):
        names = set()
        for value in expr.values:
            names |= _env_names(value, scope, depth + 1)
        return names
    return set()


def _presence_vars(expr: ast.expr, scope: Scope, depth: int = 0) -> set[str]:
    """Return environment variables whose presence this expression inspects."""
    if depth > 8:
        return set()
    if isinstance(expr, ast.Call):
        argument = _env_read_argument(expr)
        if argument is not None:
            return _string_values(argument, scope, depth + 1)
        if isinstance(expr.func, ast.Name) and expr.func.id in {"all", "any"}:
            names: set[str] = set()
            for argument in expr.args:
                names |= _presence_vars(argument, scope, depth + 1)
            return names
    if isinstance(expr, ast.Subscript) and _is_os_environ(expr.value):
        return _string_values(expr.slice, scope, depth + 1)
    if isinstance(expr, ast.Name):
        assigned = scope.get(expr.id)
        return _presence_vars(assigned, scope, depth + 1) if assigned is not None else set()
    if isinstance(expr, (ast.Tuple, ast.List, ast.Set)):
        names = set()
        for item in expr.elts:
            names |= _presence_vars(item, scope, depth + 1)
        return names
    if isinstance(expr, ast.Starred):
        return _presence_vars(expr.value, scope, depth + 1)
    if isinstance(expr, (ast.ListComp, ast.SetComp, ast.GeneratorExp, ast.DictComp)):
        names = set()
        for generator in expr.generators:
            names |= _string_values(generator.iter, scope, depth + 1)
        return names
    if isinstance(expr, ast.IfExp):
        return _presence_vars(expr.body, scope, depth + 1) | _presence_vars(
            expr.orelse, scope, depth + 1
        )
    if isinstance(expr, ast.BoolOp):
        names = set()
        for value in expr.values:
            names |= _presence_vars(value, scope, depth + 1)
        return names
    return set()


def _absence_vars(expr: ast.expr, scope: Scope, depth: int = 0) -> set[str]:
    """Return variables that are missing when this guard expression is true."""
    if depth > 8:
        return set()
    if isinstance(expr, ast.UnaryOp) and isinstance(expr.op, ast.Not):
        return _presence_vars(expr.operand, scope, depth + 1)
    if isinstance(expr, ast.Compare):
        for operator, comparator in zip(expr.ops, expr.comparators, strict=True):
            if isinstance(operator, ast.NotIn) and _is_os_environ(comparator):
                return _string_values(expr.left, scope, depth + 1)
            if isinstance(operator, (ast.Is, ast.IsNot, ast.Eq, ast.NotEq)):
                names = _presence_vars(expr.left, scope, depth + 1)
                names |= _presence_vars(comparator, scope, depth + 1)
                if names:
                    return names
        return set()
    if isinstance(expr, ast.Name):
        assigned = scope.get(expr.id)
        if assigned is None:
            return set()
        names = _absence_vars(assigned, scope, depth + 1)
        if names:
            return names
        return _env_names(assigned, scope, depth + 1)
    if isinstance(expr, ast.BoolOp):
        names = set()
        for value in expr.values:
            names |= _absence_vars(value, scope, depth + 1)
        return names
    return set()


def _is_pytest_skip(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "skip"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "pytest"
    )


def _top_level_functions(body: list[ast.stmt]) -> list[ast.FunctionDef | ast.AsyncFunctionDef]:
    functions: list[ast.FunctionDef | ast.AsyncFunctionDef] = []
    for node in body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            functions.append(node)
        elif isinstance(node, ast.ClassDef):
            functions.extend(
                child
                for child in node.body
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
            )
    return functions


def _scan_scope(
    nodes: list[ast.stmt],
    scope: Scope,
    *,
    path: str,
    function: str,
) -> tuple[bool, list[SkipFinding]]:
    """Return whether this scope reads the PG DSN, plus its raw skip findings."""
    reads_pg = False
    findings: list[SkipFinding] = []

    def walk(node: ast.AST, guards: list[ast.If], active: Scope) -> None:
        nonlocal reads_pg
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)):
            return
        if isinstance(node, ast.Call):
            argument = _env_read_argument(node)
            if argument is not None and PG_VARIABLE in _string_values(argument, active):
                reads_pg = True
            if _is_pytest_skip(node):
                guard_names: set[str] = set()
                for guard in guards:
                    guard_names |= _absence_vars(guard.test, active)
                findings.append(
                    SkipFinding(
                        path=path,
                        line=node.lineno,
                        function=function,
                        guard_variables=tuple(sorted(guard_names)),
                    )
                )
        if (
            isinstance(node, ast.Subscript)
            and _is_os_environ(node.value)
            and PG_VARIABLE in _string_values(node.slice, active)
        ):
            reads_pg = True
        if isinstance(node, ast.If):
            for child in node.body:
                walk(child, [*guards, node], active)
            for child in node.orelse:
                walk(child, guards, active)
            return
        if isinstance(node, (ast.ListComp, ast.SetComp, ast.GeneratorExp, ast.DictComp)):
            comprehension_scope = dict(active)
            for generator in node.generators:
                if isinstance(generator.target, ast.Name):
                    comprehension_scope[generator.target.id] = generator.iter
                for condition in generator.ifs:
                    walk(condition, guards, comprehension_scope)
            if isinstance(node, ast.DictComp):
                walk(node.key, guards, comprehension_scope)
                walk(node.value, guards, comprehension_scope)
            else:
                walk(node.elt, guards, comprehension_scope)
            return
        for descendant in ast.iter_child_nodes(node):
            walk(descendant, guards, active)

    for node in nodes:
        walk(node, [], scope)
    return reads_pg, findings


def scan_source(path: str, source: str) -> list[SkipFinding]:
    """Report PG-DSN skips in one source text; empty for non-PG modules."""
    tree = ast.parse(source)
    module_scope = _collect_assignments(tree.body)
    modules: list[tuple[list[ast.stmt], Scope, str]] = [(tree.body, module_scope, "<module>")]
    for function in _top_level_functions(tree.body):
        scope = {**module_scope, **_collect_assignments(function.body)}
        modules.append((function.body, scope, function.name))

    reads_pg = False
    findings: list[SkipFinding] = []
    for nodes, scope, name in modules:
        scope_reads, scope_findings = _scan_scope(nodes, scope, path=path, function=name)
        reads_pg = reads_pg or scope_reads
        findings.extend(scope_findings)

    if not reads_pg:
        return []

    violations: list[SkipFinding] = []
    for finding in findings:
        reason = WHITELIST.get((path, finding.function))
        if reason:
            continue
        if not finding.guard_variables or PG_VARIABLE in finding.guard_variables:
            violations.append(finding)
    return violations


def scan_file(path: Path) -> list[SkipFinding]:
    relative = path.resolve().relative_to(REPO_ROOT).as_posix()
    return scan_source(relative, path.read_text(encoding="utf-8"))


def test_no_pg_dsn_skip_on_missing_path() -> None:
    findings: list[SkipFinding] = []
    for root in SCAN_ROOTS:
        for path in sorted(root.rglob("*.py")):
            findings.extend(scan_file(path))

    details = "\n".join(
        f"- {finding.path}:{finding.line} ({finding.function}) skips when "
        + (
            ", ".join(finding.guard_variables)
            if finding.guard_variables
            else "an unrecognized guard is true"
        )
        for finding in findings
    )
    assert not findings, (
        "PostgreSQL integration cases must fail instead of skipping when "
        f"{PG_VARIABLE} is missing:\n{details}"
    )


def test_guard_flags_missing_pg_dsn_skip() -> None:
    source = """
import os

def test_case() -> None:
    url = os.environ.get("SPEECHNEXUS_TEST_DATABASE_URL")
    if not url:
        pytest.skip("not evidence")
"""
    findings = scan_source("synthetic.py", source)
    assert [(finding.line, finding.guard_variables) for finding in findings] == [
        (7, (PG_VARIABLE,))
    ]


def test_guard_allows_minio_only_skip() -> None:
    source = """
import os

DATABASE_VARIABLE = "SPEECHNEXUS_TEST_DATABASE_URL"

def required() -> None:
    database = os.environ.get(DATABASE_VARIABLE)
    if not database:
        pytest.fail("missing PostgreSQL")
    objects = [os.environ.get(name) for name in ("SPEECHNEXUS_TEST_MINIO_ENDPOINT",)]
    if not all(objects):
        pytest.skip("missing MinIO")
"""
    assert scan_source("synthetic.py", source) == []


def test_guard_ignores_files_without_pg_dsn() -> None:
    source = """
import os

def test_browser() -> None:
    dsn = os.environ.get("SPEECHNEXUS_WORKBENCH_TEST_DSN")
    if not dsn:
        pytest.skip("disposable probe not configured")
"""
    assert scan_source("synthetic.py", source) == []
