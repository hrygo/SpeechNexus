"""Guard against PostgreSQL integration cases degrading a missing DSN into a skip.

``tests/integration/AGENTS.md`` requires PostgreSQL cases to fail, not skip, when
``SPEECHNEXUS_TEST_DATABASE_URL`` is absent. This module scans the integration
tests and the shared ``tests/support`` fixtures they call, then reports skip-like
outcomes (``pytest.skip``, ``pytest.xfail``, ``skipif`` markers) that are
reachable while a database DSN is missing.

The scan deliberately constrains only database DSNs. MinIO inputs, browser
probes, Unix-socket-only Workbench probes and host tools such as ``psql`` use
other prerequisites and remain optional; they are not whitelisted, they are
classified as non-database guards.

Call handling is intentionally interprocedural: a skip inside a nested helper,
or inside a shared support helper that receives a DSN, is attributed to the
integration module that reaches it. Only that reachability is modelled; the
guard does not execute the tests.
"""

from __future__ import annotations

import ast
import textwrap
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

import pytest

PG_VARIABLE = "SPEECHNEXUS_TEST_DATABASE_URL"
MYSQL_VARIABLE = "SPEECHNEXUS_TEST_MYSQL_URL"
DB_VARIABLES = frozenset({PG_VARIABLE, MYSQL_VARIABLE})
REPO_ROOT = Path(__file__).resolve().parents[2]
SCAN_PATHS = (
    REPO_ROOT / "tests" / "integration",
    REPO_ROOT / "tests" / "support",
)

# Every entry must state why the skip cannot hide a missing database DSN.
# Keys are (repository-relative path, function name). Empty by design: optional
# prerequisites are recognized structurally instead of being exempted.
WHITELIST: dict[tuple[str, str], str] = {}

NON_DSN_CALLS = frozenset(
    {
        "shutil.which",
        "shutil.disk_usage",
        "platform.system",
        "platform.machine",
        "platform.python_version",
        "importlib.util.find_spec",
        "os.path.exists",
        "os.access",
    }
)
NON_DSN_ATTRIBUTES = frozenset({"sys.platform", "os.name", "sys.version_info"})
DIALECTS = frozenset({"postgresql", "mysql"})


@dataclass(frozen=True)
class EnvNames:
    """A synthetic scope value: a name derived from these environment variables."""

    names: frozenset[str]


ScopeValue = ast.expr | EnvNames
Scope = dict[str, ScopeValue]
FunctionNode = ast.FunctionDef | ast.AsyncFunctionDef


@dataclass(frozen=True)
class SkipFinding:
    path: str
    line: int
    function: str
    reason: str
    guard_variables: tuple[str, ...]


@dataclass
class CallSite:
    node: ast.Call
    function: FunctionInfo


@dataclass
class FunctionInfo:
    module: ModuleInfo
    qualname: str
    name: str
    node: FunctionNode
    parent: FunctionInfo | None
    params: list[str]
    lexical_scope: Scope
    own_assignments: Scope
    local_functions: dict[str, FunctionInfo] = field(default_factory=dict)
    calls: list[CallSite] = field(default_factory=list)
    tainted: dict[str, set[str]] = field(default_factory=dict)
    context: bool = False


@dataclass
class ModuleInfo:
    path: str
    tree: ast.Module
    scope: Scope
    imports: dict[str, tuple[str, str]] = field(default_factory=dict)
    module_aliases: dict[str, str] = field(default_factory=dict)
    pytest_aliases: set[str] = field(default_factory=lambda: {"pytest"})
    skip_names: set[str] = field(default_factory=set)
    xfail_names: set[str] = field(default_factory=set)
    mark_aliases: set[str] = field(default_factory=set)
    skipif_names: set[str] = field(default_factory=set)
    functions: list[FunctionInfo] = field(default_factory=list)
    top_functions: dict[str, FunctionInfo] = field(default_factory=dict)
    reads_database: bool = False


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


def _collect_assignments(nodes: Iterable[ast.stmt]) -> Scope:
    """Collect simple name assignments in one lexical scope."""
    assignments: Scope = {}

    def walk(node: ast.AST) -> None:
        if isinstance(node, (FunctionNode, ast.Lambda, ast.ClassDef)):
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


def _string_values(expr: ScopeValue, scope: Scope, depth: int = 0) -> set[str]:
    """Resolve possible string values of an expression without executing it."""
    if depth > 12:
        return set()
    if isinstance(expr, EnvNames):
        return set(expr.names)
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


def _env_names(expr: ScopeValue, scope: Scope, depth: int = 0) -> set[str]:
    """Resolve environment-variable names an expression refers to."""
    if depth > 12:
        return set()
    if isinstance(expr, EnvNames):
        return set(expr.names)
    if isinstance(expr, ast.Call):
        argument = _env_read_argument(expr)
        if argument is not None:
            return _string_values(argument, scope, depth + 1)
        if isinstance(expr.func, ast.Attribute):
            attribute_names = _env_names(expr.func.value, scope, depth + 1)
            if attribute_names:
                return attribute_names
        names: set[str] = set()
        for argument in expr.args:
            names |= _env_names(argument, scope, depth + 1)
        return names
    if isinstance(expr, ast.Subscript):
        if _is_os_environ(expr.value):
            return _string_values(expr.slice, scope, depth + 1)
        if isinstance(expr.value, ast.Dict) and isinstance(expr.slice, ast.Constant):
            for key, value in zip(expr.value.keys, expr.value.values, strict=False):
                if (
                    isinstance(key, ast.Constant)
                    and key.value == expr.slice.value
                    and value is not None
                ):
                    return _env_names(value, scope, depth + 1)
        return _env_names(expr.value, scope, depth + 1)
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
    if isinstance(expr, ast.JoinedStr):
        names = set()
        for value in expr.values:
            if isinstance(value, ast.FormattedValue):
                names |= _env_names(value.value, scope, depth + 1)
        return names
    if isinstance(expr, ast.BoolOp):
        names = set()
        for value in expr.values:
            names |= _env_names(value, scope, depth + 1)
        return names
    return set()


def _presence_vars(expr: ScopeValue, scope: Scope, depth: int = 0) -> set[str]:
    """Return environment variables whose presence this expression inspects."""
    if depth > 12:
        return set()
    if isinstance(expr, EnvNames):
        return set(expr.names)
    if isinstance(expr, ast.Call):
        argument = _env_read_argument(expr)
        if argument is not None:
            return _string_values(argument, scope, depth + 1)
        if isinstance(expr.func, ast.Name) and expr.func.id in {"all", "any"}:
            names: set[str] = set()
            for argument in expr.args:
                names |= _presence_vars(argument, scope, depth + 1)
            return names
        if isinstance(expr.func, ast.Attribute):
            names = _presence_vars(expr.func.value, scope, depth + 1)
            if names:
                return names
    if isinstance(expr, ast.Subscript):
        if _is_os_environ(expr.value):
            return _string_values(expr.slice, scope, depth + 1)
        if isinstance(expr.value, ast.Dict) and isinstance(expr.slice, ast.Constant):
            for key, value in zip(expr.value.keys, expr.value.values, strict=False):
                if (
                    isinstance(key, ast.Constant)
                    and key.value == expr.slice.value
                    and value is not None
                ):
                    return _presence_vars(value, scope, depth + 1)
        return _presence_vars(expr.value, scope, depth + 1)
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
    if isinstance(expr, ast.Dict):
        names = set()
        for value in expr.values:
            if value is not None:
                names |= _presence_vars(value, scope, depth + 1)
        return names
    if isinstance(expr, ast.JoinedStr):
        names = set()
        for value in expr.values:
            if isinstance(value, ast.FormattedValue):
                names |= _presence_vars(value.value, scope, depth + 1)
        return names
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
    """Return variables that are missing when this guard expression is true.

    A bare ``if url`` tests presence and must not be mistaken for absence. Only a
    negation, an ``is None``/``== None`` comparison, or a missing-variable list
    created by a comprehension describes an absent prerequisite.
    """
    if depth > 12:
        return set()
    if isinstance(expr, ast.UnaryOp) and isinstance(expr.op, ast.Not):
        return _presence_vars(expr.operand, scope, depth + 1)
    if isinstance(expr, ast.Compare):
        for operator, comparator in zip(expr.ops, expr.comparators, strict=False):
            if isinstance(operator, ast.NotIn) and _is_os_environ(comparator):
                return _string_values(expr.left, scope, depth + 1)
            if (
                isinstance(operator, (ast.Is, ast.Eq))
                and isinstance(comparator, ast.Constant)
                and (comparator.value is None or comparator.value is False)
            ):
                return _presence_vars(expr.left, scope, depth + 1)
        return set()
    if isinstance(expr, ast.Name):
        assigned = scope.get(expr.id)
        if not isinstance(assigned, ast.expr):
            return set()
        if isinstance(assigned, (ast.ListComp, ast.SetComp, ast.GeneratorExp, ast.DictComp)):
            return _presence_vars(assigned, scope, depth + 1)
        if isinstance(assigned, (ast.BoolOp, ast.Compare, ast.UnaryOp)):
            return _absence_vars(assigned, scope, depth + 1)
        return set()
    if isinstance(expr, ast.BoolOp):
        names = set()
        for value in expr.values:
            names |= _absence_vars(value, scope, depth + 1)
        return names
    return set()


def _comprehension_values(name: ast.Name, root: ast.AST, scope: Scope) -> set[str]:
    """Resolve a comprehension target to the strings of its iterable."""
    for node in ast.walk(root):
        if not isinstance(node, (ast.ListComp, ast.SetComp, ast.GeneratorExp, ast.DictComp)):
            continue
        for generator in node.generators:
            if isinstance(generator.target, ast.Name) and generator.target.id == name.id:
                return _string_values(generator.iter, scope)
    return set()


def _dotted_name(node: ast.expr) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = _dotted_name(node.value)
        return f"{prefix}.{node.attr}" if prefix is not None else None
    return None


def _is_non_dsn_prerequisite(expr: ast.expr, scope: Scope, depth: int = 0) -> bool:
    """Recognize guards for host tools and engine dialects, not database DSNs."""
    if depth > 12:
        return False
    if isinstance(expr, ast.UnaryOp) and isinstance(expr.op, ast.Not):
        return _is_non_dsn_prerequisite(expr.operand, scope, depth + 1)
    if isinstance(expr, ast.BoolOp):
        return all(_is_non_dsn_prerequisite(value, scope, depth + 1) for value in expr.values)
    if isinstance(expr, ast.Compare):
        for comparator in expr.comparators:
            if isinstance(comparator, ast.Constant) and comparator.value in DIALECTS:
                return True
        for side in (expr.left, *expr.comparators):
            if isinstance(side, ast.Call) and _dotted_name(side.func) in NON_DSN_CALLS:
                return True
            dotted = _dotted_name(side)
            if dotted is not None and (
                dotted in NON_DSN_ATTRIBUTES
                or any(dotted.startswith(f"{attribute}.") for attribute in NON_DSN_ATTRIBUTES)
            ):
                return True
        return all(isinstance(item, ast.Constant) for item in (expr.left, *expr.comparators))
    if isinstance(expr, ast.Call):
        dotted = _dotted_name(expr.func)
        return dotted is not None and (
            dotted in NON_DSN_CALLS
            or any(dotted.startswith(f"{attribute}.") for attribute in NON_DSN_ATTRIBUTES)
        )
    if isinstance(expr, ast.Attribute):
        dotted = _dotted_name(expr)
        return dotted is not None and (
            dotted in NON_DSN_ATTRIBUTES
            or any(dotted.startswith(f"{attribute}.") for attribute in NON_DSN_ATTRIBUTES)
        )
    if isinstance(expr, ast.Constant):
        return isinstance(expr.value, bool)
    if isinstance(expr, ast.Name):
        assigned = scope.get(expr.id)
        if isinstance(assigned, ast.expr):
            return _is_non_dsn_prerequisite(assigned, scope, depth + 1)
    return False


def _is_pytest_like(node: ast.expr, aliases: set[str]) -> bool:
    return isinstance(node, ast.Name) and node.id in aliases


def _pytest_attribute(node: ast.expr, aliases: set[str]) -> str | None:
    if isinstance(node, ast.Attribute) and _is_pytest_like(node.value, aliases):
        return node.attr
    return None


def _marker_kind(function: ast.expr, module: ModuleInfo) -> str | None:
    if isinstance(function, ast.Name) and function.id in module.skipif_names:
        return "skipif"
    if isinstance(function, ast.Attribute) and function.attr in {"skipif", "skip"}:
        if _is_pytest_like(function.value, module.pytest_aliases):
            return function.attr
        if _is_pytest_like(function.value, module.mark_aliases):
            return function.attr
        mark = _pytest_attribute(function.value, module.pytest_aliases)
        if mark == "mark":
            return function.attr
    return None


def _top_level_functions(body: Iterable[ast.stmt]) -> Iterable[FunctionNode]:
    for node in body:
        if isinstance(node, FunctionNode):
            yield node
        elif isinstance(node, ast.ClassDef):
            for child in node.body:
                if isinstance(child, FunctionNode):
                    yield child


def _local_function_defs(nodes: Iterable[ast.stmt]) -> Iterable[FunctionNode]:
    def walk(node: ast.AST) -> Iterable[FunctionNode]:
        if isinstance(node, FunctionNode):
            yield node
            return
        if isinstance(node, (ast.ClassDef, ast.Lambda)):
            return
        for child in ast.iter_child_nodes(node):
            yield from walk(child)

    for node in nodes:
        yield from walk(node)


def _walk_local(node: ast.AST) -> Iterable[ast.AST]:
    """Yield nodes in one function scope, excluding nested functions and classes."""
    if isinstance(node, (FunctionNode, ast.ClassDef, ast.Lambda)):
        return
    yield node
    for child in ast.iter_child_nodes(node):
        yield from _walk_local(child)


def _walk_markers(node: ast.AST) -> Iterable[ast.AST]:
    """Walk module and class bodies for marker calls, stopping at functions."""
    if isinstance(node, FunctionNode):
        return
    yield node
    for child in ast.iter_child_nodes(node):
        yield from _walk_markers(child)


def _walk_guarded(
    node: ast.AST,
    guards: list[ast.If],
    visit: Callable[[ast.AST, list[ast.If]], None],
) -> None:
    if isinstance(node, (FunctionNode, ast.ClassDef, ast.Lambda)):
        return
    if isinstance(node, ast.If):
        for child in node.body:
            _walk_guarded(child, [*guards, node], visit)
        for child in node.orelse:
            _walk_guarded(child, guards, visit)
        return
    visit(node, guards)
    for descendant in ast.iter_child_nodes(node):
        _walk_guarded(descendant, guards, visit)


class Analyzer:
    """Parse scanned modules and attribute skip-like outcomes to database guards."""

    def __init__(self, files: Mapping[str, str]) -> None:
        self.files = dict(files)
        self.modules: dict[str, ModuleInfo] = {}
        self.symbols: dict[tuple[str, str], FunctionInfo] = {}
        for path, source in sorted(self.files.items()):
            module = self._parse_module(path, source)
            self.modules[path] = module
        for module in self.modules.values():
            for function in module.functions:
                self.symbols[(module.path, function.qualname)] = function

    def _parse_module(self, path: str, source: str) -> ModuleInfo:
        tree = ast.parse(source, filename=path)
        module = ModuleInfo(path=path, tree=tree, scope=_collect_assignments(tree.body))
        self._parse_imports(module)
        for node in tree.body:
            if isinstance(node, FunctionNode):
                function = self._collect_function(module, node, module.scope, None)
                module.functions.append(function)
                module.top_functions[function.name] = function
            elif isinstance(node, ast.ClassDef):
                for child in node.body:
                    if isinstance(child, FunctionNode):
                        function = self._collect_function(module, child, module.scope, None)
                        module.functions.append(function)
                        module.top_functions.setdefault(f"{node.name}.{function.name}", function)
        return module

    def _parse_imports(self, module: ModuleInfo) -> None:
        for node in module.tree.body:
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == "pytest":
                        module.pytest_aliases.add(alias.asname or "pytest")
                    elif alias.asname is not None:
                        module.module_aliases[alias.asname] = alias.name
            elif isinstance(node, ast.ImportFrom) and node.module is not None:
                if node.module == "pytest":
                    for alias in node.names:
                        local = alias.asname or alias.name
                        if alias.name == "skip":
                            module.skip_names.add(local)
                        elif alias.name == "xfail":
                            module.xfail_names.add(local)
                        elif alias.name == "mark":
                            module.mark_aliases.add(local)
                        elif alias.name == "skipif":
                            module.skipif_names.add(local)
                elif node.module.startswith("tests."):
                    for alias in node.names:
                        module.imports[alias.asname or alias.name] = (
                            node.module,
                            alias.name,
                        )

    def _collect_function(
        self,
        module: ModuleInfo,
        node: FunctionNode,
        lexical_scope: Scope,
        parent: FunctionInfo | None,
    ) -> FunctionInfo:
        own = _collect_assignments(node.body)
        params = [
            argument.arg
            for argument in (
                *node.args.posonlyargs,
                *node.args.args,
                *node.args.kwonlyargs,
            )
        ]
        qualname = f"{parent.qualname}.{node.name}" if parent is not None else node.name
        function = FunctionInfo(
            module=module,
            qualname=qualname,
            name=node.name,
            node=node,
            parent=parent,
            params=params,
            lexical_scope=lexical_scope,
            own_assignments=own,
        )
        nested_scope = {**lexical_scope, **own}
        for child_node in _local_function_defs(node.body):
            child = self._collect_function(module, child_node, nested_scope, function)
            function.local_functions[child.name] = child
            module.functions.append(child)
        for statement in node.body:
            for candidate in _walk_local(statement):
                if isinstance(candidate, ast.Call):
                    function.calls.append(CallSite(node=candidate, function=function))
        return function

    def _effective_scope(self, function: FunctionInfo) -> Scope:
        if function.parent is not None:
            scope = dict(self._effective_scope(function.parent))
        else:
            scope = dict(function.lexical_scope)
        scope.update(function.own_assignments)
        for parameter, variables in function.tainted.items():
            if variables:
                scope[parameter] = EnvNames(frozenset(variables))
        return scope

    def _module_path(self, dotted: str) -> str | None:
        candidate = dotted.replace(".", "/")
        for suffix in (".py", "/__init__.py"):
            path = candidate + suffix
            if path in self.files:
                return path
        return None

    def _resolve_import(self, module: ModuleInfo, name: str) -> FunctionInfo | None:
        target = module.imports.get(name)
        if target is None:
            return None
        dotted, attribute = target
        path = self._module_path(dotted)
        if path is None:
            return None
        return self.symbols.get((path, attribute))

    def _resolve_local(self, function: FunctionInfo | None, name: str) -> FunctionInfo | None:
        current = function
        while current is not None:
            if name in current.local_functions:
                return current.local_functions[name]
            current = current.parent
        return None

    def resolve_call(
        self, module: ModuleInfo, function: FunctionInfo | None, call: ast.Call
    ) -> FunctionInfo | None:
        target = call.func
        if isinstance(target, ast.Name):
            local = self._resolve_local(function, target.id)
            if local is not None:
                return local
            if target.id in module.top_functions:
                return module.top_functions[target.id]
            return self._resolve_import(module, target.id)
        dotted = _dotted_name(target)
        if dotted is None:
            return None
        parts = dotted.split(".")
        for index in range(len(parts) - 1, 0, -1):
            alias = ".".join(parts[:index])
            attribute = ".".join(parts[index:])
            imported = module.module_aliases.get(alias)
            if imported is not None:
                path = self._module_path(imported)
                if path is not None:
                    return self.symbols.get((path, attribute))
        if len(parts) == 2:
            return module.top_functions.get(dotted)
        return None

    def _arg_bindings(self, call: ast.Call, callee: FunctionInfo) -> Iterable[tuple[str, ast.expr]]:
        params = callee.params
        if params and params[0] in {"self", "cls"}:
            params = params[1:]
        for parameter, argument in zip(params, call.args, strict=False):
            if isinstance(argument, ast.Starred):
                continue
            yield parameter, argument
        for keyword in call.keywords:
            if keyword.arg is not None and keyword.arg in params:
                yield keyword.arg, keyword.value

    def _module_scope_reads_database(self, module: ModuleInfo) -> bool:
        for node in ast.walk(module.tree):
            if isinstance(node, ast.Call):
                argument = _env_read_argument(node)
                if argument is None:
                    continue
                values = _string_values(argument, module.scope)
                if not values and isinstance(argument, ast.Name):
                    values = _comprehension_values(argument, module.tree, module.scope)
                if DB_VARIABLES & values:
                    return True
            elif isinstance(node, ast.Subscript) and _is_os_environ(node.value):
                if DB_VARIABLES & _string_values(node.slice, module.scope):
                    return True
        return False

    def _reads_database(self, function: FunctionInfo) -> bool:
        scope = self._effective_scope(function)
        for statement in function.node.body:
            for node in _walk_local(statement):
                if isinstance(node, ast.Call):
                    argument = _env_read_argument(node)
                    if argument is None:
                        continue
                    values = _string_values(argument, scope)
                    if not values and isinstance(argument, ast.Name):
                        values = _comprehension_values(argument, function.node, scope)
                    if DB_VARIABLES & values:
                        return True
                elif isinstance(node, ast.Subscript) and _is_os_environ(node.value):
                    if DB_VARIABLES & _string_values(node.slice, scope):
                        return True
        return False

    def propagate(self) -> None:
        for _ in range(12):
            changed = False
            for module in self.modules.values():
                for function in module.functions:
                    scope = self._effective_scope(function)
                    for call in function.calls:
                        callee = self.resolve_call(module, function, call.node)
                        if callee is None:
                            continue
                        for parameter, argument in self._arg_bindings(call.node, callee):
                            variables = _env_names(argument, scope) & DB_VARIABLES
                            if not variables:
                                continue
                            current = callee.tainted.setdefault(parameter, set())
                            if not variables <= current:
                                current |= variables
                                changed = True
            if not changed:
                break

        for module in self.modules.values():
            module.reads_database = any(
                self._reads_database(function) for function in module.functions
            ) or self._module_scope_reads_database(module)
            if module.path.startswith("tests/integration/") and module.reads_database:
                for function in module.functions:
                    function.context = True
            for function in module.functions:
                if function.tainted:
                    function.context = True

        for _ in range(12):
            changed = False
            for module in self.modules.values():
                for function in module.functions:
                    if (
                        function.parent is not None
                        and function.parent.context
                        and not function.context
                    ):
                        function.context = True
                        changed = True
                    if not function.context:
                        continue
                    for call in function.calls:
                        callee = self.resolve_call(module, function, call.node)
                        if callee is not None and not callee.context:
                            callee.context = True
                            changed = True
            if not changed:
                break

    def _decorator_scope(self, function: FunctionInfo) -> Scope:
        if function.parent is not None:
            return self._effective_scope(function.parent)
        return function.module.scope

    def _decorator_findings(self, function: FunctionInfo) -> list[SkipFinding]:
        scope = self._decorator_scope(function)
        findings: list[SkipFinding] = []
        if not function.context:
            return findings
        for decorator in function.node.decorator_list:
            if not isinstance(decorator, ast.Call):
                continue
            kind = _marker_kind(decorator.func, function.module)
            if kind is None:
                continue
            finding = _marker_finding(
                path=function.module.path,
                function=function.qualname,
                call=decorator,
                scope=scope,
                kind=kind,
                context=function.context,
            )
            if finding is not None:
                findings.append(finding)
        return findings

    def _module_marker_findings(self, module: ModuleInfo) -> list[SkipFinding]:
        if not (module.path.startswith("tests/integration/") and module.reads_database):
            return []
        findings: list[SkipFinding] = []
        for node in _walk_markers(module.tree):
            if not isinstance(node, ast.Call):
                continue
            kind = _marker_kind(node.func, module)
            if kind is None:
                continue
            finding = _marker_finding(
                path=module.path,
                function="<module>",
                call=node,
                scope=module.scope,
                kind=kind,
                context=True,
            )
            if finding is not None:
                findings.append(finding)
        return findings

    def _call_findings(self, function: FunctionInfo) -> list[SkipFinding]:
        if not function.context:
            return []
        scope = self._effective_scope(function)
        findings: list[SkipFinding] = []

        def visit(node: ast.AST, guards: list[ast.If]) -> None:
            if not isinstance(node, ast.Call):
                return
            kind = _call_kind(node.func, function.module)
            if kind is None:
                return
            finding = _call_finding(
                path=function.module.path,
                function=function.qualname,
                call=node,
                guards=guards,
                scope=scope,
            )
            if finding is not None:
                findings.append(finding)

        for statement in function.node.body:
            _walk_guarded(statement, [], visit)
        return findings

    def findings(self) -> list[SkipFinding]:
        self.propagate()
        findings: list[SkipFinding] = []
        for module in self.modules.values():
            findings.extend(self._module_marker_findings(module))
            for function in module.functions:
                findings.extend(self._decorator_findings(function))
                findings.extend(self._call_findings(function))
        violations = [
            finding for finding in findings if not WHITELIST.get((finding.path, finding.function))
        ]
        return sorted(violations, key=lambda finding: (finding.path, finding.line))


def _call_kind(function: ast.expr, module: ModuleInfo) -> str | None:
    if isinstance(function, ast.Name):
        if function.id in module.skip_names:
            return "skip"
        if function.id in module.xfail_names:
            return "xfail"
        return None
    attribute = _pytest_attribute(function, module.pytest_aliases)
    if attribute in {"skip", "xfail"}:
        return attribute
    return None


def _marker_condition(call: ast.Call) -> ast.expr | None:
    if call.args:
        return call.args[0]
    for keyword in call.keywords:
        if keyword.arg == "condition":
            return keyword.value
    return None


def _condition_ast(condition: ast.expr | None) -> ast.expr | None:
    if condition is None:
        return None
    if isinstance(condition, ast.Constant) and isinstance(condition.value, str):
        try:
            return ast.parse(condition.value, mode="eval").body
        except SyntaxError:
            return None
    return condition


def _guard_result(guards: list[ast.If], scope: Scope) -> tuple[bool, tuple[str, ...], bool]:
    """Return (database_missing, database_variables, puzzle)."""
    absent: set[str] = set()
    non_dsn = False
    unknown = False
    for guard in guards:
        variables = _absence_vars(guard.test, scope)
        if variables:
            absent |= variables
        elif _is_non_dsn_prerequisite(guard.test, scope):
            non_dsn = True
        else:
            unknown = True
    database = tuple(sorted(absent & DB_VARIABLES))
    optional = bool(absent) or (non_dsn and not unknown)
    return bool(database), database, optional


def _call_finding(
    *,
    path: str,
    function: str,
    call: ast.Call,
    guards: list[ast.If],
    scope: Scope,
) -> SkipFinding | None:
    database, variables, optional = _guard_result(guards, scope)
    if database:
        return SkipFinding(
            path=path,
            line=call.lineno,
            function=function,
            reason="skip is reachable while a database DSN is missing",
            guard_variables=variables,
        )
    if optional:
        return None
    return SkipFinding(
        path=path,
        line=call.lineno,
        function=function,
        reason="unconditional or unrecognized skip in a database integration context",
        guard_variables=(),
    )


def _marker_finding(
    *,
    path: str,
    function: str,
    call: ast.Call,
    scope: Scope,
    kind: str,
    context: bool,
) -> SkipFinding | None:
    if kind == "skip":
        if context:
            return SkipFinding(
                path=path,
                line=call.lineno,
                function=function,
                reason="unconditional skip marker in a database integration context",
                guard_variables=(),
            )
        return None
    condition = _condition_ast(_marker_condition(call))
    if condition is None:
        return None
    variables = _absence_vars(condition, scope)
    database = tuple(sorted(variables & DB_VARIABLES))
    if database:
        return SkipFinding(
            path=path,
            line=call.lineno,
            function=function,
            reason="skipif is reachable while a database DSN is missing",
            guard_variables=database,
        )
    if variables:
        return None
    if _is_non_dsn_prerequisite(condition, scope):
        return None
    if context:
        return SkipFinding(
            path=path,
            line=call.lineno,
            function=function,
            reason="skipif condition is not a recognized optional prerequisite",
            guard_variables=(),
        )
    return None


def _scan_paths(*roots: Path) -> dict[str, str]:
    files: dict[str, str] = {}
    for root in roots:
        for path in sorted(root.rglob("*.py")):
            files[path.relative_to(REPO_ROOT).as_posix()] = path.read_text(encoding="utf-8")
    return files


def scan_files(files: Mapping[str, str]) -> list[SkipFinding]:
    return Analyzer(files).findings()


def test_no_database_dsn_skip_on_missing_path() -> None:
    findings = scan_files(_scan_paths(*SCAN_PATHS))
    details = "\n".join(
        f"- {finding.path}:{finding.line} ({finding.function}) {finding.reason}"
        + (f"; guards: {', '.join(finding.guard_variables)}" if finding.guard_variables else "")
        for finding in findings
    )
    assert not findings, (
        "PostgreSQL integration cases must fail instead of skipping when a database "
        f"DSN is missing:\n{details}"
    )


def _dedent(source: str) -> str:
    return textwrap.dedent(source).strip("\n")


def test_guard_flags_skipif_marker() -> None:
    source = _dedent(
        """
        import os

        @pytest.mark.skipif(not os.environ.get("SPEECHNEXUS_TEST_DATABASE_URL"), reason="x")
        def test_case() -> None:
            pass
        """
    )
    findings = scan_files({"tests/integration/test_case.py": source})
    assert [(finding.line, finding.guard_variables) for finding in findings] == [
        (3, (PG_VARIABLE,))
    ]


def test_guard_flags_imported_skip_alias() -> None:
    source = _dedent(
        """
        import os
        from pytest import skip as want_skip

        def test_case() -> None:
            url = os.environ.get("SPEECHNEXUS_TEST_DATABASE_URL")
            if not url:
                want_skip("not evidence")
        """
    )
    findings = scan_files({"tests/integration/test_case.py": source})
    assert [(finding.line, finding.function) for finding in findings] == [(7, "test_case")]


def test_guard_flags_xfail() -> None:
    source = _dedent(
        """
        import os

        def test_case() -> None:
            url = os.environ.get("SPEECHNEXUS_TEST_DATABASE_URL")
            if not url:
                pytest.xfail("not evidence")
        """
    )
    findings = scan_files({"tests/integration/test_case.py": source})
    assert [(finding.line, finding.function) for finding in findings] == [(6, "test_case")]


def test_guard_flags_nested_helper() -> None:
    source = _dedent(
        """
        import os

        def test_case() -> None:
            url = os.environ.get("SPEECHNEXUS_TEST_DATABASE_URL")
            def require() -> None:
                if not url:
                    pytest.skip("not evidence")
            require()
        """
    )
    findings = scan_files({"tests/integration/test_case.py": source})
    assert [(finding.line, finding.function) for finding in findings] == [(7, "test_case.require")]


def test_guard_flags_cross_file_helper() -> None:
    files = {
        "tests/integration/test_case.py": _dedent(
            """
            import os
            from tests.support.pg_helper import require_pg

            def test_case() -> None:
                require_pg(os.environ.get("SPEECHNEXUS_TEST_DATABASE_URL"))
            """
        ),
        "tests/support/pg_helper.py": _dedent(
            """

            def require_pg(dsn: str | None) -> None:
                if not dsn:
                    pytest.skip("not evidence")
            """
        ),
    }
    findings = scan_files(files)
    assert [(finding.path, finding.line, finding.function) for finding in findings] == [
        ("tests/support/pg_helper.py", 3, "require_pg")
    ]


def test_guard_flags_mysql_guard_in_pg_module() -> None:
    source = _dedent(
        """
        import os

        def test_case() -> None:
            postgres = os.environ.get("SPEECHNEXUS_TEST_DATABASE_URL")
            if postgres and not os.environ.get("SPEECHNEXUS_TEST_MYSQL_URL"):
                pytest.skip("only one database configured")
        """
    )
    findings = scan_files({"tests/integration/test_case.py": source})
    assert [(finding.line, finding.guard_variables) for finding in findings] == [
        (6, (MYSQL_VARIABLE,))
    ]


def test_guard_allows_non_dsn_prerequisites() -> None:
    source = _dedent(
        """
        import os
        import shutil

        def test_case(dialect: str) -> None:
            url = os.environ.get("SPEECHNEXUS_TEST_DATABASE_URL")
            if not url:
                pytest.fail("missing DSN")
            if not shutil.which("psql"):
                pytest.skip("host psql is unavailable")
            if dialect != "postgresql":
                pytest.skip("InnoDB holds this schema")
        """
    )
    assert scan_files({"tests/integration/test_case.py": source}) == []


def test_guard_allows_minio_only_skip() -> None:
    source = _dedent(
        """
        import os

        def required() -> None:
            database = os.environ.get("SPEECHNEXUS_TEST_DATABASE_URL")
            if not database:
                pytest.fail("missing PostgreSQL")
            objects = [os.environ.get("SPEECHNEXUS_TEST_MINIO_ENDPOINT")]
            if not all(objects):
                pytest.skip("missing MinIO")
        """
    )
    assert scan_files({"tests/integration/test_case.py": source}) == []


def test_guard_ignores_files_without_database_dsn() -> None:
    source = _dedent(
        """
        import os

        def test_browser() -> None:
            dsn = os.environ.get("SPEECHNEXUS_WORKBENCH_TEST_DSN")
            if not dsn:
                pytest.skip("disposable probe not configured")
        """
    )
    assert scan_files({"tests/integration/test_browser.py": source}) == []


pytestmark = pytest.mark.integration


def test_guard_flags_helper_with_derived_dsn() -> None:
    files = {
        "tests/integration/test_case.py": _dedent(
            """
            import os
            from tests.support.pg_helper import require_pg

            def test_case() -> None:
                dsn = os.environ.get("SPEECHNEXUS_TEST_DATABASE_URL")
                require_pg(dsn.replace("postgresql://", "postgresql+psycopg://"))
            """
        ),
        "tests/support/pg_helper.py": _dedent(
            """
            import pytest

            def require_pg(dsn: str) -> None:
                if not dsn:
                    pytest.skip("not evidence")
            """
        ),
    }
    findings = scan_files(files)
    assert [(finding.path, finding.function) for finding in findings] == [
        ("tests/support/pg_helper.py", "require_pg")
    ]


def test_guard_flags_configured_dict_guard() -> None:
    source = _dedent(
        """
        import os
        import pytest

        VARIABLES = (
            "SPEECHNEXUS_TEST_DATABASE_URL",
            "SPEECHNEXUS_TEST_MYSQL_URL",
        )
        configured = {name: os.environ.get(name) for name in VARIABLES}

        def test_case() -> None:
            if not all(configured):
                pytest.skip("one database is missing")
        """
    )
    findings = scan_files({"tests/integration/test_case.py": source})
    assert [(finding.line, finding.guard_variables) for finding in findings] == [
        (12, (PG_VARIABLE, MYSQL_VARIABLE))
    ]
