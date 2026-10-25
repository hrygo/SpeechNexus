"""Audit workbench operations from contract through binding, consumer and tests.

The map is evidence of production wiring, not an acceptance result: a bound
operation still needs behavioural review for the original ACs. `--write`
re-derives the map and keeps existing named-dependency registrations; `--check`
fails when the committed map drifts from the contract or the code.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
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


def is_test_candidate(path: Path) -> bool:
    return path.suffix in TEST_SUFFIXES and (".test." in path.name or path.parts[0] == "tests")


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
    escaped_id = re.escape(operation["id"])
    escaped_path = re.escape(operation["path"])
    requested = rf"\.{operation['method'].upper()}\(\s*[\"']{escaped_path}[\"']"
    patterns = [rf"\b{escaped_id}\b", requested]
    if method:
        patterns.append(rf"\.{re.escape(method)}\(")
    return re.compile("|".join(patterns))


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
            if not path.is_file() or not is_test_candidate(path):
                continue
            if reference.search(path.read_text(errors="ignore")):
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
