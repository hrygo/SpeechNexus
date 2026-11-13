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
from typing import Any, Literal, cast

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
        "/v1/nexus/admin/capacity-manifests/{manifest_id}",
        "get",
        "200",
        "0.1.0.dev9",
        "0.1.0.dev10",
    ): {
        "issue": "#605",
        "reason": (
            "The recorded evidence reference keeps only facts the publication "
            "evidence table stores: the identifier, the digest when a publication "
            "row recorded one, and the earliest recording time. No column stores "
            "a release-evidence locator and the digest column is nullable, so both "
            "fields are optional; the response reference is unchanged on both "
            "sides."
        ),
        "from_schema": "#/components/schemas/CapacityManifestRecord",
        "to_schema": "#/components/schemas/CapacityManifestRecord",
    },
    (
        "contracts/admin-openapi.yaml",
        "/v1/nexus/admin/qualification-reports/{report_id}",
        "get",
        "200",
        "0.1.0.dev9",
        "0.1.0.dev10",
    ): {
        "issue": "#605",
        "reason": (
            "The recorded evidence reference keeps only facts the publication "
            "evidence table stores: the identifier, the digest when a publication "
            "row recorded one, and the earliest recording time. No column stores "
            "a release-evidence locator and the digest column is nullable, so both "
            "fields are optional; the response reference is unchanged on both "
            "sides."
        ),
        "from_schema": "#/components/schemas/QualificationReportRecord",
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


# Bound keywords that can be tightened, and the numeric direction that makes a
# change a narrowing. A bound edit is only "narrowing" when the admitted value
# set shrinks: an upper bound moves down, a lower bound moves up.
_BOUND_DIRECTIONS: dict[str, Literal["down", "up"]] = {
    "maxLength": "down",
    "maxItems": "down",
    "maxProperties": "down",
    "maximum": "down",
    "exclusiveMaximum": "down",
    "minLength": "up",
    "minItems": "up",
    "minProperties": "up",
    "minContains": "up",
    "maxContains": "down",
    "minimum": "up",
    "exclusiveMinimum": "up",
}

# Exact, product-approved request-bound narrowings. This class exists because a
# narrowing is a real structural change that the closed-tagged-object proof
# cannot express: it removes declared capability without removing a declaration.
# An entry is filtered only when every one of these holds, and every other case
# still rejects:
#
# - the exact source/path/method/version pair matches, so the entry never covers
#   another operation or a later version period;
# - the named component is reachable from that operation's request body, so the
#   entry cannot be satisfied by an unrelated schema that happens to carry the
#   same property;
# - the declared pointer and property name agree and name one bound keyword;
# - the numeric direction really is a narrowing ("max*" down, "min*" up), so a
#   relaxation, a type change or a structural change is never approved here;
# - the service schema differs from the baseline by exactly that one bound, so
#   the entry cannot hide a second simultaneous request change;
# - the entry carries the capability proof: why no admitted request is lost.
#   A registration without a proof is a defect, not an approval.
#
# A narrowing that already landed keeps the entry dormant (baseline and current
# both carry the new bound) instead of reporting it as unbacked. New entries must
# add matching negative cases and update the frozen assertion in
# tests/contract/test_approved_http_request_bound_narrowings.py.
_APPROVED_REQUEST_BOUND_NARROWINGS: dict[
    tuple[str, str, str, str, str, str, str, str, str, int | float, int | float],
    dict[str, str],
] = {
    (
        "contracts/speech-openapi.yaml",
        "/v1/nexus/jobs",
        "post",
        "0.1.0.dev18",
        "0.1.0.dev18",
        "TtsJobInput",
        "oneOf/0/properties/text",
        "text",
        "maxLength",
        200000,
        4096,
    ): {
        "issue": "#690",
        "reason": (
            "One async tts Job performs exactly one synthesis, so its inline text "
            "bound must equal the synchronous input bound and the enforcing domain "
            "and gateway limits; 200000 described text the service never accepts."
        ),
        "proof": (
            "Production admission assembly, real HTTP: 4096 characters created a Job "
            "(202) and 4097 characters were already refused (400, domain "
            "MAX_SPEECH_TEXT_CHARS=4096), so no text above 4096 was ever usable and "
            "the narrowing removes only unachievable promises. 4096 code points also "
            "stay within the 16384-byte gateway budget, so the character bound is the "
            "binding constraint."
        ),
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


# Annotation-only members carry no constraint, so recording them as structure
# would reject a narrowing for documenting itself. The response-replacement
# approval already ignores `description` on the same reasoning.
_NON_STRUCTURAL_KEYS = frozenset({"description"})


def _is_number(value: object) -> bool:
    return type(value) in (int, float)


def _bound_movement(keyword: str, base: object, current: object) -> str | None:
    """Classify a bound edit as a narrowing or a relaxation; None if neither."""

    direction = _BOUND_DIRECTIONS.get(keyword)
    if direction is None or not _is_number(base) or not _is_number(current):
        return None
    left = cast(int | float, base)
    right = cast(int | float, current)
    if left == right:
        return None
    narrowed = right < left if direction == "down" else right > left
    return "narrowing" if narrowed else "relaxation"


def _schema_components(document: Mapping[str, Any]) -> Mapping[str, Any]:
    components = document.get("components")
    schemas = components.get("schemas") if isinstance(components, Mapping) else None
    return schemas if isinstance(schemas, Mapping) else {}


def _request_body(document: Mapping[str, Any], path: str, method: str) -> Mapping[str, Any] | None:
    paths = document.get("paths")
    item = paths.get(path) if isinstance(paths, Mapping) else None
    operation = item.get(method) if isinstance(item, Mapping) else None
    body = operation.get("requestBody") if isinstance(operation, Mapping) else None
    return body if isinstance(body, Mapping) else None


def _reachable_schemas(document: Mapping[str, Any], node: object) -> set[str]:
    """Component schema names reachable from a node through local references."""

    schemas = _schema_components(document)
    names: set[str] = set()
    pending = [node]
    while pending:
        value = pending.pop()
        if isinstance(value, Mapping):
            reference = value.get("$ref")
            if isinstance(reference, str) and reference.startswith("#/components/schemas/"):
                name = reference.rsplit("/", 1)[-1]
                if name not in names:
                    names.add(name)
                    target = schemas.get(name)
                    if isinstance(target, Mapping):
                        pending.append(target)
            pending.extend(value.values())
        elif isinstance(value, list):
            pending.extend(value)
    return names


def _bound_surface(
    node: object,
    document: Mapping[str, Any],
    *,
    pointer: str = "",
    trail: tuple[str, ...] = (),
) -> tuple[object, dict[str, dict[str, object]]]:
    """Split a subtree into its bound-free shape and its numeric bound values.

    Component references are expanded so two versions compare by meaning rather
    than by reference name; the expansion is labelled, so a rename still shows up
    as a structural difference. Bound keywords are removed from the shape and
    recorded per pointer, which lets a registration prove that one bound is the
    only difference between the baseline and current service schema.
    """

    if isinstance(node, Mapping):
        reference = node.get("$ref")
        if isinstance(reference, str) and reference.startswith("#/components/schemas/"):
            name = reference.rsplit("/", 1)[-1]
            target = _schema_components(document).get(name)
            if not isinstance(target, Mapping):
                return {"$unresolved": name}, {}
            if name in trail:
                return {"$cycle": name}, {}
            nested_pointer = f"{pointer}/$ref({name})" if pointer else f"$ref({name})"
            body, bounds = _bound_surface(
                target, document, pointer=nested_pointer, trail=(*trail, name)
            )
            shape: dict[str, object] = {"$ref": name, "$body": body}
            merged = dict(bounds)
            for key, value in node.items():
                if key == "$ref":
                    continue
                child_pointer = f"{pointer}/{key}" if pointer else key
                shape[key], child_bounds = _bound_surface(
                    value, document, pointer=child_pointer, trail=trail
                )
                merged.update(child_bounds)
            return shape, merged
        shape = {}
        bounds = {}
        for key, value in node.items():
            if key in _NON_STRUCTURAL_KEYS:
                continue
            if key in _BOUND_DIRECTIONS and _is_number(value):
                bounds.setdefault(pointer, {})[key] = value
                continue
            child_pointer = f"{pointer}/{key}" if pointer else key
            shape[key], child_bounds = _bound_surface(
                value, document, pointer=child_pointer, trail=trail
            )
            bounds.update(child_bounds)
        return shape, bounds
    if isinstance(node, list):
        shape_items = []
        bounds = {}
        for index, item in enumerate(node):
            child_pointer = f"{pointer}/{index}" if pointer else str(index)
            child_shape, child_bounds = _bound_surface(
                item, document, pointer=child_pointer, trail=trail
            )
            shape_items.append(child_shape)
            bounds.update(child_bounds)
        return shape_items, bounds
    return node, {}


def _bound_differences(
    base: dict[str, dict[str, object]], current: dict[str, dict[str, object]]
) -> dict[tuple[str, str], tuple[object, object]]:
    differences: dict[tuple[str, str], tuple[object, object]] = {}
    for pointer in {*base, *current}:
        base_keywords = base.get(pointer, {})
        current_keywords = current.get(pointer, {})
        for keyword in {*base_keywords, *current_keywords}:
            before = base_keywords.get(keyword)
            after = current_keywords.get(keyword)
            if before != after:
                differences[(pointer, keyword)] = (before, after)
    return differences


def _request_body_surface(
    document: Mapping[str, Any], path: str, method: str
) -> tuple[object, dict[str, dict[str, object]]] | None:
    body = _request_body(document, path, method)
    return None if body is None else _bound_surface(body, document)


def _service_schema(
    document: Mapping[str, Any], component: str
) -> tuple[object, dict[str, dict[str, object]]] | None:
    target = _schema_components(document).get(component)
    if not isinstance(target, Mapping):
        return None
    return _bound_surface(target, document)


def _apply_approved_request_bound_narrowings(
    source: str,
    base: Mapping[str, Any],
    current: Mapping[str, Any],
    base_version: str,
    current_version: str,
    findings: list[str],
) -> tuple[list[str], list[str]]:
    remaining = list(findings)
    approved: list[str] = []
    for key, metadata in _APPROVED_REQUEST_BOUND_NARROWINGS.items():
        (
            entry_source,
            path,
            method,
            from_version,
            to_version,
            component,
            pointer,
            field,
            keyword,
            from_bound,
            to_bound,
        ) = key
        if entry_source != source:
            continue
        pointer_label = f"{source}:paths/{path}/{method}/requestBody"
        if (base_version, current_version) != (from_version, to_version):
            continue
        expected = (
            f"{pointer_label}/application/json/oneOf: "
            "exclusive variants changed; compatibility needs explicit proof"
        )
        problem: str | None = None
        if not all(metadata.get(name) for name in ("issue", "reason", "proof")):
            problem = "entry must include a non-empty issue, reason and proof"
        elif not pointer.endswith(f"properties/{field}"):
            problem = f"declared pointer does not end with properties/{field}"
        elif _bound_movement(keyword, from_bound, to_bound) != "narrowing":
            problem = (
                f"{keyword} {from_bound} -> {to_bound} is not a narrowing; "
                "only a shrinking admitted set can be registered"
            )
        elif component not in _reachable_schemas(
            base, _request_body(base, path, method)
        ) or component not in _reachable_schemas(current, _request_body(current, path, method)):
            problem = f"{component} is not reachable from this operation request body"
        else:
            # The kernel emits one finding for the whole request body, because it
            # inlines every exclusive variant before comparing. The registration
            # therefore proves the whole body, not just its own component: any
            # change anywhere else in the same body keeps the finding, otherwise
            # the entry would downgrade the gate from "this bound moved" to "this
            # operation moved and one of its bounds happens to be registered".
            base_surface = _request_body_surface(base, path, method)
            current_surface = _request_body_surface(current, path, method)
            base_schema = _service_schema(base, component)
            current_schema = _service_schema(current, component)
            if base_surface is None or current_surface is None:
                problem = "operation does not declare a request body"
            elif base_schema is None or current_schema is None:
                problem = f"component {component} is missing on one side"
            else:
                base_shape, base_bounds = base_surface
                current_shape, current_bounds = current_surface
                differences = _bound_differences(base_bounds, current_bounds)
                shape_same = _kernel()._same_json_tree(base_shape, current_shape)
                before = base_schema[1].get(pointer, {}).get(keyword)
                after = current_schema[1].get(pointer, {}).get(keyword)
                if (before, after) == (to_bound, to_bound):
                    # The narrowing already landed: the entry stays dormant until
                    # its version period changes, so it does not report a backlog
                    # item once the change is the new baseline, and it never adds
                    # a finding of its own. A change beside a dormant entry keeps
                    # the kernel's own finding, which the entry does not touch.
                    continue
                if not shape_same:
                    problem = "another structural change exists in the same operation request body"
                elif len(differences) != 1:
                    problem = (
                        f"the operation request body has {len(differences)} bound "
                        "differences; exactly the registered one is required — register "
                        "one narrowing per operation request body per change, or split "
                        "the change so each narrowing lands on its own"
                    )
                else:
                    (only_pointer, only_keyword), (before_moved, after_moved) = next(
                        iter(differences.items())
                    )
                    marker = f"$ref({component})"
                    if (
                        only_keyword != keyword
                        or marker not in only_pointer
                        or not only_pointer.endswith(pointer)
                    ):
                        problem = "the only bound difference is not the registered field"
                    elif (before_moved, after_moved) != (from_bound, to_bound):
                        problem = (
                            f"declared {keyword} {from_bound} -> {to_bound} does not match "
                            f"the documents {before_moved} -> {after_moved}"
                        )
                    elif expected not in remaining:
                        problem = f"exact finding {expected!r} is absent"
                    else:
                        remaining.remove(expected)
                        approved.append(
                            f"{pointer_label}/{component}/{pointer}: request bound narrowed "
                            f"{keyword} {from_bound} -> {to_bound} "
                            f"[{metadata['issue']}: {metadata['reason']}]"
                        )
                        continue
        remaining.append(
            f"{pointer_label}: approved request bound narrowing entry does not match: {problem}"
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
    approved_narrowings: list[str] = []
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
            detected, approved_narrowing = _apply_approved_request_bound_narrowings(
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
            approved_narrowings.extend(approved_narrowing)
            row["findings"] = len(detected)
            compared_sources.add(path)
        except ValueError, OSError, yaml.YAMLError, subprocess.CalledProcessError, RecursionError:
            # Broken sources/refs or an unbounded schema cannot turn into a pass.
            findings.append(f"{path}: source or reference comparison failed; not proven")
    selected_approval_sources = (
        {key[0] for key in _APPROVED_OPERATION_REMOVALS}
        | {key[0] for key in _APPROVED_PARAMETER_REMOVALS}
        | {key[0] for key in _APPROVED_RESPONSE_REPLACEMENTS}
        | {key[0] for key in _APPROVED_REQUEST_BOUND_NARROWINGS}
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
        "approved_request_bound_narrowings": approved_narrowings,
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
        for approval in report["approved_request_bound_narrowings"]:
            print("  approved request bound narrowing: " + approval)
        print("Known HTTP structural checks: " + ("rejected" if report["findings"] else "passed"))
        print("Not a full protocol compatibility proof; see unverified scope:")
        for boundary in report["unverified"]:
            print("  - " + boundary)
    return 1 if report["findings"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
