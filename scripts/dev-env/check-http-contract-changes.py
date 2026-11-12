"""Check known HTTP structural changes; speech is an explicit additional surface.

This is not a proof of complete protocol compatibility. It adds no compatibility
promise for versioned internal APIs, event ordering, authorization or side effects.
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
from typing import Any, Literal

import yaml

_ROOT = Path(__file__).resolve().parents[2]
_KERNEL = Path(__file__).with_name("check-openai-contract-compat.py")
SOURCES = (
    "contracts/admin-openapi.yaml",
    "contracts/workbench-openapi.yaml",
    "contracts/internal/runtime-control.openapi.yaml",
    "contracts/internal/execution-control.openapi.yaml",
)
SPEECH_SOURCE = "contracts/speech-openapi.yaml"
UNVERIFIED = (
    "Complete JSON Schema semantic inclusion and standalone JSON contracts",
    "HTTP serialization settings, added statuses/media and security scheme evolution",
    "WS/event ordering, epoch, authorization and business effects",
    "Consumer upgrade, migration and actual deployment qualification",
)

# Literal allowlist for breaking operation removals explicitly approved by the
# product owner. This is not a relaxation of structural checks: only an exact
# source/path/method/version-pair/change-kind match is filtered, every other
# finding still rejects, and every entry must correspond to a real removal in
# this comparison or it rejects too. New entries must add matching negative
# cases and update the frozen assertion in
# tests/contract/test_approved_http_operation_removals.py.
_APPROVED_OPERATION_REMOVALS: dict[tuple[str, str, str, str, str, str], dict[str, str]] = {
    (
        "contracts/admin-openapi.yaml",
        "/v1/nexus/admin/profiles",
        "get",
        "0.1.0.dev6",
        "0.1.0.dev7",
        "operation-removed",
    ): {
        "issue": "#603",
        "reason": (
            "Unimplemented legacy declaration; the published profile lists v1/v2 "
            "already cover the capability."
        ),
    },
    (
        "contracts/admin-openapi.yaml",
        "/v1/nexus/admin/policies",
        "get",
        "0.1.0.dev7",
        "0.1.0.dev8",
        "operation-removed",
    ): {
        "issue": "#605",
        "reason": (
            "No policy table, revision history, or updated_at exists; the "
            "operation has no consumer and could only be implemented by "
            "fabricating state."
        ),
    },
    (
        "contracts/admin-openapi.yaml",
        "/v1/nexus/admin/policies/{policy_id}",
        "get",
        "0.1.0.dev7",
        "0.1.0.dev8",
        "operation-removed",
    ): {
        "issue": "#605",
        "reason": (
            "No policy table, revision history, or updated_at exists; the "
            "operation has no consumer and could only be implemented by "
            "fabricating state."
        ),
    },
    (
        "contracts/admin-openapi.yaml",
        "/v1/nexus/admin/policies/{policy_id}",
        "patch",
        "0.1.0.dev7",
        "0.1.0.dev8",
        "operation-removed",
    ): {
        "issue": "#605",
        "reason": (
            "Policy changes have no table, revision history, or updated_at to "
            "persist against; PATCH had no consumer and no durable landing "
            "place."
        ),
    },
}

# Exact, product-approved parameter removals. A removal is filtered only when
# the exact source/path/method/version pair/location/name finding exists; the
# entry never covers another parameter, operation or version pair.
_APPROVED_PARAMETER_REMOVALS: dict[tuple[str, str, str, str, str, str, str], dict[str, str]] = {
    (
        "contracts/admin-openapi.yaml",
        "/v1/nexus/audit-events",
        "get",
        "0.1.0.dev7",
        "0.1.0.dev8",
        "query",
        "workspace_id",
    ): {
        "issue": "#605",
        "reason": (
            "The audit table stores only model binding lifecycle events and has "
            "no workspace column; the declared filter could never match a row."
        ),
    },
}

# Exact, product-approved response-schema replacements. The entry names the
# raw baseline and current schema references, and is active only for its exact
# version pair. Every finding under that response is filtered only when the
# response headers and other metadata (apart from the description) are
# unchanged.
_APPROVED_RESPONSE_REPLACEMENTS: dict[tuple[str, str, str, str, str, str], dict[str, str]] = {
    (
        "contracts/admin-openapi.yaml",
        "/v1/nexus/admin/capacity-manifests/{manifest_id}",
        "get",
        "200",
        "0.1.0.dev7",
        "0.1.0.dev8",
    ): {
        "issue": "#605",
        "reason": (
            "The admin surface exposes only the recorded evidence identity and "
            "release-evidence locator; the document body stays authoritative in "
            "the release evidence chain."
        ),
        "from_schema": "./capacity-manifest.schema.json",
        "to_schema": "#/components/schemas/CapacityManifestRecord",
    },
    (
        "contracts/admin-openapi.yaml",
        "/v1/nexus/admin/qualification-reports/{report_id}",
        "get",
        "200",
        "0.1.0.dev7",
        "0.1.0.dev8",
    ): {
        "issue": "#605",
        "reason": (
            "The admin surface exposes only the recorded evidence identity and "
            "release-evidence locator; the document body stays authoritative in "
            "the release evidence chain."
        ),
        "from_schema": "./qualification-report.schema.json",
        "to_schema": "#/components/schemas/QualificationReportRecord",
    },
    (
        "contracts/admin-openapi.yaml",
        "/v1/nexus/admin/model-instances",
        "get",
        "200",
        "0.1.0.dev8",
        "0.1.0.dev9",
    ): {
        "issue": "#605",
        "reason": (
            "The recorded instance projection no longer requires facts the runtime "
            "instance store does not hold (loaded artifact digests, process start and "
            "heartbeat times, and the instance stop document's evidence kind and "
            "digest); the response reference is unchanged on both sides."
        ),
        "from_schema": "#/components/schemas/ModelInstanceList",
        "to_schema": "#/components/schemas/ModelInstanceList",
    },
    (
        "contracts/admin-openapi.yaml",
        "/v1/nexus/admin/model-instances/{instance_id}/generations/{generation}",
        "get",
        "200",
        "0.1.0.dev8",
        "0.1.0.dev9",
    ): {
        "issue": "#605",
        "reason": (
            "The recorded instance projection no longer requires facts the runtime "
            "instance store does not hold (loaded artifact digests, process start and "
            "heartbeat times, and the instance stop document's evidence kind and "
            "digest); the single read now names the same document through the declared "
            "component ModelRuntimeInstance."
        ),
        "from_schema": "./model-instance.schema.json",
        "to_schema": "#/components/schemas/ModelRuntimeInstance",
    },
}


def _kernel() -> ModuleType:
    spec = importlib.util.spec_from_file_location("_http_contract_change_kernel", _KERNEL)
    if spec is None or spec.loader is None:
        raise RuntimeError("HTTP comparison kernel unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    vars(module)["_ROOT"] = _ROOT
    return module


def _version(document: Any) -> str:
    if (
        not isinstance(document, Mapping)
        or document.get("openapi") != "3.1.0"
        or not isinstance(document.get("paths"), Mapping)
        or not isinstance(document.get("info"), Mapping)
        or not isinstance(document["info"].get("version"), str)
        or not document["info"]["version"]
    ):
        raise ValueError("Expected a versioned OpenAPI 3.1.0 HTTP document")
    return str(document["info"]["version"])


def _path_operations(item: object) -> set[str]:
    methods = {"get", "post", "put", "patch", "delete", "head", "options", "trace"}
    return {method for method in item if method in methods} if isinstance(item, Mapping) else set()


def _apply_approved_operation_removals(
    source: str,
    base: Mapping[str, Any],
    current: Mapping[str, Any],
    base_version: str,
    current_version: str,
    findings: list[str],
) -> tuple[list[str], list[str]]:
    remaining = list(findings)
    approved: list[str] = []
    base_paths = base.get("paths")
    current_paths = current.get("paths")
    base_paths = base_paths if isinstance(base_paths, Mapping) else {}
    current_paths = current_paths if isinstance(current_paths, Mapping) else {}
    active = {
        (path, method): metadata
        for (
            entry_source,
            path,
            method,
            from_version,
            to_version,
            change,
        ), metadata in _APPROVED_OPERATION_REMOVALS.items()
        if entry_source == source
        and (from_version, to_version) == (base_version, current_version)
        and change == "operation-removed"
        and metadata.get("issue")
        and metadata.get("reason")
    }
    consumed: set[tuple[str, str]] = set()
    # A removed path is approved only when every operation it declared is
    # individually registered for this exact version pair. The path-level
    # finding is then replaced by one approval per registered operation.
    for path, base_item in base_paths.items():
        if path in current_paths:
            continue
        declared = _path_operations(base_item)
        if not declared or any((path, method) not in active for method in declared):
            continue
        expected = f"{source}:paths/{path}: path removed"
        if expected not in remaining:
            continue
        remaining.remove(expected)
        for method in sorted(declared):
            metadata = active[(path, method)]
            approved.append(
                f"{source}:paths/{path}/{method}: operation removed "
                f"[{metadata['issue']}: {metadata['reason']}]"
            )
            consumed.add((path, method))
    for key, metadata in _APPROVED_OPERATION_REMOVALS.items():
        entry_source, path, method, from_version, to_version, change = key
        if entry_source != source:
            continue
        pointer = f"{source}:paths/{path}/{method}"
        problem: str | None = None
        if change != "operation-removed":
            problem = f"unsupported change kind {change!r}"
        elif (base_version, current_version) != (from_version, to_version):
            # Entries are active only for their exact version pair. A different
            # pair leaves any removal unapproved instead of treating the entry as
            # a blanket permission; it also keeps synthetic fixtures isolated.
            continue
        elif not metadata.get("issue") or not metadata.get("reason"):
            problem = "entry must include a non-empty issue and reason"
        elif (path, method) in consumed:
            continue
        else:
            base_item = base_paths.get(path)
            current_item = current_paths.get(path)
            base_methods = base_item if isinstance(base_item, Mapping) else {}
            current_methods = current_item if isinstance(current_item, Mapping) else {}
            if method not in base_methods:
                problem = "baseline does not declare this operation"
            elif path not in current_paths:
                problem = (
                    "the whole path was removed; every operation must be individually registered"
                )
            elif method in current_methods:
                problem = "current contract still declares this operation"
            else:
                expected = f"{pointer}: operation removed"
                if expected not in remaining:
                    problem = f"exact finding {expected!r} is absent"
                else:
                    remaining.remove(expected)
                    approved.append(f"{expected} [{metadata['issue']}: {metadata['reason']}]")
                    continue
        remaining.append(f"{pointer}: approved operation removal entry does not match: {problem}")
    return remaining, approved


def _operation_parameters(
    document: Mapping[str, Any], path: str, method: str
) -> dict[tuple[str, str], Mapping[str, Any]]:
    def resolved(raw: object) -> Mapping[str, Any] | None:
        node = raw
        while isinstance(node, Mapping) and isinstance(node.get("$ref"), str):
            reference = node["$ref"]
            if not reference.startswith("#/"):
                return None
            target: object = document
            for token in reference.removeprefix("#/").split("/"):
                target = target.get(token) if isinstance(target, Mapping) else None
            node = target
        return node if isinstance(node, Mapping) else None

    paths = document.get("paths")
    item = paths.get(path) if isinstance(paths, Mapping) else None
    operation = item.get(method) if isinstance(item, Mapping) else None
    result: dict[tuple[str, str], Mapping[str, Any]] = {}
    for container in (item, operation):
        if not isinstance(container, Mapping):
            continue
        for raw in container.get("parameters", []) or []:
            parameter = resolved(raw)
            if parameter is None:
                continue
            name, location = parameter.get("name"), parameter.get("in")
            if isinstance(name, str) and isinstance(location, str):
                result[(name, location)] = parameter
    return result


def _apply_approved_parameter_removals(
    source: str,
    base: Mapping[str, Any],
    current: Mapping[str, Any],
    base_version: str,
    current_version: str,
    findings: list[str],
) -> tuple[list[str], list[str]]:
    remaining = list(findings)
    approved: list[str] = []
    for key, metadata in _APPROVED_PARAMETER_REMOVALS.items():
        entry_source, path, method, from_version, to_version, location, name = key
        if entry_source != source:
            continue
        pointer = f"{source}:paths/{path}/{method}"
        if (base_version, current_version) != (from_version, to_version):
            continue
        expected = f"{pointer}: parameter removed: {location} {name}"
        problem: str | None = None
        if not metadata.get("issue") or not metadata.get("reason"):
            problem = "entry must include a non-empty issue and reason"
        elif (name, location) not in _operation_parameters(base, path, method):
            problem = "baseline does not declare this parameter"
        elif (name, location) in _operation_parameters(current, path, method):
            problem = "current contract still declares this parameter"
        elif expected not in remaining:
            problem = f"exact finding {expected!r} is absent"
        else:
            remaining.remove(expected)
            approved.append(f"{expected} [{metadata['issue']}: {metadata['reason']}]")
            continue
        remaining.append(f"{pointer}: approved parameter removal entry does not match: {problem}")
    return remaining, approved


def _response_schema(
    document: Mapping[str, Any], path: str, method: str, status: str
) -> tuple[Mapping[str, Any] | None, Mapping[str, Any] | None]:
    paths = document.get("paths")
    item = paths.get(path) if isinstance(paths, Mapping) else None
    operation = item.get(method) if isinstance(item, Mapping) else None
    responses = operation.get("responses") if isinstance(operation, Mapping) else None
    response = responses.get(status) if isinstance(responses, Mapping) else None
    if not isinstance(response, Mapping):
        return None, None
    content = response.get("content")
    media = content.get("application/json") if isinstance(content, Mapping) else None
    schema = media.get("schema") if isinstance(media, Mapping) else None
    return response, schema if isinstance(schema, Mapping) else None


def _apply_approved_response_replacements(
    source: str,
    base: Mapping[str, Any],
    current: Mapping[str, Any],
    base_version: str,
    current_version: str,
    findings: list[str],
) -> tuple[list[str], list[str]]:
    remaining = list(findings)
    approved: list[str] = []
    for key, metadata in _APPROVED_RESPONSE_REPLACEMENTS.items():
        entry_source, path, method, status, from_version, to_version = key
        if entry_source != source:
            continue
        pointer = f"{source}:paths/{path}/{method}/responses/{status}"
        if (base_version, current_version) != (from_version, to_version):
            continue
        problem: str | None = None
        required = ("issue", "reason", "from_schema", "to_schema")
        if not all(metadata.get(field) for field in required):
            problem = "entry must include a non-empty issue, reason, from_schema and to_schema"
        else:
            base_response, base_schema = _response_schema(base, path, method, status)
            current_response, current_schema = _response_schema(current, path, method, status)
            if base_response is None or base_schema is None:
                problem = "baseline does not declare this response schema"
            elif current_response is None or current_schema is None:
                problem = "current contract does not declare this response schema"
            elif base_schema.get("$ref") != metadata["from_schema"]:
                problem = f"baseline schema reference is not {metadata['from_schema']!r}"
            elif current_schema.get("$ref") != metadata["to_schema"]:
                problem = f"current schema reference is not {metadata['to_schema']!r}"
            elif {
                field: value
                for field, value in base_response.items()
                if field not in {"content", "description"}
            } != {
                field: value
                for field, value in current_response.items()
                if field not in {"content", "description"}
            }:
                problem = "response metadata other than the schema or description changed"
            else:
                matching = [finding for finding in remaining if finding.startswith(pointer)]
                if not matching:
                    problem = f"no response findings start with {pointer!r}"
                else:
                    remaining = [
                        finding for finding in remaining if not finding.startswith(pointer)
                    ]
                    approved.append(
                        f"{pointer}: response schema replaced "
                        f"{metadata['from_schema']} -> {metadata['to_schema']} "
                        f"[{metadata['issue']}: {metadata['reason']}]"
                    )
                    continue
        remaining.append(
            f"{pointer}: approved response replacement entry does not match: {problem}"
        )
    return remaining, approved


def inspect_sources(
    base: str, *, surface: Literal["nonstandard", "speech"] = "nonstandard"
) -> dict[str, Any]:
    if surface not in {"nonstandard", "speech"}:
        raise ValueError("Unknown HTTP comparison surface")
    # Resolve once: every external reference on the baseline side uses the same
    # immutable commit, even if a moving branch changes during the comparison.
    resolved = subprocess.check_output(
        ["git", "-C", str(_ROOT), "rev-parse", "--verify", "--end-of-options", base + "^{commit}"],
        text=True,
        stderr=subprocess.PIPE,
    ).strip()
    baseline_paths = set(
        subprocess.check_output(
            ["git", "-C", str(_ROOT), "ls-tree", "-r", "--name-only", resolved, "--", "contracts"],
            text=True,
        ).splitlines()
    )
    compare = _kernel()._compare_openapi
    rows: list[dict[str, Any]] = []
    findings: list[str] = []
    approved_removals: list[str] = []
    approved_parameters: list[str] = []
    approved_replacements: list[str] = []
    compared_sources: set[str] = set()
    selected = (SPEECH_SOURCE,) if surface == "speech" else SOURCES
    for path in selected:
        row: dict[str, Any] = {"path": path, "base_version": None, "current_version": None}
        rows.append(row)
        if path not in baseline_paths:
            findings.append(f"{path}: frozen baseline source missing; not compared")
            continue
        if not (_ROOT / path).is_file():
            findings.append(f"{path}: current source removed or unavailable")
            continue
        try:
            old = yaml.safe_load(
                subprocess.check_output(
                    ["git", "-C", str(_ROOT), "show", resolved + ":" + path], text=True
                )
            )
            new = yaml.safe_load((_ROOT / path).read_text(encoding="utf-8"))
            row["base_version"], row["current_version"] = _version(old), _version(new)
            row["paths"] = len(new["paths"])
            detected = list(compare(old, new, path, base_git_ref=resolved, standard_only=False))
            detected, approved = _apply_approved_operation_removals(
                path,
                old,
                new,
                row["base_version"],
                row["current_version"],
                detected,
            )
            detected, approved_parameter = _apply_approved_parameter_removals(
                path,
                old,
                new,
                row["base_version"],
                row["current_version"],
                detected,
            )
            detected, approved_replacement = _apply_approved_response_replacements(
                path,
                old,
                new,
                row["base_version"],
                row["current_version"],
                detected,
            )
            findings.extend(detected)
            approved_removals.extend(approved)
            approved_parameters.extend(approved_parameter)
            approved_replacements.extend(approved_replacement)
            row["findings"] = len(detected)
            compared_sources.add(path)
        except ValueError, OSError, yaml.YAMLError, subprocess.CalledProcessError, RecursionError:
            # Broken sources/refs or an unbounded schema cannot turn into a pass.
            findings.append(f"{path}: source or reference comparison failed; not proven")
    selected_approval_sources = (
        {key[0] for key in _APPROVED_OPERATION_REMOVALS}
        | {key[0] for key in _APPROVED_PARAMETER_REMOVALS}
        | {key[0] for key in _APPROVED_RESPONSE_REPLACEMENTS}
    ) & set(selected)
    for source in sorted(selected_approval_sources - compared_sources):
        findings.append(f"{source}: approved change entries could not be verified")
    return {
        "baseline": resolved,
        "scope": "known-speech-http-structure"
        if surface == "speech"
        else "known-nonstandard-http-structure",
        "sources": rows,
        "findings": findings,
        "approved_removals": approved_removals,
        "approved_parameter_removals": approved_parameters,
        "approved_response_replacements": approved_replacements,
        "unverified": list(UNVERIFIED),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", required=True, help="Git commit/ref of the frozen baseline")
    parser.add_argument("--format", choices=("text", "json"), default="text")
    parser.add_argument("--surface", choices=("nonstandard", "speech"), default="nonstandard")
    args = parser.parse_args()
    try:
        report = inspect_sources(args.base, surface=args.surface)
    except OSError, subprocess.CalledProcessError, RuntimeError:
        print("HTTP structural comparison failed: baseline or kernel unavailable", file=sys.stderr)
        return 2
    if args.format == "json":
        print(json.dumps(report, sort_keys=True))
    else:
        if args.surface == "speech":
            print("HTTP comparison surface: speech (all paths, full fields)")
        print("HTTP structural baseline: " + report["baseline"])
        for row in report["sources"]:
            print(f"{row['path']}: {row['base_version']} -> {row['current_version']}")
        for finding in report["findings"]:
            print("  - " + finding)
        for approval in report["approved_removals"]:
            print("  approved: " + approval)
        for approval in report["approved_parameter_removals"]:
            print("  approved parameter removal: " + approval)
        for approval in report["approved_response_replacements"]:
            print("  approved response replacement: " + approval)
        print("Known HTTP structural checks: " + ("rejected" if report["findings"] else "passed"))
        print("Not a full protocol compatibility proof; see unverified scope:")
        for boundary in report["unverified"]:
            print("  - " + boundary)
    return 1 if report["findings"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
