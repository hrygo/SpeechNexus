#!/usr/bin/env python3
"""Fail when registered speech HTTP routes drift from the frozen contract.

The checker imports create_app with synthetic settings, reads the routes the
application actually registered, and compares their methods, paths and
operationIds with contracts/speech-openapi.yaml. Response statuses are
derived statically from the handler source: response constructors, HTTP
exceptions, local helpers and the *_errors.py code-to-status maps.

A declared 405 is not trusted on the strength of the route existing: the
checker also sends a request with a method the path does not register and
requires the public error envelope, so a route added without envelope
handling fails instead of passing on the status alone.

It never starts the lifespan, opens a database connection, contacts object
storage or calls an engine. Expressions that cannot be resolved statically are
reported as undetermined rather than guessed; a registered endpoint that cannot
be located in the indexed source tree is a finding, never a silent pass.
"""

from __future__ import annotations

import argparse
import ast
import asyncio
import json
import os
import re
import sys
from collections.abc import Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[2]
CONTRACT = Path("contracts/speech-openapi.yaml")
HTTP_MODULE_ROOT = Path("backend/src/speechnexus_backend/speech/interfaces/http")
SOURCE_ROOTS = ("backend/src", "packages/contracts/src", "packages/media/src")
HTTP_METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "TRACE"})
RESPONSE_CLASSES = frozenset(
    {
        "Response",
        "JSONResponse",
        "StreamingResponse",
        "PlainTextResponse",
        "HTMLResponse",
        "RedirectResponse",
        "FileResponse",
        "ORJSONResponse",
        "UJSONResponse",
    }
)
FRAMEWORK_METHOD_NOT_ALLOWED = 405
FRAMEWORK_PATHS = frozenset({"/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc"})

# A 405 probe must use a method the path does not register. PUT/PATCH/DELETE
# keep the request body empty and are not auto-added by Starlette (only HEAD
# follows GET); TRACE is the last resort for a path that registers everything
# else. The request ID is fixed so the envelope's correlation field is provable.
PROBE_REQUEST_ID = "route-conformance-405-probe"
PROBE_METHODS = ("PUT", "PATCH", "DELETE", "POST", "GET", "TRACE")
PROBE_PATH_PARAMETER = "probe"
ENVELOPE_KEYS = frozenset({"code", "type", "message", "param", "request_id", "retryable"})

# Contract operations this deployment deliberately does not implement. Each is
# registered once by the central placeholder router, answers 503
# operation_not_implemented, and stays listed here until the owning feature
# ships. A new omission must fail instead of silently joining the baseline.
# #432/#436 implemented retryJob and streamJobEvents, and #442/#443 implemented
# listVoices and getVoiceRevision after the original audit, so the audit's 25
# operations are 21 here; a stale entry now fails the check. #473 replaced the
# framework 404/405/400 replies with the placeholder, so "registered" is no
# longer evidence of implementation: the baseline is checked against the
# placeholder table and the single-entry unimplemented error map instead.
KNOWN_UNIMPLEMENTED_OPERATIONS: dict[str, str] = {
    "retireVoiceRevision": "voice revision retirement route is not implemented",
    # Collection-level pronunciation routes are intercepted by
    # PronunciationIngress and rewritten to 400 invalid_request.
    "listPronunciationSets": "pronunciation collection read route is not implemented",
    "createPronunciationRevision": "pronunciation collection write route is not implemented",
    # GET shares /v1/nexus/jobs with createJob and currently receives 405.
    "listJobs": "public Job list route is not implemented",
    # Operation coordination is declared but has no producer.
    "getOperation": "operation read route is not implemented",
    "cancelOperation": "operation cancellation route is not implemented",
    # Realtime sessions and the WebSocket upgrade are not implemented.
    "createRealtimeSession": "realtime session creation is not implemented",
    "getRealtimeSession": "realtime session read is not implemented",
    "cancelRealtimeSession": "realtime session cancellation is not implemented",
    "connectRealtime": "the /v1/realtime WebSocket route is not implemented",
    # Voice creation, review and publication are not implemented.
    "createVoiceDesign": "voice design route is not implemented",
    "createVoiceClone": "voice clone route is not implemented",
    "getVoiceDraft": "voice draft read route is not implemented",
    "updateVoiceDraftMetadata": "voice draft metadata update is not implemented",
    "generateVoiceDraftRevision": "voice draft generation is not implemented",
    "createVoicePreview": "voice draft preview is not implemented",
    "validateVoiceDraftRevision": "voice draft validation is not implemented",
    "publishVoiceDraftRevision": "voice draft publication is not implemented",
    # Content deletion is not implemented.
    "speechCreateDeletionPlan": "speech deletion plan route is not implemented",
    "speechCreateDeletion": "speech deletion route is not implemented",
    "speechGetDeletion": "speech deletion read route is not implemented",
}

# #462 declared every observed status, including framework 405 replies.
# Keep the baseline empty: a newly reachable undeclared status must fail.
KNOWN_UNDECLARED_STATUSES: dict[str, frozenset[int]] = {}

# The single placeholder surface. Its table, its module and its one-entry error
# map are verified against the baseline below, so a placeholder cannot drift
# into a second code or an extra status unnoticed.
UNIMPLEMENTED_ROUTE_MODULE = "speechnexus_backend.speech.interfaces.http.unimplemented"
UNIMPLEMENTED_ERROR_MODULE = "speechnexus_backend.speech.interfaces.http.unimplemented_errors"
UNIMPLEMENTED_OPERATION_CODE = "operation_not_implemented"
UNIMPLEMENTED_OPERATION_STATUS = 503


type LiteralValue = str | int | bool
type ValueSet = frozenset[LiteralValue] | None


@dataclass(frozen=True)
class ContractOperation:
    operation_id: str
    method: str
    path: str
    declared_statuses: frozenset[int]


@dataclass(frozen=True)
class Undetermined:
    operation: str
    location: str
    detail: str
    possible_statuses: tuple[int, ...] = ()


@dataclass(frozen=True)
class RegisteredRoute:
    method: str
    path: str
    operation_id: str | None
    statuses: frozenset[int]
    undetermined: tuple[Undetermined, ...] = ()
    extraction_error: str | None = None
    module: str | None = None


@dataclass(frozen=True)
class Finding:
    kind: str
    detail: str
    operation_id: str | None = None


@dataclass(frozen=True)
class OperationRow:
    operation: ContractOperation
    route: RegisteredRoute | None
    undeclared: tuple[int, ...] = ()
    known_undeclared: tuple[int, ...] = ()
    declared_not_observed: tuple[int, ...] = ()
    unimplemented: bool = False
    envelope_probed: bool = False
    envelope_error: str | None = None


@dataclass(frozen=True)
class UnimplementedSurface:
    """Central placeholder table plus the one-entry error map it answers with."""

    operations: Mapping[str, tuple[str, str]] = field(default_factory=dict)
    error_map: Mapping[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class Report:
    contract_path: str
    contract_version: str
    contract_operations: tuple[ContractOperation, ...]
    registered: tuple[RegisteredRoute, ...]
    other_contract_ids: frozenset[str]
    rows: tuple[OperationRow, ...]
    findings: tuple[Finding, ...]
    known_unimplemented: tuple[str, ...]
    known_status_gaps: tuple[tuple[str, tuple[int, ...]], ...]
    undetermined: tuple[Undetermined, ...]
    unimplemented: tuple[tuple[str, str, str], ...] = ()


@dataclass
class FunctionScope:
    module: ModuleInfo
    node: ast.FunctionDef | ast.AsyncFunctionDef
    qualname: str
    parent: FunctionScope | None = None
    children: dict[str, FunctionScope] = field(default_factory=dict)


@dataclass
class ModuleInfo:
    path: Path
    name: str
    relative_path: str
    tree: ast.Module
    scopes: dict[str, FunctionScope] = field(default_factory=dict)
    module_functions: dict[str, FunctionScope] = field(default_factory=dict)
    imports: dict[str, tuple[str, str]] = field(default_factory=dict)
    constants: dict[str, ValueSet] = field(default_factory=dict)


@dataclass
class FunctionAnalysis:
    statuses: set[int] = field(default_factory=set)
    undetermined: list[Undetermined] = field(default_factory=list)

    def merge(self, other: FunctionAnalysis) -> None:
        self.statuses.update(other.statuses)
        self.undetermined.extend(other.undetermined)


def normalize_path(path: str) -> str:
    """Compare path shape, not the spelling of a parameter."""
    return re.sub(r"\{[^/{}]+\}", "{}", path)


def load_contract(root: Path) -> tuple[str, tuple[ContractOperation, ...]]:
    path = root / CONTRACT
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(document, Mapping):
        raise ValueError(f"{CONTRACT} is not an object")
    info = document.get("info")
    version = info.get("version") if isinstance(info, Mapping) else None
    if not isinstance(version, str) or not version:
        raise ValueError(f"{CONTRACT} has no info.version")
    paths = document.get("paths")
    if not isinstance(paths, Mapping):
        raise ValueError(f"{CONTRACT} has no paths")
    operations: list[ContractOperation] = []
    for raw_path, item in paths.items():
        if not isinstance(raw_path, str) or not isinstance(item, Mapping):
            raise ValueError(f"{CONTRACT} has an invalid path entry")
        for raw_method, specification in item.items():
            method = str(raw_method).upper()
            if method not in HTTP_METHODS:
                continue
            if not isinstance(specification, Mapping):
                raise ValueError(f"{method} {raw_path} is not an object")
            operation_id = specification.get("operationId")
            if not isinstance(operation_id, str) or not operation_id:
                raise ValueError(f"{method} {raw_path} has no operationId")
            responses = specification.get("responses", {})
            if not isinstance(responses, Mapping):
                raise ValueError(f"{operation_id} has no responses object")
            statuses = frozenset(
                int(key) for key in responses if isinstance(key, str) and key.isdigit()
            )
            operations.append(ContractOperation(operation_id, method, raw_path, statuses))
    identifiers = [item.operation_id for item in operations]
    if len(identifiers) != len(set(identifiers)):
        raise ValueError(f"{CONTRACT} declares duplicate operationIds")
    return version, tuple(sorted(operations, key=lambda item: item.operation_id))


def load_other_operation_ids(root: Path) -> frozenset[str]:
    """OperationIds declared by any other contract; used to classify extras."""
    identifiers: set[str] = set()
    for path in sorted((root / "contracts").rglob("*.yaml")):
        if path == root / CONTRACT:
            continue
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(document, Mapping):
            continue
        paths = document.get("paths")
        if not isinstance(paths, Mapping):
            continue
        for item in paths.values():
            if not isinstance(item, Mapping):
                continue
            for specification in item.values():
                if isinstance(specification, Mapping):
                    identifier = specification.get("operationId")
                    if isinstance(identifier, str) and identifier:
                        identifiers.add(identifier)
    return frozenset(identifiers)


def parse_module(path: Path, name: str, root: Path) -> ModuleInfo:
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    try:
        relative = path.relative_to(root).as_posix()
    except ValueError:
        relative = path.as_posix()
    module = ModuleInfo(path=path, name=name, relative_path=relative, tree=tree)
    for statement in tree.body:
        if isinstance(statement, ast.ImportFrom) and statement.module:
            for alias in statement.names:
                module.imports[alias.asname or alias.name] = (statement.module, alias.name)
        if isinstance(statement, ast.Assign):
            value = literal_set(statement.value)
            if value is not None:
                for target in statement.targets:
                    if isinstance(target, ast.Name):
                        module.constants[target.id] = value
        if isinstance(statement, ast.AnnAssign) and isinstance(statement.target, ast.Name):
            value = literal_set(statement.value)
            if value is not None:
                module.constants[statement.target.id] = value
    _collect_scopes(module, tree.body, None)
    return module


def load_modules(root: Path) -> dict[str, ModuleInfo]:
    modules: dict[str, ModuleInfo] = {}
    source_root = root / "backend/src"
    for path in sorted((root / HTTP_MODULE_ROOT).rglob("*.py")):
        relative = path.relative_to(source_root).with_suffix("")
        name = ".".join(relative.parts)
        modules[name] = parse_module(path, name, root)
    return modules


def load_unimplemented_surface(root: Path) -> UnimplementedSurface:
    """Read the central placeholder table and its error map from source.

    The table is parsed, not imported, so the check keeps reading the same
    working tree it reports on and cannot be satisfied by a runtime override.
    """
    directory = root / HTTP_MODULE_ROOT
    table_path = directory / "unimplemented.py"
    tree = ast.parse(table_path.read_text(encoding="utf-8"), filename=str(table_path))
    operations: dict[str, tuple[str, str]] = {}
    for statement in tree.body:
        target: ast.expr | None = None
        value: ast.expr | None = None
        if isinstance(statement, ast.AnnAssign):
            target, value = statement.target, statement.value
        elif isinstance(statement, ast.Assign) and len(statement.targets) == 1:
            target, value = statement.targets[0], statement.value
        if not isinstance(target, ast.Name) or target.id != "UNIMPLEMENTED_OPERATIONS":
            continue
        if not isinstance(value, ast.Dict):
            raise ValueError("UNIMPLEMENTED_OPERATIONS is not a literal table")
        for key, entry in zip(value.keys, value.values, strict=False):
            if not isinstance(key, ast.Constant) or not isinstance(key.value, str):
                raise ValueError("UNIMPLEMENTED_OPERATIONS has a non-literal operationId")
            if not isinstance(entry, (ast.Tuple, ast.List)) or len(entry.elts) != 2:
                raise ValueError(
                    f"UNIMPLEMENTED_OPERATIONS[{key.value!r}] is not a (method, path) pair"
                )
            method, path = (literal_set(element) for element in entry.elts)
            if not isinstance(method, frozenset) or len(method) != 1:
                raise ValueError(f"UNIMPLEMENTED_OPERATIONS[{key.value!r}] has no literal method")
            if not isinstance(path, frozenset) or len(path) != 1:
                raise ValueError(f"UNIMPLEMENTED_OPERATIONS[{key.value!r}] has no literal path")
            raw_method, raw_path = next(iter(method)), next(iter(path))
            if not isinstance(raw_method, str) or not isinstance(raw_path, str):
                raise ValueError(f"UNIMPLEMENTED_OPERATIONS[{key.value!r}] is not text")
            operations[key.value] = (raw_method.upper(), raw_path)
        break
    else:
        raise ValueError("UNIMPLEMENTED_OPERATIONS is missing from the placeholder module")
    error_module = parse_module(
        directory / "unimplemented_errors.py", UNIMPLEMENTED_ERROR_MODULE, root
    )
    extracted = extract_error_mapping(error_module)
    error_map = dict(extracted[1]) if extracted is not None else {}
    return UnimplementedSurface(operations=operations, error_map=error_map)


def _collect_scopes(
    module: ModuleInfo,
    body: Sequence[ast.stmt],
    parent: FunctionScope | None,
) -> None:
    for statement in body:
        if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
            qualname = (
                statement.name if parent is None else f"{parent.qualname}.<locals>.{statement.name}"
            )
            scope = FunctionScope(module=module, node=statement, qualname=qualname, parent=parent)
            module.scopes[qualname] = scope
            if parent is None:
                module.module_functions[statement.name] = scope
            else:
                parent.children[statement.name] = scope
            _collect_scopes(module, statement.body, scope)
        elif isinstance(statement, (ast.If, ast.For, ast.AsyncFor, ast.While)):
            _collect_scopes(module, statement.body, parent)
            _collect_scopes(module, statement.orelse, parent)
        elif isinstance(statement, (ast.With, ast.AsyncWith)):
            _collect_scopes(module, statement.body, parent)
        elif isinstance(statement, ast.Try):
            _collect_scopes(module, statement.body, parent)
            for handler in statement.handlers:
                _collect_scopes(module, handler.body, parent)
            _collect_scopes(module, statement.orelse, parent)
            _collect_scopes(module, statement.finalbody, parent)
        elif isinstance(statement, ast.Match):
            for case in statement.cases:
                _collect_scopes(module, case.body, parent)


def constant_literal(value: object) -> ValueSet:
    if isinstance(value, (bool, str)):
        return frozenset({value})
    if isinstance(value, int) and not isinstance(value, bool):
        return frozenset({value})
    return None


def literal_set(expression: ast.expr | None) -> ValueSet:
    if expression is None:
        return None
    if isinstance(expression, ast.Constant):
        return constant_literal(expression.value)
    if isinstance(expression, (ast.Tuple, ast.List, ast.Set)):
        values: set[LiteralValue] = set()
        for element in expression.elts:
            item = literal_set(element)
            if item is None or len(item) != 1:
                return None
            values.add(next(iter(item)))
        return frozenset(values)
    return None


def extract_error_mapping(module: ModuleInfo) -> tuple[str, dict[str, int]] | None:
    for statement in module.tree.body:
        target: ast.expr | None = None
        value: ast.expr | None = None
        if isinstance(statement, ast.Assign) and len(statement.targets) == 1:
            target, value = statement.targets[0], statement.value
        elif isinstance(statement, ast.AnnAssign):
            target, value = statement.target, statement.value
        if not isinstance(target, ast.Name) or "ERROR" not in target.id:
            continue
        if not isinstance(value, ast.Dict):
            continue
        mapping: dict[str, int] = {}
        for key, entry in zip(value.keys, value.values, strict=False):
            if not isinstance(key, ast.Constant) or not isinstance(key.value, str):
                return None
            if not isinstance(entry, (ast.Tuple, ast.List)) or not entry.elts:
                return None
            status = literal_set(entry.elts[0])
            if status is None or len(status) != 1:
                return None
            item = next(iter(status))
            if type(item) is not int:
                return None
            mapping[key.value] = item
        return target.id, mapping
    return None


def factory_key(module_name: str, qualname: str) -> str:
    """Scope factories by module; several modules define their own _error."""
    return f"{module_name}:{qualname}"


def collect_factories(modules: Mapping[str, ModuleInfo]) -> dict[str, dict[str, int]]:
    factories: dict[str, dict[str, int]] = {}
    for module in modules.values():
        extracted = extract_error_mapping(module)
        if extracted is None:
            continue
        mapping_name, mapping = extracted
        for name, scope in module.module_functions.items():
            if not name.endswith("_error"):
                continue
            if _uses_mapping(scope.node, mapping_name):
                factories[factory_key(module.name, scope.qualname)] = mapping
    return factories


def _uses_mapping(node: ast.AST, mapping_name: str) -> bool:
    return any(
        isinstance(child, ast.Subscript)
        and isinstance(child.value, ast.Name)
        and child.value.id == mapping_name
        for child in ast.walk(node)
    )


def resolve_function(scope: FunctionScope, name: str) -> FunctionScope | None:
    current: FunctionScope | None = scope
    while current is not None:
        if name in current.children:
            return current.children[name]
        current = current.parent
    return scope.module.module_functions.get(name)


def resolve_factory(
    scope: FunctionScope,
    name: str,
    modules: Mapping[str, ModuleInfo],
    factories: Mapping[str, Mapping[str, int]],
) -> Mapping[str, int] | None:
    local = resolve_function(scope, name)
    if local is not None:
        return factories.get(factory_key(local.module.name, local.qualname))
    imported = scope.module.imports.get(name)
    if imported is None:
        return None
    module_name, attribute = imported
    target = modules.get(module_name)
    if target is None:
        return None
    target_scope = target.module_functions.get(attribute)
    if target_scope is None:
        return None
    return factories.get(factory_key(target.name, target_scope.qualname))


def call_name(function: ast.expr) -> str | None:
    if isinstance(function, ast.Name):
        return function.id
    if isinstance(function, ast.Attribute) and (
        function.attr in RESPONSE_CLASSES
        or function.attr == "HTTPException"
        or function.attr == "run_in_threadpool"
        or function.attr == "partial"
    ):
        return function.attr
    return None


class Analyzer:
    """A small abstract interpreter for one route, branching on known constants."""

    def __init__(
        self,
        modules: Mapping[str, ModuleInfo],
        factories: Mapping[str, Mapping[str, int]],
        operation: str,
    ) -> None:
        self._modules = modules
        self._factories = factories
        self._operation = operation
        self._undetermined: dict[tuple[str, str, tuple[int, ...]], Undetermined] = {}
        self._partials: dict[
            tuple[str, str], tuple[FunctionScope, tuple[ast.expr, ...], tuple[ast.keyword, ...]]
        ] = {}

    def function(
        self,
        scope: FunctionScope,
        bindings: Mapping[str, ValueSet],
        stack: tuple[str, ...] = (),
        depth: int = 0,
    ) -> FunctionAnalysis:
        if depth > 12 or scope.qualname in stack:
            return FunctionAnalysis(
                undetermined=[
                    self._record(
                        scope.module.relative_path,
                        scope.node.lineno,
                        "递归或调用深度超过静态分析上限，未展开",
                    )
                ]
            )
        environment: dict[str, ValueSet] = dict(scope.module.constants)
        environment.update(bindings)
        result = FunctionAnalysis()
        self._scan_statements(
            scope.node.body,
            scope,
            environment,
            {},
            result,
            (*stack, scope.qualname),
            depth,
        )
        result.undetermined = list(self._undetermined.values())
        return result

    def _record(
        self,
        relative_path: str,
        line: int,
        detail: str,
        possible: Iterable[int] = (),
    ) -> Undetermined:
        possible_statuses = tuple(sorted(set(possible)))
        item = Undetermined(
            operation=self._operation,
            location=f"{relative_path}:{line}",
            detail=detail,
            possible_statuses=possible_statuses,
        )
        key = (item.location, item.detail, item.possible_statuses)
        self._undetermined.setdefault(key, item)
        return item

    def _scan_statements(
        self,
        statements: Sequence[ast.stmt],
        scope: FunctionScope,
        environment: dict[str, ValueSet],
        narrowings: Mapping[str, ValueSet],
        result: FunctionAnalysis,
        stack: tuple[str, ...],
        depth: int,
    ) -> None:
        for statement in statements:
            if isinstance(statement, ast.Assign):
                self._assign(statement.targets, statement.value, environment, narrowings)
                self._remember_partial(statement.targets, statement.value, scope)
                self._scan_expression(
                    statement.value, scope, environment, narrowings, result, stack, depth
                )
            elif isinstance(statement, ast.AnnAssign):
                if statement.value is not None:
                    self._assign([statement.target], statement.value, environment, narrowings)
                    self._remember_partial([statement.target], statement.value, scope)
                    self._scan_expression(
                        statement.value, scope, environment, narrowings, result, stack, depth
                    )
            elif isinstance(statement, ast.AugAssign):
                self._scan_expression(
                    statement.value, scope, environment, narrowings, result, stack, depth
                )
            elif isinstance(statement, ast.If):
                self._scan_expression(
                    statement.test, scope, environment, narrowings, result, stack, depth
                )
                truth = self._evaluate_bool(statement.test, environment, narrowings)
                if truth is True:
                    self._scan_statements(
                        statement.body, scope, environment, narrowings, result, stack, depth
                    )
                elif truth is False:
                    self._scan_statements(
                        statement.orelse, scope, environment, narrowings, result, stack, depth
                    )
                else:
                    narrowed = self._narrow_from_test(statement.test, environment, narrowings)
                    true_environment = dict(environment)
                    false_environment = dict(environment)
                    self._scan_statements(
                        statement.body,
                        scope,
                        true_environment,
                        narrowed,
                        result,
                        stack,
                        depth,
                    )
                    self._scan_statements(
                        statement.orelse,
                        scope,
                        false_environment,
                        narrowings,
                        result,
                        stack,
                        depth,
                    )
                    environment.clear()
                    environment.update(merge_environments(true_environment, false_environment))
            elif isinstance(statement, (ast.For, ast.AsyncFor)):
                self._scan_expression(
                    statement.iter, scope, environment, narrowings, result, stack, depth
                )
                bind_target_unknown(statement.target, environment)
                self._scan_statements(
                    statement.body, scope, environment, narrowings, result, stack, depth
                )
                self._scan_statements(
                    statement.orelse, scope, environment, narrowings, result, stack, depth
                )
            elif isinstance(statement, ast.While):
                self._scan_expression(
                    statement.test, scope, environment, narrowings, result, stack, depth
                )
                self._scan_statements(
                    statement.body, scope, environment, narrowings, result, stack, depth
                )
                self._scan_statements(
                    statement.orelse, scope, environment, narrowings, result, stack, depth
                )
            elif isinstance(statement, (ast.With, ast.AsyncWith)):
                for item in statement.items:
                    self._scan_expression(
                        item.context_expr, scope, environment, narrowings, result, stack, depth
                    )
                    if item.optional_vars is not None:
                        bind_target_unknown(item.optional_vars, environment)
                self._scan_statements(
                    statement.body, scope, environment, narrowings, result, stack, depth
                )
            elif isinstance(statement, ast.Try):
                self._scan_statements(
                    statement.body, scope, environment, narrowings, result, stack, depth
                )
                for handler in statement.handlers:
                    if handler.type is not None:
                        self._scan_expression(
                            handler.type, scope, environment, narrowings, result, stack, depth
                        )
                    handler_environment = dict(environment)
                    if handler.name:
                        handler_environment[handler.name] = None
                    self._scan_statements(
                        handler.body,
                        scope,
                        handler_environment,
                        narrowings,
                        result,
                        stack,
                        depth,
                    )
                self._scan_statements(
                    statement.orelse, scope, environment, narrowings, result, stack, depth
                )
                self._scan_statements(
                    statement.finalbody, scope, environment, narrowings, result, stack, depth
                )
            elif isinstance(statement, ast.Match):
                self._scan_expression(
                    statement.subject, scope, environment, narrowings, result, stack, depth
                )
                for case in statement.cases:
                    if case.guard is not None:
                        self._scan_expression(
                            case.guard, scope, environment, narrowings, result, stack, depth
                        )
                    case_environment = dict(environment)
                    self._scan_statements(
                        case.body,
                        scope,
                        case_environment,
                        narrowings,
                        result,
                        stack,
                        depth,
                    )
            elif isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            elif isinstance(statement, ast.Return):
                if statement.value is not None:
                    self._scan_expression(
                        statement.value, scope, environment, narrowings, result, stack, depth
                    )
            elif isinstance(statement, ast.Raise):
                if statement.exc is not None:
                    self._scan_expression(
                        statement.exc, scope, environment, narrowings, result, stack, depth
                    )
            elif isinstance(statement, ast.Expr):
                self._scan_expression(
                    statement.value, scope, environment, narrowings, result, stack, depth
                )
            else:
                for child in ast.iter_child_nodes(statement):
                    if isinstance(child, ast.expr):
                        self._scan_expression(
                            child, scope, environment, narrowings, result, stack, depth
                        )

    def _remember_partial(
        self,
        targets: Sequence[ast.expr],
        value_expression: ast.expr,
        scope: FunctionScope,
    ) -> None:
        for target in targets:
            if isinstance(target, ast.Name):
                self._partials.pop((scope.qualname, target.id), None)
        if not isinstance(value_expression, ast.Call):
            return
        if call_name(value_expression.func) != "partial" or not value_expression.args:
            return
        function = value_expression.args[0]
        if not isinstance(function, ast.Name):
            return
        local = resolve_function(scope, function.id)
        if local is None:
            return
        for target in targets:
            if isinstance(target, ast.Name):
                self._partials[(scope.qualname, target.id)] = (
                    local,
                    tuple(value_expression.args[1:]),
                    tuple(value_expression.keywords),
                )

    def _assign(
        self,
        targets: Sequence[ast.expr],
        value_expression: ast.expr,
        environment: dict[str, ValueSet],
        narrowings: Mapping[str, ValueSet],
    ) -> None:
        value = self._evaluate(value_expression, environment, narrowings)
        for target in targets:
            self._bind(target, value, value_expression, environment, narrowings)

    def _bind(
        self,
        target: ast.expr,
        value: ValueSet,
        value_expression: ast.expr,
        environment: dict[str, ValueSet],
        narrowings: Mapping[str, ValueSet],
    ) -> None:
        if isinstance(target, ast.Name):
            environment[target.id] = value
            return
        if (
            isinstance(target, (ast.Tuple, ast.List))
            and isinstance(value_expression, (ast.Tuple, ast.List))
            and len(target.elts) == len(value_expression.elts)
        ):
            for element_target, element_value in zip(
                target.elts, value_expression.elts, strict=False
            ):
                self._bind(
                    element_target,
                    self._evaluate(element_value, environment, narrowings),
                    element_value,
                    environment,
                    narrowings,
                )
            return
        bind_target_unknown(target, environment)

    def _scan_expression(
        self,
        expression: ast.expr,
        scope: FunctionScope,
        environment: dict[str, ValueSet],
        narrowings: Mapping[str, ValueSet],
        result: FunctionAnalysis,
        stack: tuple[str, ...],
        depth: int,
    ) -> None:
        if isinstance(expression, ast.Call):
            self._handle_call(expression, scope, environment, narrowings, result, stack, depth)
            for argument in expression.args:
                self._scan_expression(
                    argument, scope, environment, narrowings, result, stack, depth
                )
            for keyword in expression.keywords:
                self._scan_expression(
                    keyword.value, scope, environment, narrowings, result, stack, depth
                )
            return
        if isinstance(expression, ast.Lambda):
            return
        if isinstance(expression, ast.IfExp):
            self._scan_expression(
                expression.test, scope, environment, narrowings, result, stack, depth
            )
            self._scan_expression(
                expression.body, scope, environment, narrowings, result, stack, depth
            )
            self._scan_expression(
                expression.orelse, scope, environment, narrowings, result, stack, depth
            )
            return
        for child in ast.iter_child_nodes(expression):
            if isinstance(child, ast.expr):
                self._scan_expression(child, scope, environment, narrowings, result, stack, depth)

    def _handle_call(
        self,
        call: ast.Call,
        scope: FunctionScope,
        environment: dict[str, ValueSet],
        narrowings: Mapping[str, ValueSet],
        result: FunctionAnalysis,
        stack: tuple[str, ...],
        depth: int,
    ) -> None:
        name = call_name(call.func)
        if name is None:
            return
        if name == "run_in_threadpool":
            self._handle_indirect(call, scope, environment, narrowings, result, stack, depth)
            return
        if name == "HTTPException":
            status = self._status_keyword(call, environment, narrowings, result, scope)
            if not status and call.args:
                # Starlette's first positional argument is status_code:
                # HTTPException(418, "teapot").
                values = self._evaluate(call.args[0], environment, narrowings)
                if values is None:
                    self._record(
                        scope.module.relative_path,
                        call.lineno,
                        "HTTPException 位置参数无法静态求值，未猜造状态码",
                    )
                else:
                    status = {value for value in values if type(value) is int}
            result.statuses.update(status)
            return
        if name in RESPONSE_CLASSES or name.endswith("Response"):
            status = self._status_keyword(call, environment, narrowings, result, scope)
            if not status:
                status = {200}
            result.statuses.update(status)
            return
        factory = resolve_factory(scope, name, self._modules, self._factories)
        if factory is not None:
            self._handle_error_factory(call, factory, environment, narrowings, result, scope)
            return
        local = resolve_function(scope, name)
        if local is not None:
            self._analyze_local(call, local, environment, narrowings, result, stack, depth)

    def _handle_indirect(
        self,
        call: ast.Call,
        scope: FunctionScope,
        environment: dict[str, ValueSet],
        narrowings: Mapping[str, ValueSet],
        result: FunctionAnalysis,
        stack: tuple[str, ...],
        depth: int,
    ) -> None:
        if not call.args or not isinstance(call.args[0], ast.Name):
            return
        name = call.args[0].id
        partial = self._partials.get((scope.qualname, name))
        if partial is not None:
            target, partial_args, partial_keywords = partial
            bindings = self._bind_arguments(
                target,
                (*partial_args, *call.args[1:]),
                (*partial_keywords, *call.keywords),
                environment,
                narrowings,
            )
            result.merge(self.function(target, bindings, stack, depth + 1))
            return
        local = resolve_function(scope, name)
        if local is None:
            return
        bindings = self._bind_arguments(
            local, call.args[1:], call.keywords, environment, narrowings
        )
        result.merge(self.function(local, bindings, stack, depth + 1))

    def _analyze_local(
        self,
        call: ast.Call,
        local: FunctionScope,
        environment: dict[str, ValueSet],
        narrowings: Mapping[str, ValueSet],
        result: FunctionAnalysis,
        stack: tuple[str, ...],
        depth: int,
    ) -> None:
        bindings = self._bind_arguments(local, call.args, call.keywords, environment, narrowings)
        result.merge(self.function(local, bindings, stack, depth + 1))

    def _bind_arguments(
        self,
        local: FunctionScope,
        arguments: Sequence[ast.expr],
        keywords: Sequence[ast.keyword],
        environment: dict[str, ValueSet],
        narrowings: Mapping[str, ValueSet],
    ) -> dict[str, ValueSet]:
        parameters = [argument.arg for argument in local.node.args.args]
        bindings: dict[str, ValueSet] = {}
        for parameter_name, argument in zip(parameters, arguments, strict=False):
            bindings[parameter_name] = self._evaluate(argument, environment, narrowings)
        for keyword in keywords:
            if keyword.arg is not None:
                bindings[keyword.arg] = self._evaluate(keyword.value, environment, narrowings)
        defaults = local.node.args.defaults
        if defaults:
            for parameter, default in zip(
                local.node.args.args[-len(defaults) :], defaults, strict=False
            ):
                if parameter.arg not in bindings:
                    value = literal_set(default)
                    if value is not None:
                        bindings[parameter.arg] = value
        for parameter, keyword_default in zip(
            local.node.args.kwonlyargs, local.node.args.kw_defaults, strict=False
        ):
            if keyword_default is not None and parameter.arg not in bindings:
                value = literal_set(keyword_default)
                if value is not None:
                    bindings[parameter.arg] = value
        return bindings

    def _status_keyword(
        self,
        call: ast.Call,
        environment: dict[str, ValueSet],
        narrowings: Mapping[str, ValueSet],
        result: FunctionAnalysis,
        scope: FunctionScope,
    ) -> set[int]:
        expression = next(
            (keyword.value for keyword in call.keywords if keyword.arg == "status_code"), None
        )
        if expression is None:
            return set()
        values = self._evaluate(expression, environment, narrowings)
        if values is None:
            self._record(
                scope.module.relative_path,
                call.lineno,
                "status_code 表达式无法静态求值，未猜造状态码",
            )
            return set()
        statuses = {value for value in values if type(value) is int}
        if not statuses:
            self._record(
                scope.module.relative_path,
                call.lineno,
                "status_code 没有静态整数取值，未猜造状态码",
            )
        return statuses

    def _handle_error_factory(
        self,
        call: ast.Call,
        factory: Mapping[str, int],
        environment: dict[str, ValueSet],
        narrowings: Mapping[str, ValueSet],
        result: FunctionAnalysis,
        scope: FunctionScope,
    ) -> None:
        possible = tuple(sorted(set(factory.values())))
        if not call.args:
            self._record(
                scope.module.relative_path,
                call.lineno,
                "错误工厂没有 code 参数，无法静态判定",
                possible,
            )
            return
        values = self._evaluate(call.args[0], environment, narrowings)
        if values is None:
            if len(factory) == 1:
                # A single-entry factory can only ever return that one status,
                # whatever it was handed: every successful return uses it.
                result.statuses.add(next(iter(factory.values())))
                return
            self._record(
                scope.module.relative_path,
                call.lineno,
                "错误码来自运行时表达式（如 error.code），未静态求值",
                possible,
            )
            return
        for value in values:
            if not isinstance(value, str):
                self._record(
                    scope.module.relative_path,
                    call.lineno,
                    "错误码不是字符串常量，无法静态判定",
                    possible,
                )
                continue
            status = factory.get(value)
            if status is None:
                self._record(
                    scope.module.relative_path,
                    call.lineno,
                    f"错误码 {value!r} 不在该错误工厂映射中",
                    possible,
                )
            else:
                result.statuses.add(status)

    def _evaluate(
        self,
        expression: ast.expr,
        environment: Mapping[str, ValueSet],
        narrowings: Mapping[str, ValueSet],
    ) -> ValueSet:
        if isinstance(expression, ast.Constant):
            return constant_literal(expression.value)
        if isinstance(expression, ast.Name):
            return environment.get(expression.id)
        if isinstance(expression, ast.Attribute):
            return narrowings.get(attribute_key(expression))
        if isinstance(expression, (ast.Tuple, ast.List, ast.Set)):
            values: set[LiteralValue] = set()
            for element in expression.elts:
                item = self._evaluate(element, environment, narrowings)
                if item is None or len(item) != 1:
                    return None
                values.add(next(iter(item)))
            return frozenset(values)
        if isinstance(expression, ast.IfExp):
            truth = self._evaluate_bool(expression.test, environment, narrowings)
            if truth is True:
                return self._evaluate(expression.body, environment, narrowings)
            if truth is False:
                return self._evaluate(expression.orelse, environment, narrowings)
            body = self._evaluate(expression.body, environment, narrowings)
            alternative = self._evaluate(expression.orelse, environment, narrowings)
            if body is None or alternative is None:
                return None
            return body | alternative
        if isinstance(expression, ast.BoolOp) and isinstance(expression.op, ast.Or):
            truth = self._evaluate_bool(expression, environment, narrowings)
            if truth is not None:
                return frozenset({truth})
            return None
        if isinstance(expression, ast.UnaryOp) and isinstance(expression.op, ast.Not):
            truth = self._evaluate_bool(expression, environment, narrowings)
            return None if truth is None else frozenset({truth})
        if isinstance(expression, ast.Compare):
            truth = self._evaluate_bool(expression, environment, narrowings)
            return None if truth is None else frozenset({truth})
        if isinstance(expression, ast.BinOp) and isinstance(expression.op, ast.Add):
            left = self._evaluate(expression.left, environment, narrowings)
            right = self._evaluate(expression.right, environment, narrowings)
            if left is None or right is None:
                return None
            combined: set[LiteralValue] = set()
            for first in left:
                for second in right:
                    if isinstance(first, str) and isinstance(second, str):
                        combined.add(first + second)
                    else:
                        return None
                    if len(combined) > 100:
                        return None
            return frozenset(combined)
        if isinstance(expression, ast.JoinedStr):
            parts: list[str] = []
            for part in expression.values:
                if isinstance(part, ast.Constant) and isinstance(part.value, str):
                    parts.append(part.value)
                elif isinstance(part, ast.FormattedValue):
                    evaluated = self._evaluate(part.value, environment, narrowings)
                    if evaluated is None or len(evaluated) != 1:
                        return None
                    formatted = next(iter(evaluated))
                    if not isinstance(formatted, str) and type(formatted) is not int:
                        return None
                    parts.append(str(formatted))
                else:
                    return None
            return frozenset({"".join(parts)})
        return None

    def _evaluate_bool(
        self,
        expression: ast.expr,
        environment: Mapping[str, ValueSet],
        narrowings: Mapping[str, ValueSet],
    ) -> bool | None:
        if isinstance(expression, ast.Constant) and isinstance(expression.value, bool):
            return expression.value
        if isinstance(expression, ast.Name):
            values = environment.get(expression.id)
            if values is not None and len(values) == 1:
                item = next(iter(values))
                if isinstance(item, bool):
                    return item
            return None
        if isinstance(expression, ast.UnaryOp) and isinstance(expression.op, ast.Not):
            truth = self._evaluate_bool(expression.operand, environment, narrowings)
            return None if truth is None else not truth
        if isinstance(expression, ast.BoolOp):
            truth_values = [
                self._evaluate_bool(value, environment, narrowings) for value in expression.values
            ]
            if isinstance(expression.op, ast.And):
                if any(value is False for value in truth_values):
                    return False
                if all(value is True for value in truth_values):
                    return True
                return None
            if any(value is True for value in truth_values):
                return True
            if all(value is False for value in truth_values):
                return False
            return None
        if isinstance(expression, ast.Compare) and len(expression.ops) == 1:
            left = self._evaluate(expression.left, environment, narrowings)
            right = self._evaluate(expression.comparators[0], environment, narrowings)
            if left is None or right is None or len(left) != 1:
                return None
            left_value = next(iter(left))
            operation = expression.ops[0]
            if isinstance(operation, ast.In):
                return left_value in right
            if isinstance(operation, ast.NotIn):
                return left_value not in right
            if isinstance(operation, (ast.Eq, ast.NotEq, ast.Is, ast.IsNot)):
                matches = left_value in right
                if isinstance(operation, (ast.NotEq, ast.IsNot)):
                    return not matches
                return matches
        return None

    def _narrow_from_test(
        self,
        test: ast.expr,
        environment: Mapping[str, ValueSet],
        narrowings: Mapping[str, ValueSet],
    ) -> Mapping[str, ValueSet]:
        if (
            isinstance(test, ast.Compare)
            and len(test.ops) == 1
            and isinstance(test.ops[0], ast.Eq)
            and isinstance(test.left, ast.Attribute)
        ):
            value = self._evaluate(test.comparators[0], environment, narrowings)
            if value is not None and all(isinstance(item, str) for item in value):
                narrowed = dict(narrowings)
                narrowed[attribute_key(test.left)] = value
                return narrowed
        return narrowings


def attribute_key(expression: ast.Attribute) -> str:
    try:
        return ast.unparse(expression)
    except Exception:  # pragma: no cover - ast.unparse is stable for handler code
        return expression.attr


def bind_target_unknown(target: ast.expr, environment: dict[str, ValueSet]) -> None:
    if isinstance(target, ast.Name):
        environment[target.id] = None
        return
    if isinstance(target, (ast.Tuple, ast.List)):
        for element in target.elts:
            bind_target_unknown(element, environment)
    elif isinstance(target, ast.Starred):
        bind_target_unknown(target.value, environment)


def merge_environments(
    first: Mapping[str, ValueSet],
    second: Mapping[str, ValueSet],
) -> dict[str, ValueSet]:
    merged: dict[str, ValueSet] = {}
    for name in set(first) | set(second):
        left = first.get(name)
        right = second.get(name)
        if left is None or right is None:
            merged[name] = None
        else:
            merged[name] = left | right
    return merged


def flatten_routes(routes: Iterable[Any], prefix: str = "") -> Iterator[Any]:
    for route in routes:
        original = getattr(route, "original_router", None)
        if original is not None:
            context = getattr(route, "include_context", None)
            nested_prefix = getattr(context, "prefix", "") or ""
            yield from flatten_routes(original.routes, prefix + nested_prefix)
        else:
            path = getattr(route, "path", None)
            if isinstance(path, str) and prefix and not path.startswith(prefix):
                route.path = prefix.rstrip("/") + "/" + path.lstrip("/")
            yield route


def load_app(root: Path = ROOT, *, assets: Any = None) -> Any:
    _ensure_source_path(root)
    from speechnexus_backend.app import create_app
    from speechnexus_backend.settings import Settings

    with _without_speechnexus_environment():
        settings = Settings(
            _env_file=None,
            environment="test",
            database_url="postgresql://127.0.0.1:1/speechnexus-conformance",
            object_storage_endpoint="http://127.0.0.1:1",
            object_storage_healthcheck_url="http://127.0.0.1:1/health",
            object_storage_bucket="speechnexus-conformance",
        )
    return create_app(settings=settings, assets=assets)


def probe_app(root: Path = ROOT) -> Any:
    """Load the application with an inert asset service for the 405 probe.

    AssetIngress answers 503 when no asset service is configured, before the
    router can reject a wrong method. The probe never reaches an asset
    endpoint, so an inert double enables the middleware without opening an
    object-storage connection.
    """
    from unittest.mock import Mock

    from speechnexus_backend.speech.application.assets import AssetService

    return load_app(root, assets=Mock(spec=AssetService))


def _ensure_source_path(root: Path) -> None:
    for relative in SOURCE_ROOTS:
        path = str((root / relative).resolve())
        if path not in sys.path:
            sys.path.insert(0, path)


@contextmanager
def _without_speechnexus_environment() -> Iterator[None]:
    saved = {key: value for key, value in os.environ.items() if key.startswith("SPEECHNEXUS_")}
    for key in saved:
        del os.environ[key]
    try:
        yield
    finally:
        os.environ.update(saved)


def discover_app_routes(
    root: Path,
    modules: Mapping[str, ModuleInfo],
    factories: Mapping[str, Mapping[str, int]],
    *,
    app: Any = None,
) -> tuple[RegisteredRoute, ...]:
    app = load_app(root) if app is None else app
    registered: list[RegisteredRoute] = []
    seen: set[int] = set()
    for route in flatten_routes(app.routes):
        identifier = id(route)
        if identifier in seen:
            continue
        seen.add(identifier)
        methods = getattr(route, "methods", None)
        path = getattr(route, "path", None)
        if not methods or not isinstance(path, str) or path in FRAMEWORK_PATHS:
            continue
        operation_id = getattr(route, "operation_id", None)
        if operation_id is not None and not isinstance(operation_id, str):
            operation_id = None
        endpoint = getattr(route, "endpoint", None)
        statuses, undetermined, extraction_error = analyze_endpoint(
            endpoint, operation_id, modules, factories
        )
        module_name = getattr(endpoint, "__module__", None)
        for method in sorted(method.upper() for method in methods):
            if method not in HTTP_METHODS:
                continue
            registered.append(
                RegisteredRoute(
                    method=method,
                    path=path,
                    operation_id=operation_id,
                    statuses=frozenset(statuses) | {FRAMEWORK_METHOD_NOT_ALLOWED},
                    undetermined=tuple(undetermined),
                    extraction_error=extraction_error,
                    module=module_name if isinstance(module_name, str) else None,
                )
            )
    return tuple(
        sorted(
            registered,
            key=lambda item: (
                item.path,
                item.method,
                item.operation_id or "",
            ),
        )
    )


def fill_path_parameters(path: str) -> str:
    """Render a contract path template into a concrete path for the probe."""
    return re.sub(r"\{[^/{}]+\}", PROBE_PATH_PARAMETER, path)


def envelope_problem(response: Any, request_id: str) -> str | None:
    """Explain why a 405 reply is not the public envelope, or return None."""
    if response.status_code != FRAMEWORK_METHOD_NOT_ALLOWED:
        return f"the probe request answered {response.status_code} instead of 405"
    try:
        body = response.json()
    except ValueError:
        return "the 405 body is not JSON"
    if not isinstance(body, Mapping):
        return "the 405 body is not a JSON object"
    error = body.get("error")
    if not isinstance(error, Mapping):
        return "the 405 body is not the public error envelope"
    missing = sorted(ENVELOPE_KEYS - set(error))
    if missing:
        return f"the 405 error envelope is missing {', '.join(missing)}"
    if error.get("request_id") != request_id:
        return "the 405 error envelope does not echo the request id"
    if not isinstance(error.get("code"), str) or not error["code"]:
        return "the 405 error envelope has no code"
    if error.get("retryable") is not False:
        return "the 405 error envelope must be non-retryable"
    if "detail" in body:
        return "the 405 body also carries a framework detail field"
    return None


async def probe_method_not_allowed_envelopes(
    app: Any, operations: Sequence[ContractOperation]
) -> dict[str, str | None]:
    """Request a wrong method for each declared-405 operation; require the envelope.

    The probe sees what a consumer sees: the full middleware stack, the
    router 405, and the application exception handler. The method is one the
    path does not register, chosen with the placeholder router matcher
    (methods_for_path, #473), so the probe adds no second path-matching rule.
    """
    import httpx

    from speechnexus_backend.speech.interfaces.http.unimplemented import methods_for_path

    results: dict[str, str | None] = {}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://localhost"
    ) as client:
        for operation in operations:
            if FRAMEWORK_METHOD_NOT_ALLOWED not in operation.declared_statuses:
                continue
            path = fill_path_parameters(operation.path)
            registered = methods_for_path(app.router.routes, path)
            method = next(
                (candidate for candidate in PROBE_METHODS if candidate not in registered),
                None,
            )
            if method is None:
                results[operation.operation_id] = (
                    "every probe method is registered on the path, so 405 was not exercised"
                )
                continue
            response = await client.request(
                method, path, headers={"X-Request-ID": PROBE_REQUEST_ID}
            )
            problem = envelope_problem(response, PROBE_REQUEST_ID)
            results[operation.operation_id] = (
                None if problem is None else f"{method} {path}: {problem}"
            )
    return results


def analyze_endpoint(
    endpoint: Any,
    operation_id: str | None,
    modules: Mapping[str, ModuleInfo],
    factories: Mapping[str, Mapping[str, int]],
) -> tuple[set[int], list[Undetermined], str | None]:
    operation = operation_id or "<no-operation-id>"
    if endpoint is None:
        detail = "endpoint 为空，无法静态提取状态码"
        return set(), [Undetermined(operation, "app.routes", detail, ())], detail
    module_name = getattr(endpoint, "__module__", None)
    qualname = getattr(endpoint, "__qualname__", None)
    module = modules.get(module_name) if isinstance(module_name, str) else None
    scope = (
        module.scopes.get(qualname) if module is not None and isinstance(qualname, str) else None
    )
    if module is None or scope is None:
        location = f"{module_name or '?'}.{qualname or '?'}"
        detail = f"无法在源码索引中定位 endpoint（{location}），状态码未判定"
        return set(), [Undetermined(operation, location, detail, ())], detail
    bindings: dict[str, ValueSet] = {}
    if operation_id is not None:
        # Some factories create one handler per operation and close over the
        # operationId (for example profile_publications.handler(operation)).
        # Bind it so only that operation's branches are analyzed.
        current: FunctionScope | None = scope
        while current is not None:
            parameter_names = {argument.arg for argument in current.node.args.args}
            if "operation" in parameter_names:
                bindings["operation"] = frozenset({operation_id})
                break
            current = current.parent
    analysis = Analyzer(modules, factories, operation).function(scope, bindings)
    return analysis.statuses, analysis.undetermined, None


def evaluate(
    contract_operations: Sequence[ContractOperation],
    registered: Sequence[RegisteredRoute],
    *,
    other_contract_ids: frozenset[str],
    baseline_unimplemented: Mapping[str, str] | None = None,
    baseline_statuses: Mapping[str, frozenset[int]] | None = None,
    unimplemented: UnimplementedSurface | None = None,
    method_not_allowed_envelopes: Mapping[str, str | None] | None = None,
) -> Report:
    unimplemented_baseline = baseline_unimplemented or {}
    status_baseline = baseline_statuses or {}
    placeholder = unimplemented or UnimplementedSurface()
    by_identifier = {operation.operation_id: operation for operation in contract_operations}
    by_route: dict[str, RegisteredRoute] = {}
    findings: list[Finding] = []
    for candidate in registered:
        if candidate.operation_id is None:
            continue
        if candidate.operation_id in by_route:
            findings.append(
                Finding(
                    "duplicate_registered_operation",
                    f"{candidate.method} {candidate.path} registers operationId "
                    f"{candidate.operation_id} more than once",
                    candidate.operation_id,
                )
            )
        else:
            by_route[candidate.operation_id] = candidate
    for candidate in registered:
        if candidate.extraction_error is not None:
            findings.append(
                Finding(
                    "status_extraction_failed",
                    f"{candidate.method} {candidate.path} "
                    f"({candidate.operation_id or 'no-operation-id'}): "
                    f"{candidate.extraction_error}",
                    candidate.operation_id,
                )
            )
    rows: list[OperationRow] = []
    for operation in contract_operations:
        route = by_route.get(operation.operation_id)
        is_placeholder = operation.operation_id in placeholder.operations
        if route is None:
            findings.append(
                Finding(
                    "declared_not_registered",
                    f"{operation.method} {operation.path} declares {operation.operation_id} "
                    "but no route is registered"
                    + (" (its central placeholder route is missing)" if is_placeholder else ""),
                    operation.operation_id,
                )
            )
            rows.append(OperationRow(operation, None, unimplemented=is_placeholder))
            continue
        if route.method != operation.method or normalize_path(route.path) != normalize_path(
            operation.path
        ):
            findings.append(
                Finding(
                    "operation_route_mismatch",
                    f"{operation.operation_id} is declared as {operation.method} {operation.path} "
                    f"but registered as {route.method} {route.path}",
                    operation.operation_id,
                )
            )
        undeclared = route.statuses - operation.declared_statuses
        declared_not_observed = operation.declared_statuses - route.statuses
        known = status_baseline.get(operation.operation_id, frozenset())
        new_undeclared = undeclared - known
        stale = known - undeclared
        for status in sorted(new_undeclared):
            findings.append(
                Finding(
                    "undeclared_response_status",
                    f"{operation.operation_id} can return {status}; "
                    f"declared responses are {format_statuses(operation.declared_statuses)}",
                    operation.operation_id,
                )
            )
        for status in sorted(stale):
            findings.append(
                Finding(
                    "stale_status_baseline",
                    f"{operation.operation_id} no longer returns the baselined undeclared "
                    f"status {status}; remove it from KNOWN_UNDECLARED_STATUSES",
                    operation.operation_id,
                )
            )
        envelope_probed = False
        envelope_error: str | None = None
        if FRAMEWORK_METHOD_NOT_ALLOWED in operation.declared_statuses:
            if method_not_allowed_envelopes is None:
                # Synthetic evaluations may not run the probe. Leave the row
                # explicitly unprobed so reporting cannot call it verified.
                pass
            elif operation.operation_id not in method_not_allowed_envelopes:
                envelope_error = "the 405 response envelope was not probed"
            else:
                envelope_probed = True
                envelope_error = method_not_allowed_envelopes[operation.operation_id]
            if envelope_error is not None:
                findings.append(
                    Finding(
                        "method_not_allowed_envelope_missing",
                        f"{operation.operation_id} declares 405 but {envelope_error}",
                        operation.operation_id,
                    )
                )
        rows.append(
            OperationRow(
                operation=operation,
                route=route,
                undeclared=tuple(sorted(undeclared)),
                known_undeclared=tuple(sorted(known & undeclared)),
                declared_not_observed=tuple(sorted(declared_not_observed)),
                unimplemented=is_placeholder,
                envelope_probed=envelope_probed,
                envelope_error=envelope_error,
            )
        )
    if unimplemented is not None and dict(placeholder.error_map) != {
        UNIMPLEMENTED_OPERATION_CODE: UNIMPLEMENTED_OPERATION_STATUS
    }:
        findings.append(
            Finding(
                "unimplemented_error_map_drift",
                "the placeholder error map must contain only "
                f"{UNIMPLEMENTED_OPERATION_CODE!r} -> {UNIMPLEMENTED_OPERATION_STATUS}; "
                f"found {sorted(placeholder.error_map.items())}",
            )
        )
    for operation_id in sorted(unimplemented_baseline):
        if operation_id not in by_identifier:
            findings.append(
                Finding(
                    "baseline_operation_unknown",
                    f"baseline names {operation_id}, which the contract does not declare",
                    operation_id,
                )
            )
            continue
        declared = placeholder.operations.get(operation_id)
        if declared is None:
            findings.append(
                Finding(
                    "stale_unimplemented_baseline",
                    f"{operation_id} is listed as unimplemented but the central placeholder "
                    "table does not declare it; remove it from "
                    "KNOWN_UNIMPLEMENTED_OPERATIONS",
                    operation_id,
                )
            )
            continue
        route = by_route.get(operation_id)
        if route is None:
            # The row loop already reports the missing route.
            continue
        method, path = declared
        observed = frozenset(route.statuses - {FRAMEWORK_METHOD_NOT_ALLOWED})
        expected = frozenset({UNIMPLEMENTED_OPERATION_STATUS})
        problems: list[str] = []
        if route.module != UNIMPLEMENTED_ROUTE_MODULE:
            problems.append(
                f"is registered by {route.module or 'an unknown module'} instead of "
                f"{UNIMPLEMENTED_ROUTE_MODULE}"
            )
        if route.method != method or normalize_path(route.path) != normalize_path(path):
            problems.append(
                f"is registered as {route.method} {route.path} instead of {method} {path}"
            )
        if observed != expected:
            problems.append(
                f"observes {format_statuses(observed)} instead of only "
                f"{UNIMPLEMENTED_OPERATION_STATUS}"
            )
        if problems:
            findings.append(
                Finding(
                    "unimplemented_placeholder_drift",
                    f"{operation_id} " + "; ".join(problems),
                    operation_id,
                )
            )
    for operation_id in sorted(set(placeholder.operations) - set(unimplemented_baseline)):
        findings.append(
            Finding(
                "unimplemented_table_unknown",
                f"the central placeholder table declares {operation_id}, which "
                "KNOWN_UNIMPLEMENTED_OPERATIONS does not list",
                operation_id,
            )
        )
    for operation_id, statuses in sorted(status_baseline.items()):
        if operation_id not in by_identifier:
            findings.append(
                Finding(
                    "baseline_status_operation_unknown",
                    f"status baseline names {operation_id}, which the contract does not declare",
                    operation_id,
                )
            )
        elif operation_id not in by_route:
            for status in sorted(statuses):
                findings.append(
                    Finding(
                        "stale_status_baseline",
                        f"{operation_id} has no registered route; remove baselined status {status}",
                        operation_id,
                    )
                )
    for route in registered:
        if route.operation_id is not None and route.operation_id in by_identifier:
            continue
        if route.operation_id is not None and route.operation_id in other_contract_ids:
            continue
        kind = (
            "registered_without_operation_id"
            if route.operation_id is None
            else "registered_not_declared"
        )
        findings.append(
            Finding(
                kind,
                f"{route.method} {route.path} is registered but not declared "
                "by the speech contract",
                route.operation_id,
            )
        )
    undetermined: dict[tuple[str, str, str, tuple[int, ...]], Undetermined] = {}
    for route in registered:
        if route.operation_id not in by_identifier and (
            route.operation_id is None or route.operation_id in other_contract_ids
        ):
            # Other surfaces (admin, operations, workbench) are declared
            # elsewhere; only speech operations and undeclared extras matter.
            continue
        for item in route.undetermined:
            undetermined.setdefault(
                (item.operation, item.location, item.detail, item.possible_statuses), item
            )
    return Report(
        contract_path=CONTRACT.as_posix(),
        contract_version="",
        contract_operations=tuple(contract_operations),
        registered=tuple(registered),
        other_contract_ids=other_contract_ids,
        rows=tuple(rows),
        findings=tuple(findings),
        known_unimplemented=tuple(sorted(unimplemented_baseline)),
        known_status_gaps=tuple(
            (operation_id, tuple(sorted(statuses)))
            for operation_id, statuses in sorted(status_baseline.items())
        ),
        undetermined=tuple(
            sorted(
                undetermined.values(),
                key=lambda item: (item.operation, item.location, item.detail),
            )
        ),
        unimplemented=tuple(
            sorted(
                (operation_id, method, path)
                for operation_id, (method, path) in placeholder.operations.items()
            )
        ),
    )


def declared_method_not_allowed_operations(
    contract_operations: Sequence[ContractOperation],
) -> tuple[ContractOperation, ...]:
    return tuple(
        operation
        for operation in contract_operations
        if FRAMEWORK_METHOD_NOT_ALLOWED in operation.declared_statuses
    )


def require_envelope_coverage(
    operations: Sequence[ContractOperation],
    envelopes: Mapping[str, str | None],
) -> None:
    """Refuse to report a declared 405 as verified when it was never probed."""
    expected = {operation.operation_id for operation in operations}
    missing = sorted(expected - set(envelopes))
    if missing:
        raise RuntimeError(
            "the 405 envelope probe returned no result for "
            + ", ".join(missing)
            + "; refusing to report declared 405s as verified"
        )


def build_report(root: Path = ROOT) -> Report:
    contract_version, contract_operations = load_contract(root)
    other_contract_ids = load_other_operation_ids(root)
    modules = load_modules(root)
    factories = collect_factories(modules)
    app = probe_app(root)
    registered = discover_app_routes(root, modules, factories, app=app)
    declared_405 = declared_method_not_allowed_operations(contract_operations)
    envelopes = asyncio.run(probe_method_not_allowed_envelopes(app, declared_405))
    require_envelope_coverage(declared_405, envelopes)
    report = evaluate(
        contract_operations,
        registered,
        other_contract_ids=other_contract_ids,
        baseline_unimplemented=KNOWN_UNIMPLEMENTED_OPERATIONS,
        baseline_statuses=KNOWN_UNDECLARED_STATUSES,
        unimplemented=load_unimplemented_surface(root),
        method_not_allowed_envelopes=envelopes,
    )
    return replace(report, contract_version=contract_version)


def format_statuses(statuses: Iterable[int]) -> str:
    return ",".join(str(status) for status in sorted(statuses)) or "-"


def format_report(report: Report) -> str:
    contract_ids = {operation.operation_id for operation in report.contract_operations}
    speech_routes = [route for route in report.registered if route.operation_id in contract_ids]
    lines = [
        f"Speech route conformance: {report.contract_path} ({report.contract_version})",
        f"Declared operations: {len(report.contract_operations)}; "
        f"registered speech operations: {len(speech_routes)}; "
        f"explicit unimplemented baseline: {len(report.known_unimplemented)}; "
        f"known undeclared-status baseline: {len(report.known_status_gaps)} operation(s)",
        "",
        "OPERATION                     METHOD PATH",
    ]
    for row in report.rows:
        operation = row.operation
        lines.append(f"{operation.operation_id:28} {operation.method:6} {operation.path}")
        if row.route is None:
            note = "UNIMPLEMENTED placeholder missing" if row.unimplemented else "NOT REGISTERED"
            lines.append(
                f"  declared={format_statuses(operation.declared_statuses)} observed=- [{note}]"
            )
            continue
        lines.append(
            f"  declared={format_statuses(operation.declared_statuses)} "
            f"observed={format_statuses(row.route.statuses)}"
            + (
                f" [unimplemented -> {UNIMPLEMENTED_OPERATION_STATUS} "
                f"{UNIMPLEMENTED_OPERATION_CODE}]"
                if row.unimplemented
                else ""
            )
        )
        lines.append(
            f"  undeclared={format_statuses(row.undeclared)} "
            f"known-baseline={format_statuses(row.known_undeclared)} "
            f"declared-not-observed={format_statuses(row.declared_not_observed)}"
        )
        if row.route.extraction_error is not None:
            lines.append(f"  status-extraction=FAILED ({row.route.extraction_error})")
        if FRAMEWORK_METHOD_NOT_ALLOWED in operation.declared_statuses:
            if row.envelope_error is not None:
                lines.append(f"  405-envelope=FAILED ({row.envelope_error})")
            elif not row.envelope_probed:
                lines.append("  405-envelope=not probed")
            else:
                lines.append("  405-envelope=verified")
    extras = [route for route in report.registered if route.operation_id not in contract_ids]
    if extras:
        lines.append("")
        lines.append("OTHER REGISTERED ROUTES")
        for route in extras:
            classification = (
                "other-contract"
                if route.operation_id is not None
                and route.operation_id in report.other_contract_ids
                else "UNDECLARED"
            )
            lines.append(
                f"  {route.method:6} {route.path} {route.operation_id or '-'} [{classification}]"
            )
    if report.undetermined:
        lines.append("")
        lines.append("UNDETERMINED (无法判定；只报告可能值，不猜造，不判失败)")
        for item in report.undetermined:
            possible = format_statuses(item.possible_statuses)
            suffix = f"; possible={possible}" if item.possible_statuses else ""
            lines.append(f"  {item.operation} @ {item.location}: {item.detail}{suffix}")
    lines.append("")
    if report.findings:
        lines.append(f"FAILURES ({len(report.findings)})")
        for finding in report.findings:
            lines.append(f"  - {finding.kind}: {finding.detail}")
    else:
        lines.append(
            "OK: every registered speech route matches the contract; "
            "known gaps are explicit and no new deviation was found."
        )
    return "\n".join(lines)


def report_payload(report: Report) -> dict[str, Any]:
    return {
        "contract": report.contract_path,
        "contract_version": report.contract_version,
        "declared_operations": len(report.contract_operations),
        "registered": [
            {
                "method": route.method,
                "path": route.path,
                "operationId": route.operation_id,
                "statuses": sorted(route.statuses),
                "extraction_error": route.extraction_error,
                "undetermined": [
                    {
                        "location": item.location,
                        "detail": item.detail,
                        "possible_statuses": list(item.possible_statuses),
                    }
                    for item in route.undetermined
                ],
            }
            for route in report.registered
        ],
        "rows": [
            {
                "operationId": row.operation.operation_id,
                "method": row.operation.method,
                "path": row.operation.path,
                "declared": sorted(row.operation.declared_statuses),
                "observed": sorted(row.route.statuses) if row.route else [],
                "undeclared": list(row.undeclared),
                "known_undeclared": list(row.known_undeclared),
                "declared_not_observed": list(row.declared_not_observed),
                "unimplemented": row.unimplemented,
                "envelope_probed": row.envelope_probed,
                "envelope_error": row.envelope_error,
            }
            for row in report.rows
        ],
        "known_unimplemented": list(report.known_unimplemented),
        "unimplemented_placeholders": [
            {"operationId": operation_id, "method": method, "path": path}
            for operation_id, method, path in report.unimplemented
        ],
        "known_status_gaps": [
            {"operationId": operation_id, "statuses": list(statuses)}
            for operation_id, statuses in report.known_status_gaps
        ],
        "undetermined": [
            {
                "operation": item.operation,
                "location": item.location,
                "detail": item.detail,
                "possible_statuses": list(item.possible_statuses),
            }
            for item in report.undetermined
        ],
        "findings": [
            {"kind": finding.kind, "operationId": finding.operation_id, "detail": finding.detail}
            for finding in report.findings
        ],
    }


def main(arguments: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT, help="repository root")
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    options = parser.parse_args(arguments)
    try:
        report = build_report(options.root)
    except Exception as error:
        print(f"route conformance check could not run: {error}", file=sys.stderr)
        return 2
    if options.json:
        print(json.dumps(report_payload(report), ensure_ascii=False, sort_keys=True))
    else:
        print(format_report(report))
    return 1 if report.findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
