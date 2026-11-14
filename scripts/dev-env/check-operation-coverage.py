"""Audit workbench operations from contract through binding, consumer and tests.

The map is evidence of production wiring, not an acceptance result: a bound
operation still needs behavioural review for the original ACs. `--write`
re-derives the map and keeps existing named-dependency registrations; `--check`
fails when the committed map drifts from the contract or the code.

Test evidence is read from both sides of the repository: the TypeScript suite below
`web/workbench/src` (which drives the client) and every test-suffixed file below
`tests/`, including the Python suite (which drives the routers and the generated
contract). Each language is matched on surfaces it really has -- see
:func:`test_pattern` and :func:`python_evidence`.
"""

from __future__ import annotations

import argparse
import ast
import functools
import hashlib
import io
import json
import re
import subprocess
import tokenize
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[2]
CONTRACT = Path("contracts/workbench-openapi.yaml")
BINDINGS = Path("contracts/bindings.json")
HTTP_TYPE = Path("packages/contracts/src/workbench-http.ts")
HTTP_SCHEMA = Path("packages/contracts/src/speechnexus_contracts/workbench-http.schema.json")
DEFAULT_MAP = Path("docs/audits/2026-10-26-team-c-operation-coverage.json")
CONSUMER_ROOTS = (Path("web/workbench/src"),)
TEST_ROOTS = (Path("web/workbench/src"), Path("tests"))
CONSUMER_SUFFIXES = (".ts", ".tsx")
TEST_SUFFIXES = (".ts", ".tsx", ".py", ".mjs")
# Python drives the generated contract and the routers instead of the hand-written client,
# so its evidence is a request, a binding call or an exact operationId handle. A client
# method name is not one of them: in Python the same word is an unrelated domain method
# (`transcript.revise()`, `snapshot.status()`), the false positive #788 removed when
# `getByRole("status")` counted for the client's `status()` (#792).
BINDING_FUNCTIONS = frozenset(
    {
        "validate_request",
        "validate_response",
        "validate_event",
        "validate_http_request",
        "validate_workbench_response",
        "iter_events",
        "assert_contract",
    }
)
# This file's operationIds are the gate's own fixture data, so crediting it as coverage
# of those operations is self-credit: the same strings would satisfy the gate whether or
# not a single request is ever served (#792).
GATE_SELF_TEST = Path("tests/unit/test_operation_coverage.py")
PENDING_KEYS = ("owner", "artifact", "unblock_condition", "impact")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def declared_operations(root: Path) -> list[dict[str, str]]:
    document = yaml.safe_load((root / CONTRACT).read_text())
    operations: list[dict[str, str]] = []
    for path, methods in document["paths"].items():
        for method, specification in methods.items():
            if method not in {"get", "post", "put", "delete", "patch"}:
                continue
            identifier = specification.get("operationId")
            if not isinstance(identifier, str) or not identifier:
                raise ValueError(f"{method.upper()} {path} has no operationId")
            operations.append({"id": identifier, "method": method, "path": path})
    identifiers = [item["id"] for item in operations]
    if len(identifiers) != len(set(identifiers)):
        raise ValueError("contract declares duplicate operationIds")
    return sorted(operations, key=lambda item: item["id"])


def is_consumer_candidate(path: Path) -> bool:
    name = path.name
    return (
        path.suffix in CONSUMER_SUFFIXES
        and ".test." not in name
        and not name.endswith(".generated.ts")
        and "fixtures" not in name
    )


def is_test_candidate(root: Path, path: Path) -> bool:
    """A test is a ``.test.`` asset or any test-suffixed file below ``root/tests``.

    ``path`` is absolute here because it comes from ``(root / base).rglob()``, so the
    scope has to be read from the path relative to ``root``: comparing ``parts[0]``
    against the literal ``tests`` never matched an absolute path, which kept every
    Python test below ``tests/`` out of the evidence set (#792).
    """
    if path.suffix not in TEST_SUFFIXES:
        return False
    resolved = path if path.is_absolute() else root / path
    try:
        relative = resolved.relative_to(root)
    except ValueError:
        return False
    if relative == GATE_SELF_TEST:
        return False
    if ".test." in path.name:
        return True
    return relative.parts[0] == "tests"


def client_methods(root: Path) -> dict[tuple[str, str], str]:
    """Map (METHOD, path) to the workbench client method that performs it."""
    text = (root / "web/workbench/src/manuscripts/workbench-client.ts").read_text()
    starts = [(match.start(), match.group(1)) for match in re.finditer(r"async (\w+)\(", text)]
    mapping: dict[tuple[str, str], str] = {}
    for index, (position, name) in enumerate(starts):
        end = starts[index + 1][0] if index + 1 < len(starts) else len(text)
        call = re.search(r"\.(GET|POST|PUT|PATCH|DELETE)\(\"([^\"]+)\"", text[position:end])
        if call:
            mapping[(call.group(1), call.group(2))] = name
    return mapping


def consumer_pattern(operation: dict[str, str]) -> re.Pattern[str]:
    escaped_id = re.escape(operation["id"])
    escaped_path = re.escape(operation["path"])
    validated = rf"validate(?:Request|Response|Event)?\(\s*[\"']{escaped_id}[\"']"
    requested = rf"\.{operation['method'].upper()}\(\s*[\"']{escaped_path}[\"']"
    return re.compile(f"{requested}|{validated}")


def test_pattern(operation: dict[str, str], method: str | None) -> re.Pattern[str]:
    """Match TypeScript evidence that reaches this operation's real request surface.

    Only four forms count: the contract ``operationId``, the exact method+path the
    client sends, a call to the client method that performs the request, or that
    method as a spy target bound to a real object (``vi.spyOn(client, "status")``).
    A bare quoted name is never evidence: ``getByRole("status")`` asserts an ARIA
    role that happens to share a word with the client method, and accepting it let
    any decorative string satisfy the coverage gate (#788).

    The client-method legs exist because the workbench client is the surface a
    TypeScript test drives; Python has no such client, so it is served by
    :func:`python_evidence` instead (#792).
    """
    escaped_id = re.escape(operation["id"])
    escaped_path = re.escape(operation["path"])
    requested = rf"\.{operation['method'].upper()}\(\s*[\"']{escaped_path}[\"']"
    patterns = [rf"\b{escaped_id}\b", requested]
    if method:
        escaped_method = re.escape(method)
        patterns.append(rf"\.{escaped_method}\(")
        # A spy must name the object whose member it replaces; the object binding is what
        # ties the method name to this operation instead of to an unrelated string.
        patterns.append(rf"\bspyOn\(\s*[^,()\n]+,\s*[\"']{escaped_method}[\"']\s*\)")
    return re.compile("|".join(patterns))


@functools.cache
def python_operation_handles(path: str) -> frozenset[str]:
    """String literals one Python file uses as handles, not as prose.

    A test may name an operation as a bare handle: ``"getBookExport"`` in a
    parametrize list, a routed-operation set driving real requests, or a dict key.
    That is what a handle looks like syntactically -- an argument, a collection
    element, a mapping key or an assigned value -- so the literal has to sit in one of
    those positions. Comments, docstrings and prose assertions are excluded, and so is
    a substring: ``example("getVoiceRevision-invalid-request")`` is a fixture key
    belonging to another operation, and ``\\b`` matched the operationId inside it.
    """
    text = Path(path).read_text(errors="ignore")
    handles: set[str] = set()
    previous: tokenize.TokenInfo | None = None
    ignored = {
        tokenize.NL,
        tokenize.NEWLINE,
        tokenize.INDENT,
        tokenize.DEDENT,
        tokenize.COMMENT,
        tokenize.ENCODING,
    }
    try:
        for token in tokenize.generate_tokens(io.StringIO(text).readline):
            if token.type in ignored:
                continue
            if token.type == tokenize.STRING and previous is not None:
                opens_a_value = (
                    previous.type == tokenize.OP
                    and previous.string
                    in {
                        "(",
                        "[",
                        "{",
                        ",",
                        ":",
                        "=",
                        "==",
                        "!=",
                    }
                ) or (previous.type == tokenize.NAME and previous.string in {"return", "yield"})
                if opens_a_value:
                    try:
                        value = ast.literal_eval(token.string)
                    except SyntaxError, ValueError:
                        value = None
                    if isinstance(value, str):
                        handles.add(value)
            previous = token
    except tokenize.TokenError, IndentationError, SyntaxError:
        for match in re.finditer(r"[({\[,]\s*\"([^\"\\\n]*)\"|[({\[,]\s*'([^'\\\n]*)'", text):
            handles.add(match.group(1) if match.group(1) is not None else match.group(2))
    return frozenset(handles)


@functools.cache
def request_paths(path: str, method: str) -> tuple[str, ...]:
    """The path literals one file hands to ``method``, f-strings masked.

    Python tests build URLs as f-strings (``f"/v1/workbench/books/{book['id']}"``), so
    a regular expression over the source cannot tell where the path ends: the quotes
    inside the interpolation would look like the closing quote and a sibling route
    (``.../{book['id']}/chapters``) would read as this one. Parsing the literal and
    replacing each interpolation with ``{}`` keeps the segments intact.
    """
    text = Path(path).read_text(errors="ignore")
    found: list[str] = []
    for call in re.finditer(rf"\.{method}\(\s*f?([\"'])", text):
        quote = call.group(1)
        index = call.end()
        literal: list[str] = []
        depth = 0
        while index < len(text):
            character = text[index]
            if character == "\\":
                literal.append(text[index : index + 2])
                index += 2
                continue
            if depth:
                if character == "{":
                    depth += 1
                elif character == "}":
                    depth -= 1
                    if depth == 0:
                        literal.append("{}")
                index += 1
                continue
            if character == "{":
                if text[index : index + 2] == "{{":
                    literal.append("{{")
                    index += 2
                    continue
                depth = 1
                index += 1
                continue
            if character == quote:
                break
            literal.append(character)
            index += 1
        found.append("".join(literal))
    return tuple(found)


def _segments(path: str) -> list[str]:
    trimmed = path.split("?", 1)[0].split("#", 1)[0].strip("/")
    return trimmed.split("/") if trimmed else []


def requests_route(operation: dict[str, str], path: Path) -> bool:
    """Whether the file issues a real request to this operation's own route.

    The route template and the request share their static segments and their length,
    so a request cannot stand in for a sibling route or for the other verb on the
    same path: ``POST /v1/workbench/projects`` is not the read, and
    ``/v1/workbench/exports/{export_id}/content`` is not the export read.
    """
    template = _segments(operation["path"])
    wildcards = [segment.startswith("{") and segment.endswith("}") for segment in template]
    for literal in request_paths(str(path), operation["method"]):
        actual = _segments(literal)
        if len(actual) != len(template):
            continue
        for declared, sent, wildcard in zip(template, actual, wildcards, strict=True):
            if wildcard:
                if not sent:
                    break
                continue
            if declared != sent:
                break
        else:
            return True
    return False


def python_tokens(path: str) -> list[tokenize.TokenInfo]:
    """The meaningful tokens of one Python file, comments and layout dropped."""
    text = Path(path).read_text(errors="ignore")
    layout = {tokenize.COMMENT, tokenize.NL, tokenize.NEWLINE, tokenize.ENCODING}
    return [
        token
        for token in tokenize.generate_tokens(io.StringIO(text).readline)
        if token.type not in layout
        and token.type != tokenize.INDENT
        and token.type != tokenize.DEDENT
    ]


@functools.cache
def python_binding_operations(path: str) -> frozenset[str]:
    """operationIds handed to a generated-contract binding call as its own argument.

    The call names the operation it validates (``validate_response("getBook", 200, …)``),
    which is the Python side of the client method: it is a real boundary, not a string.
    Arguments are read from the call's own parentheses, so a mention inside a comment or
    a docstring -- where the text is not even tokenised as a call -- cannot satisfy it.
    """
    try:
        tokens = python_tokens(path)
    except tokenize.TokenError, IndentationError, SyntaxError:
        return frozenset()
    found: set[str] = set()
    for index, token in enumerate(tokens):
        if token.type != tokenize.NAME or token.string not in BINDING_FUNCTIONS:
            continue
        if index + 1 >= len(tokens) or tokens[index + 1].string != "(":
            continue
        depth = 0
        for argument in tokens[index + 1 :]:
            value = argument.string
            if value == "(":
                depth += 1
            elif value == ")":
                depth -= 1
                if depth == 0:
                    break
            elif depth == 1 and argument.type == tokenize.STRING:
                try:
                    literal = ast.literal_eval(value)
                except SyntaxError, ValueError:
                    continue
                if isinstance(literal, str):
                    found.add(literal)
    return frozenset(found)


@functools.cache
def python_operation_id_assertions(path: str) -> frozenset[str]:
    """operationIds compared against a live document's ``operationId``.

    ``schema["paths"][path]["get"]["operationId"] == "getBook"`` asserts the route the
    application mounts carries this operation, which the string in a comment does not.
    """
    try:
        tokens = python_tokens(path)
    except tokenize.TokenError, IndentationError, SyntaxError:
        return frozenset()
    found: set[str] = set()
    for index, token in enumerate(tokens):
        if token.type != tokenize.STRING:
            continue
        try:
            if ast.literal_eval(token.string) != "operationId":
                continue
        except SyntaxError, ValueError:
            continue
        following = tokens[index + 1 : index + 5]
        if len(following) < 3:
            continue
        if following[0].string != "]" or following[1].string != "==":
            continue
        if following[2].type != tokenize.STRING:
            continue
        try:
            literal = ast.literal_eval(following[2].string)
        except SyntaxError, ValueError:
            continue
        if isinstance(literal, str):
            found.add(literal)
    return frozenset(found)


def python_evidence(operation: dict[str, str], path: Path) -> bool:
    """Whether one Python file really reaches this operation.

    Three surfaces count, all of them real: a request to the route the app serves, a
    generated-contract binding call that states the operation it validates, and an
    exact ``operationId`` handle (a live ``operationId`` assertion, a parametrised list
    or a routed-operation set the test drives). A mention in a comment, a docstring, a
    contract description or a longer fixture key is not evidence (#788, #792).
    """
    identifier = operation["id"]
    if identifier in python_binding_operations(str(path)):
        return True
    if identifier in python_operation_id_assertions(str(path)):
        return True
    if requests_route(operation, path):
        return True
    return identifier in python_operation_handles(str(path))


def scan(
    root: Path,
    operation: dict[str, str],
    methods: dict[tuple[str, str], str],
) -> tuple[list[str], list[str]]:
    """Return production consumer files and test files that reference one operation."""
    consumers: list[str] = []
    tests: list[str] = []
    consumer = consumer_pattern(operation)
    reference = test_pattern(
        operation, methods.get((operation["method"].upper(), operation["path"]))
    )
    for base in CONSUMER_ROOTS:
        for path in sorted((root / base).rglob("*")):
            if not path.is_file() or not is_consumer_candidate(path):
                continue
            if consumer.search(path.read_text(errors="ignore")):
                consumers.append(path.relative_to(root).as_posix())
    for base in TEST_ROOTS:
        for path in sorted((root / base).rglob("*")):
            if not path.is_file() or not is_test_candidate(root, path):
                continue
            if path.suffix == ".py":
                matched = python_evidence(operation, path)
            else:
                matched = bool(reference.search(path.read_text(errors="ignore")))
            if matched:
                tests.append(path.relative_to(root).as_posix())
    return sorted(set(consumers)), sorted(set(tests))


def binding_flags(root: Path, operation: dict[str, str]) -> dict[str, bool]:
    manifest = json.loads((root / BINDINGS).read_text())
    paths = manifest["http_surfaces"]["workbench"].get("paths", [])
    http_type = (root / HTTP_TYPE).read_text()
    http_schema = json.loads((root / HTTP_SCHEMA).read_text())
    available = http_schema.get("x-nexus-http-operations", {})
    identifier = operation["id"]
    return {
        "path_entry": operation["path"] in paths,
        "http_operation_union": f'"{identifier}"' in http_type,
        "python_http_schema": identifier in available,
    }


def derive(root: Path) -> dict[str, Any]:
    operations = []
    methods = client_methods(root)
    for operation in declared_operations(root):
        consumers, tests = scan(root, operation, methods)
        binding = binding_flags(root, operation)
        bound = all(binding.values()) and bool(consumers) and bool(tests)
        operations.append(
            {
                "id": operation["id"],
                "method": operation["method"],
                "path": operation["path"],
                "binding": binding,
                "consumers": consumers,
                "tests": tests,
                "status": (
                    "bound" if bound else "consumer_pending" if not consumers else "test_pending"
                ),
            }
        )
    return {
        "version": "1.0.0",
        "scope": (
            "workbench HTTP surface only; other surfaces and behaviour review are separate units"
        ),
        "contract": CONTRACT.as_posix(),
        "contract_sha256": sha256_bytes((root / CONTRACT).read_bytes()),
        # The commit the map was derived on. `--write` runs before the map is committed,
        # so this names the base the evidence was read from, never the commit that
        # carries the map; `differences()` therefore does not compare it (#792).
        "audited_head": subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip(),
        "operations": operations,
    }


def registration_state(entry: dict[str, Any]) -> bool:
    pending = entry.get("pending")
    return isinstance(pending, dict) and all(pending.get(key) for key in PENDING_KEYS)


def differences(committed: dict[str, Any], derived: dict[str, Any]) -> list[dict[str, str]]:
    """Compare a reviewed map with the freshly derived wiring."""
    findings: list[dict[str, str]] = []
    declared = {item["id"]: item for item in derived["operations"]}
    reviewed = {item["id"]: item for item in committed.get("operations", [])}
    for identifier in sorted(set(declared) - set(reviewed)):
        findings.append({"code": "operation_missing", "id": identifier, "detail": "declared"})
    for identifier in sorted(set(reviewed) - set(declared)):
        findings.append({"code": "operation_extra", "id": identifier, "detail": "not declared"})
    for identifier in sorted(set(declared) & set(reviewed)):
        expected, actual = declared[identifier], reviewed[identifier]
        if actual.get("method") != expected["method"] or actual.get("path") != expected["path"]:
            findings.append({"code": "operation_shape", "id": identifier, "detail": "method/path"})
        if actual.get("binding") != expected["binding"]:
            findings.append({"code": "binding_drift", "id": identifier, "detail": "binding flags"})
        for role in ("consumers", "tests"):
            expected_refs = expected[role]
            actual_refs = sorted(set(actual.get(role, [])))
            if expected_refs != actual_refs:
                findings.append(
                    {
                        "code": f"{role}_drift",
                        "id": identifier,
                        "detail": f"expected {expected_refs}",
                    }
                )
        if expected["status"] == "bound" and actual.get("status") != "bound":
            findings.append(
                {"code": "stale_registration", "id": identifier, "detail": "consumer exists"}
            )
        needs_registration = expected["status"] != "bound"
        if actual.get("status") == "bound" and needs_registration:
            findings.append(
                {"code": "consumer_missing", "id": identifier, "detail": "bound without consumer"}
            )
        if needs_registration and not registration_state(actual):
            findings.append(
                {
                    "code": "registration_incomplete",
                    "id": identifier,
                    "detail": "owner/artifact/unblock_condition/impact required",
                }
            )
    if committed.get("contract_sha256") != derived["contract_sha256"]:
        findings.append(
            {"code": "contract_drift", "id": "scope", "detail": "contract digest changed"}
        )
    return findings


def merge_registrations(
    derived: dict[str, Any], committed: dict[str, Any] | None
) -> dict[str, Any]:
    if not committed:
        return derived
    pending = {
        item["id"]: item["pending"]
        for item in committed.get("operations", [])
        if isinstance(item.get("pending"), dict)
    }
    for item in derived["operations"]:
        if item["status"] != "bound" and item["id"] in pending:
            item["pending"] = pending[item["id"]]
    return derived


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--map", type=Path, default=DEFAULT_MAP)
    parser.add_argument("--write", action="store_true", help="regenerate the reviewed map")
    args = parser.parse_args(argv)
    committed = json.loads(args.map.read_text()) if args.map.is_file() else None
    derived = merge_registrations(derive(ROOT), committed)
    if args.write:
        args.map.parent.mkdir(parents=True, exist_ok=True)
        args.map.write_text(json.dumps(derived, ensure_ascii=False, indent=2) + "\n")
        print(json.dumps({"written": str(args.map), "operations": len(derived["operations"])}))
        return 0
    if committed is None:
        parser.error(f"{args.map} is absent; run --write and review it")
    findings = differences(committed, derived)
    print(
        json.dumps(
            {
                "operations": len(derived["operations"]),
                "bound": sum(1 for item in derived["operations"] if item["status"] == "bound"),
                "pending": sum(1 for item in derived["operations"] if item["status"] != "bound"),
                "findings": findings,
            },
            ensure_ascii=False,
        )
    )
    return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
