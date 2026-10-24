"""Detect known structural changes to 18 frozen coordination request entrypoints.

These are the raw runtime parser inputs, not the mixed command/result root union.
This check does not establish complete schema inclusion or transactional semantics.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path
from types import ModuleType
from typing import Any

from jsonschema import Draft202012Validator, SchemaError

_ROOT = Path(__file__).resolve().parents[2]
_KERNEL = Path(__file__).with_name("check-openai-contract-compat.py")
SOURCE = "contracts/internal/coordination-transactions.schema.json"
BINDINGS = "contracts/bindings.json"
PORTS = (
    "acquire_owner",
    "renew_owner",
    "seal_barrier",
    "activate_owner",
    "admit_recipe",
    "create_job",
    "claim_job",
    "create_submit",
    "record_stop",
    "quarantine_lease",
    "release_lease",
    "append_interval",
    "create_runtime_intent",
    "observe_runtime",
    "query_outcome",
    "mark_execution_unknown",
    "record_execution_observation",
    "redact_execution_command",
)
UNVERIFIED = (
    "TransactionResult, response guarantees and unselected definitions",
    "Mixed root oneOf, command/result envelope and wire schema_version constraints",
    "Complete JSON Schema inclusion, other standalone JSON sources and WS/event ordering",
    "Atomicity, authorization, budget/stop facts and actual consumer/deployment qualification",
)


def _kernel() -> ModuleType:
    spec = importlib.util.spec_from_file_location("_coordination_request_kernel", _KERNEL)
    if spec is None or spec.loader is None:
        raise RuntimeError("Coordination comparison kernel unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    vars(module)["_ROOT"] = _ROOT
    return module


def _object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result


def _non_json_constant(value: str) -> Any:
    raise ValueError("Non-JSON numeric constant")


def _load(path: str, ref: str | None) -> Any:
    text = (
        subprocess.check_output(
            ["git", "-C", str(_ROOT), "show", ref + ":" + path],
            text=True,
            stderr=subprocess.PIPE,
        )
        if ref is not None
        else (_ROOT / path).read_text(encoding="utf-8")
    )
    return json.loads(text, object_pairs_hook=_object, parse_constant=_non_json_constant)


def _metadata(document: Any, bindings: Any) -> dict[str, str]:
    if not isinstance(document, Mapping) or not isinstance(bindings, Mapping):
        raise ValueError("Expected schema and binding objects")
    if (
        document.get("$schema") != "https://json-schema.org/draft/2020-12/schema"
        or document.get("$id") != "https://speechnexus.internal/" + SOURCE
        or not isinstance(document.get("$defs"), Mapping)
    ):
        raise ValueError("Frozen schema identity or definitions unavailable")
    Draft202012Validator.check_schema(document)
    binding = bindings.get("coordination")
    if not isinstance(binding, Mapping):
        raise ValueError("Coordination runtime profile unavailable")
    if any(binding.get(key, {}) != {} for key in ("schema_refs", "schema_unions")):
        # These generator features can make parse(operation, request) consume a
        # different schema while leaving its namesake $defs unchanged.
        raise ValueError("Remapped runtime entrypoints need an explicit comparison scope")
    version = document.get("x-speechnexus-schema-version")
    if (
        not isinstance(version, str)
        or not version.strip()
        or binding.get("version") != version
        or binding.get("source") != SOURCE.removeprefix("contracts/")
        or binding.get("definitions_prefix") != "/$defs/"
    ):
        raise ValueError("Schema and binding version/source disagree")
    entries = binding.get("schemas")
    if (
        not isinstance(entries, list)
        or len(entries) != len(PORTS) + 1
        or not all(isinstance(name, str) for name in entries)
        or set(entries) != {*PORTS, "TransactionResult"}
    ):
        raise ValueError("Frozen runtime entrypoint inventory changed")
    branches = document.get("oneOf")
    if not isinstance(branches, list):
        raise ValueError("Source operation inventory unavailable")
    operations = []
    for branch in branches:
        if isinstance(branch, Mapping) and isinstance(branch.get("properties"), Mapping):
            operation = branch["properties"].get("operation")
            if not isinstance(operation, Mapping) or not isinstance(operation.get("const"), str):
                raise ValueError("Source operation identity unavailable")
            operations.append(operation["const"])
    if len(operations) != len(PORTS) or set(operations) != set(PORTS):
        raise ValueError("Frozen source operation inventory changed")
    if not {*PORTS, "TransactionResult"} <= document["$defs"].keys():
        raise ValueError("Frozen runtime definition removed")
    return {"schema": version, "binding": str(binding["version"])}


def inspect_source(base: str) -> dict[str, Any]:
    resolved = subprocess.check_output(
        ["git", "-C", str(_ROOT), "rev-parse", "--verify", "--end-of-options", base + "^{commit}"],
        text=True,
        stderr=subprocess.PIPE,
    ).strip()
    kernel = _kernel()
    report: dict[str, Any] = {
        "baseline": resolved,
        "source": SOURCE,
        "scope": "known-coordination-request-entrypoint-structure",
        "entrypoints": list(PORTS),
        "versions": {"base": None, "current": None},
        "findings": [],
        "unverified": list(UNVERIFIED),
    }
    findings: list[str] = report["findings"]
    try:
        old, new = _load(SOURCE, resolved), _load(SOURCE, None)
        report["versions"]["base"] = _metadata(old, _load(BINDINGS, resolved))
        report["versions"]["current"] = _metadata(new, _load(BINDINGS, None))
    except (
        ValueError,
        TypeError,
        OSError,
        SchemaError,
        subprocess.CalledProcessError,
        RecursionError,
    ):
        findings.append(SOURCE + ": source/binding metadata unavailable or invalid; not compared")
        return report
    base_side = kernel._SchemaSide(old, SOURCE, resolved, {}, False)
    current_side = kernel._SchemaSide(new, SOURCE, None, {}, False)
    for operation in PORTS:
        pointer = SOURCE + ":$defs/" + operation
        try:
            old_request = kernel._dereference_schema(old["$defs"][operation], base_side)
            new_request = kernel._dereference_schema(new["$defs"][operation], current_side)
            # check_schema(document) does not follow repository references. An
            # equally invalid referenced keyword must not become an equality pass.
            Draft202012Validator.check_schema(old_request)
            Draft202012Validator.check_schema(new_request)
            kernel._compare_schema(
                old_request,
                new_request,
                pointer,
                findings,
                direction="request",
                base_side=base_side,
                current_side=current_side,
            )
        except ValueError, TypeError, OSError, SchemaError, RecursionError:
            findings.append(pointer + ": reference comparison failed; not proven")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", required=True, help="Git commit/ref of the frozen baseline")
    parser.add_argument("--format", choices=("text", "json"), default="text")
    args = parser.parse_args()
    try:
        report = inspect_source(args.base)
    except OSError, subprocess.CalledProcessError, RuntimeError:
        print(
            "Coordination request comparison failed: baseline or kernel unavailable",
            file=sys.stderr,
        )
        return 2
    if args.format == "json":
        print(json.dumps(report, sort_keys=True))
    else:
        print("Coordination request baseline: " + report["baseline"])
        print(SOURCE + ": " + json.dumps(report["versions"], sort_keys=True))
        print("Request entrypoints: " + ", ".join(report["entrypoints"]))
        for finding in report["findings"]:
            print("  - " + finding)
        print(
            "Known request structural checks: " + ("rejected" if report["findings"] else "passed")
        )
        print("Not a full protocol compatibility proof; see unverified scope:")
        for boundary in report["unverified"]:
            print("  - " + boundary)
    return 1 if report["findings"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
