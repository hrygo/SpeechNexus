"""Reject obvious breaking changes to the public OpenAI-compatible contracts."""

from __future__ import annotations

import argparse
import json
import posixpath
import subprocess
import sys
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any, NamedTuple, cast
from urllib.parse import unquote

import yaml
from openapi_spec_validator import OpenAPIV31SpecValidator

_ROOT = Path(__file__).resolve().parents[2]
_PUBLIC_CONTRACTS = (
    "contracts/speech-openapi.yaml",
    "contracts/realtime-events.schema.json",
    "contracts/shared/capability-domains.schema.json",
    "contracts/shared/errors.schema.json",
    "contracts/shared/identifiers.schema.json",
    "contracts/shared/lifecycle.schema.json",
    "contracts/shared/operation-states.schema.json",
    "contracts/shared/public-audio.schema.json",
    "contracts/shared/references.schema.json",
)
_HTTP_METHODS = {"get", "put", "post", "delete", "options", "head", "patch", "trace"}
_OPENAI_PATHS = {
    "/v1/models",
    "/v1/models/{model}",
    "/v1/audio/transcriptions",
    "/v1/audio/speech",
    "/v1/realtime",
}
# The central placeholder is a process marker, not a runtime guarantee. When an
# operation leaves the unimplemented set, its 503 must be allowed to stop saying
# "permanently not implemented"; the replacement still has to be a retryable 503
# whose header and body values keep that signal, and inline enough that it cannot
# borrow another operation's guarantees.
_PLACEHOLDER_503_REF = "#/components/responses/OperationNotImplemented"
_PLACEHOLDER_ERROR_CODE = "operation_not_implemented"
_PLACEHOLDER_ERROR_TYPE = "service_unavailable_error"
_PLACEHOLDER_RETRYABLE = False
_ERROR_RESPONSE_REF = "#/components/schemas/ErrorResponse"
# The placeholder response says the capability is unavailable. Keep it out of
# shared caches: `no-store` is the only acceptable directive, so a replacement may
# not soften "unavailable" into a publicly cacheable response.
_NO_STORE_DIRECTIVE = "no-store"
_OPENAI_REALTIME_DEFS = {
    "SessionUpdate",
    "InputAudioBufferAppend",
    "InputAudioBufferCommit",
    "InputAudioBufferClear",
    "SessionCreated",
    "SessionUpdated",
    "InputAudioBufferCommitted",
    "InputAudioBufferCleared",
    "InputAudioBufferSpeechStarted",
    "InputAudioBufferSpeechStopped",
    "TranscriptionDelta",
    "TranscriptionCompleted",
    "TranscriptionFailed",
    "ErrorEvent",
}


class _SchemaSide(NamedTuple):
    document: Mapping[str, Any]
    path: str
    git_ref: str | None
    documents: dict[tuple[str, str | None], Mapping[str, Any] | None] | None = None
    standard_only: bool = True


def _mapping(value: object) -> Mapping[str, Any] | None:
    return cast(Mapping[str, Any], value) if isinstance(value, Mapping) else None


def _sequence(value: object) -> list[Any] | None:
    return value if isinstance(value, list) else None


def _load_base(ref: str, path: str) -> object:
    text = subprocess.check_output(
        ["git", "-C", str(_ROOT), "show", f"{ref}:{path}"],
        text=True,
    )
    return yaml.safe_load(text)


def _load_current(path: str) -> object:
    with (_ROOT / path).open(encoding="utf-8") as stream:
        return yaml.safe_load(stream)


def _load_schema_document(path: str, git_ref: str | None) -> Mapping[str, Any] | None:
    document = _load_base(git_ref, path) if git_ref is not None else _load_current(path)
    return _mapping(document)


def _resolve_local_pointer(document: Mapping[str, Any], pointer: str) -> object | None:
    if pointer in ("", "#"):
        return document
    if not pointer.startswith("/"):
        return None
    current: object = document
    for encoded_part in pointer.removeprefix("/").split("/"):
        part = unquote(encoded_part).replace("~1", "/").replace("~0", "~")
        if isinstance(current, Mapping):
            current = current.get(part)
        elif isinstance(current, list) and part.isdigit() and int(part) < len(current):
            current = current[int(part)]
        else:
            return None
    return current


def _raw_response(
    document: Mapping[str, Any], path_name: str, method: str, status: str
) -> Mapping[str, Any] | None:
    paths = _mapping(document.get("paths")) or {}
    path_item = _mapping(paths.get(path_name)) or {}
    operation = _mapping(path_item.get(method)) or {}
    responses = _mapping(operation.get("responses")) or {}
    return _mapping(responses.get(status))


def _exact_placeholder_503(
    document: Mapping[str, Any], path_name: str, method: str, status: str
) -> bool:
    if status != "503":
        return False
    response = _raw_response(document, path_name, method, status)
    return (
        response is not None
        and set(response) == {"$ref"}
        and response.get("$ref") == _PLACEHOLDER_503_REF
    )


def _resolve_schema_ref(
    ref: str, side: _SchemaSide
) -> tuple[Mapping[str, Any], _SchemaSide] | None:
    """Resolve one schema reference to its target and the document containing it."""
    if "://" in ref:
        return None
    if ref.startswith("#"):
        target_path = side.path
        target_git_ref = side.git_ref
        target_document: Mapping[str, Any] | None = side.document
        fragment = ref[1:]
    else:
        path_part, _, fragment = ref.partition("#")
        target_path = posixpath.normpath(posixpath.join(posixpath.dirname(side.path), path_part))
        target_git_ref = side.git_ref
        if not target_path.startswith("contracts/"):
            return None
        cache_key = (target_path, target_git_ref)
        if side.documents is not None and cache_key in side.documents:
            target_document = side.documents[cache_key]
        else:
            target_document = _load_schema_document(target_path, target_git_ref)
            if side.documents is not None:
                side.documents[cache_key] = target_document
        if target_document is None:
            return None
        fragment = fragment if not fragment or fragment.startswith("/") else f"/{fragment}"
    if target_document is None:
        return None
    target = _mapping(_resolve_local_pointer(target_document, fragment))
    if target is None:
        return None
    return target, _SchemaSide(
        target_document, target_path, target_git_ref, side.documents, side.standard_only
    )


def _instance_level_schemas(
    schema: object,
    side: _SchemaSide,
    seen: frozenset[tuple[str, str]] = frozenset(),
    depth: int = 0,
) -> list[tuple[Mapping[str, Any], _SchemaSide]] | None:
    """Schemas constraining the same instance level, each with its own document.

    `$ref` and `allOf` are followed because both conjoin at one instance level.
    `anyOf`/`oneOf` alternatives are refused: a union is not one conjunct, and
    modelling it as one could hide a branch that still allows the placeholder
    code. None means the constraints cannot be determined here.
    """
    if depth > 16:
        return None
    mapping = _mapping(schema)
    if mapping is None:
        return [] if isinstance(schema, bool) else None
    ref = mapping.get("$ref")
    if isinstance(ref, str):
        identity = (side.path, ref)
        if identity in seen:
            return None
        resolved = _resolve_schema_ref(ref, side)
        if resolved is None:
            return None
        target, target_side = resolved
        parts = _instance_level_schemas(target, target_side, seen | {identity}, depth + 1)
        if parts is None:
            return None
        siblings = {key: value for key, value in mapping.items() if key != "$ref"}
        if not siblings:
            return parts
        sibling_parts = _instance_level_schemas(siblings, side, seen, depth + 1)
        if sibling_parts is None:
            return None
        return parts + sibling_parts
    if "anyOf" in mapping or "oneOf" in mapping:
        return None
    parts = [(mapping, side)]
    variants = mapping.get("allOf")
    if variants is not None:
        items = _sequence(variants)
        if items is None:
            return None
        for variant in items:
            variant_parts = _instance_level_schemas(variant, side, seen, depth + 1)
            if variant_parts is None:
                return None
            parts.extend(variant_parts)
    return parts


def _declared_error_codes(schema: object, side: _SchemaSide) -> set[str] | None:
    """Declared `error.code` literals, or None when they cannot be determined.

    References are resolved inside the contract so a shared component cannot hide
    `operation_not_implemented`; an external or broken reference makes the body
    unjudgeable, and the caller rejects that instead of assuming it is safe.
    """
    levels = _instance_level_schemas(schema, side)
    if levels is None:
        return None
    codes: set[str] = set()
    for level, level_side in levels:
        error = (_mapping(level.get("properties")) or {}).get("error")
        if error is None:
            continue
        error_levels = _instance_level_schemas(error, level_side)
        if error_levels is None:
            return None
        for error_level, error_side in error_levels:
            code = (_mapping(error_level.get("properties")) or {}).get("code")
            if code is None:
                continue
            code_levels = _instance_level_schemas(code, error_side)
            if code_levels is None:
                return None
            for code_level, _ in code_levels:
                values = _sequence(code_level.get("enum"))
                if values is not None:
                    if not all(isinstance(value, str) for value in values):
                        return None
                    codes.update(values)
                if "const" in code_level:
                    const = code_level["const"]
                    if not isinstance(const, str):
                        return None
                    codes.add(const)
    return codes


def _property_levels(
    schema: object,
    side: _SchemaSide,
    path: tuple[str, ...],
) -> list[tuple[Mapping[str, Any], _SchemaSide]] | None:
    """Schemas constraining one nested property, or None when undeterminable.

    Each step follows `$ref`/`allOf` conjuncts at the current instance level, so a
    shared component's declaration is visited beside the inline one. A level that
    does not declare the property contributes no constraint.
    """
    levels = _instance_level_schemas(schema, side)
    if levels is None:
        return None
    if not path:
        return levels
    result: list[tuple[Mapping[str, Any], _SchemaSide]] = []
    for level, level_side in levels:
        child = (_mapping(level.get("properties")) or {}).get(path[0])
        if child is None:
            continue
        child_levels = _property_levels(child, level_side, path[1:])
        if child_levels is None:
            return None
        result.extend(child_levels)
    return result


def _declares_exactly(
    schema: object,
    side: _SchemaSide,
    path: tuple[str, ...],
    expected: object,
) -> bool:
    """True when every conjunction restricts the property to exactly `expected`.

    Conjuncts intersect: the shared `ErrorResponse` may admit every error type
    while the inline body narrows `error.type` to one literal, and that pair is
    exact. A level without `const`/`enum` does not restrict the value. Matching is
    type-sensitive so the boolean `false` cannot pass as the number `0`, and a
    property no conjunct constrains is rejected rather than assumed safe.
    """
    levels = _property_levels(schema, side, path)
    if levels is None:
        return False
    allowed: list[object] | None = None
    for level, _ in levels:
        constraints: list[list[object]] = []
        if "const" in level:
            constraints.append([level["const"]])
        enum_values = _sequence(level.get("enum"))
        if enum_values is not None:
            constraints.append(enum_values)
        for declared in constraints:
            if allowed is None:
                allowed = declared
            else:
                allowed = [value for value in allowed if any(value == other for other in declared)]
    if not allowed:
        return False
    return all(type(value) is type(expected) and value == expected for value in allowed)


def _declares_literal_only(schema: object, expected: str) -> bool:
    """True when every value a header schema allows is exactly `expected`."""
    mapping = _mapping(schema) or {}
    if "const" in mapping and mapping["const"] != expected:
        return False
    values = _sequence(mapping.get("enum"))
    if values is not None and not all(value == expected for value in values):
        return False
    return mapping.get("const") == expected or bool(values)


def _cache_control_is_no_store(value: object) -> bool:
    """True only when every directive token in one Cache-Control value is no-store.

    `Cache-Control` is a comma-separated directive list, so the check is per
    token, not a substring search: `x-no-store`, `no-store-please`,
    `no-store, max-age=0` and `No-Store, public` all name something else (or add
    a cacheable directive) and are rejected. Tokens are stripped and compared
    case-insensitively; a non-string value cannot be verified and is rejected.
    """
    if not isinstance(value, str):
        return False
    tokens = [token.strip().lower() for token in value.split(",")]
    directives = [token for token in tokens if token]
    return bool(directives) and all(token == _NO_STORE_DIRECTIVE for token in directives)


def _declares_no_store(schema: object, side: _SchemaSide) -> bool:
    """True when every declared Cache-Control value keeps the response uncacheable.

    A placeholder 503 reports that a capability is unavailable, so the response
    must not enter a shared cache. Each declared `const`/`enum` value has to be
    exactly the `no-store` directive; values are read at the same instance level,
    following `$ref` and `allOf` conjuncts. A schema that declares no value, or
    whose reference cannot be resolved, cannot be verified and is rejected rather
    than assumed safe.
    """
    levels = _instance_level_schemas(schema, side)
    if levels is None:
        return False
    declared = False
    for level, _ in levels:
        values: list[object] = []
        if "const" in level:
            values.append(level["const"])
        enum_values = _sequence(level.get("enum"))
        if enum_values is not None:
            values.extend(enum_values)
        for value in values:
            declared = True
            if not _cache_control_is_no_store(value):
                return False
    return declared


def _placeholder_503_problems(
    response: Mapping[str, Any] | None,
    document: Mapping[str, Any],
    side: _SchemaSide,
    *,
    expected_retry: str,
    replacement: bool,
) -> list[str]:
    """One message per structural rule a placeholder 503 response violates.

    The placeholder says "permanently not implemented" through the header value
    and the error code, not only through the header names. A replacement has to
    invert both and stay inline; an unchanged placeholder must still declare the
    shared component's original shape instead of silently weakening it.
    """
    subject = "placeholder 503 replacement" if replacement else "placeholder 503 response"
    if response is None:
        return [f"{subject} is missing"]
    ref = response.get("$ref")
    if isinstance(ref, str):
        if replacement:
            return [f"{subject} must be an inline response, not a $ref"]
        definition = (
            _mapping(_resolve_local_pointer(document, ref[1:])) if ref.startswith("#") else None
        )
        if definition is None:
            return [f"{subject} reference cannot be resolved"]
    else:
        definition = response
    problems: list[str] = []
    headers = {
        str(name).lower(): value
        for name, value in (_mapping(definition.get("headers")) or {}).items()
    }
    cache_control = _mapping(headers.get("cache-control")) or {}
    if cache_control.get("required") is not True or not _declares_no_store(
        cache_control.get("schema"), side
    ):
        problems.append(
            f'{subject} requires a required Cache-Control header declaring "{_NO_STORE_DIRECTIVE}"'
        )
    retry = _mapping(headers.get("x-should-retry")) or {}
    if retry.get("required") is not True or not _declares_literal_only(
        retry.get("schema"), expected_retry
    ):
        problems.append(
            f'{subject} requires a required X-Should-Retry header declaring "{expected_retry}"'
        )
    content = _mapping(definition.get("content")) or {}
    media = _mapping(content.get("application/json")) or {}
    schema = _mapping(media.get("schema")) or {}
    variants = _sequence(schema.get("allOf")) or []
    if len(variants) < 2 or not any(
        (_mapping(variant) or {}).get("$ref") == _ERROR_RESPONSE_REF for variant in variants
    ):
        problems.append(
            f"{subject} requires an application/json body composed with allOf that references "
            f"{_ERROR_RESPONSE_REF}"
        )
    if replacement:
        codes = _declared_error_codes(schema, side)
        if codes is None:
            problems.append(f"{subject} error.code cannot be determined from external references")
        elif _PLACEHOLDER_ERROR_CODE in codes:
            problems.append(
                f"{subject} must not declare {_PLACEHOLDER_ERROR_CODE} in its error.code"
            )
    else:
        for path, expected in (
            (("error", "code"), _PLACEHOLDER_ERROR_CODE),
            (("error", "type"), _PLACEHOLDER_ERROR_TYPE),
            (("error", "retryable"), _PLACEHOLDER_RETRYABLE),
        ):
            if not _declares_exactly(schema, side, path, expected):
                problems.append(
                    f"{subject} requires {'.'.join(path)} exactly {json.dumps(expected)}"
                )
    return problems


def _dereference_schema_children(
    schema: Mapping[str, Any],
    side: _SchemaSide,
    seen: frozenset[tuple[str, str]],
) -> dict[str, Any]:
    if side.standard_only:
        # Keep the original standard compatibility policy unchanged.
        return {key: _dereference_schema(value, side, seen) for key, value in schema.items()}
    # References apply at schema positions, never inside const/enum/default or
    # example JSON data. Property names (including "const") are map keys, not
    # schema keywords until their child schema is entered.
    maps = {
        "properties",
        "patternProperties",
        "$defs",
        "definitions",
        "dependentSchemas",
        "dependencies",
    }
    arrays = {"allOf", "anyOf", "oneOf", "prefixItems"}
    singles = {
        "items",
        "additionalItems",
        "additionalProperties",
        "unevaluatedItems",
        "unevaluatedProperties",
        "contains",
        "propertyNames",
        "not",
        "if",
        "then",
        "else",
        "contentSchema",
    }
    result = dict(schema)
    for key, value in schema.items():
        if key in maps and isinstance(value, Mapping):
            result[key] = {
                name: _dereference_schema(child, side, seen) for name, child in value.items()
            }
        elif key in arrays and isinstance(value, list):
            result[key] = [_dereference_schema(child, side, seen) for child in value]
        elif key in singles:
            result[key] = _dereference_schema(value, side, seen)
    return result


def _dereference_schema(
    schema: object,
    side: _SchemaSide,
    seen: frozenset[tuple[str, str]] = frozenset(),
) -> object:
    return _dereference_node(schema, side, seen)


def _dereference_openapi(paths: object, side: _SchemaSide) -> object:
    if side.standard_only:
        return _dereference_schema(paths, side)
    return _dereference_node(paths, side, position="paths")


def _dereference_children(
    value: Mapping[str, Any],
    side: _SchemaSide,
    seen: frozenset[tuple[str, str]],
    position: str,
) -> dict[str, Any]:
    if position == "schema":
        return _dereference_schema_children(value, side, seen)
    # OpenAPI container names and object references are not schema keywords.
    # Only a media/parameter/header schema enters the JSON Schema walker.
    containers = {
        "paths": "path_item",
        "responses": "response",
        "headers": "header",
        "content": "media",
    }
    edges = {
        "path_item": {**dict.fromkeys(_HTTP_METHODS, "operation"), "parameters": "parameters"},
        "operation": {
            "parameters": "parameters",
            "requestBody": "request_body",
            "responses": "responses",
        },
        "response": {"headers": "headers", "content": "content"},
        "request_body": {"content": "content"},
        "parameter": {"schema": "schema", "content": "content"},
        "header": {"schema": "schema", "content": "content"},
        "media": {"schema": "schema"},
    }
    result = dict(value)
    for key, child in value.items():
        child_position = containers.get(position) or edges.get(position, {}).get(key)
        if child_position is not None:
            result[key] = _dereference_node(child, side, seen, position=child_position)
    return result


def _dereference_node(
    schema: object,
    side: _SchemaSide,
    seen: frozenset[tuple[str, str]] = frozenset(),
    *,
    position: str = "schema",
) -> object:
    schema_mapping = _mapping(schema)
    if schema_mapping is None:
        if isinstance(schema, list):
            return [
                _dereference_node(
                    item, side, seen, position="parameter" if position == "parameters" else position
                )
                for item in schema
            ]
        return schema
    ref = schema_mapping.get("$ref")
    if not isinstance(ref, str) or "://" in ref:
        if not side.standard_only and "$ref" in schema_mapping:
            raise ValueError("Nonstandard HTTP comparison requires closed repository references")
        return _dereference_children(schema_mapping, side, seen, position)
    identity = (side.path, ref)
    if identity in seen:
        if not side.standard_only:
            raise ValueError("Recursive HTTP schema comparison needs explicit proof")
        return schema

    if ref.startswith("#"):
        target_path = side.path
        target_git_ref = side.git_ref
        resolved_document = side.document
        fragment = ref[1:]
    else:
        path_part, separator, fragment = ref.partition("#")
        target_path = posixpath.normpath(posixpath.join(posixpath.dirname(side.path), path_part))
        target_git_ref = side.git_ref
        if not side.standard_only and not target_path.startswith("contracts/"):
            raise ValueError("HTTP references must remain within the contract sources")
        cache_key = (target_path, target_git_ref)
        if side.documents is not None and cache_key in side.documents:
            loaded_document = side.documents[cache_key]
        else:
            loaded_document = _load_schema_document(target_path, target_git_ref)
            if side.documents is not None:
                side.documents[cache_key] = loaded_document
        if loaded_document is None:
            if not side.standard_only:
                raise ValueError("HTTP referenced document is unavailable")
            return schema
        resolved_document = loaded_document
        fragment = fragment if not separator or fragment.startswith("/") else f"/{fragment}"

    target = _resolve_local_pointer(_mapping(resolved_document) or {}, fragment)
    if target is None:
        if not side.standard_only:
            raise ValueError("HTTP reference pointer is unavailable")
        return schema
    resolved = _dereference_node(
        target,
        _SchemaSide(
            resolved_document, target_path, target_git_ref, side.documents, side.standard_only
        ),
        seen | {identity},
        position=position,
    )
    siblings = {key: value for key, value in schema_mapping.items() if key != "$ref"}
    if isinstance(resolved, bool) and siblings:
        # $ref siblings intersect with its target even when that target is a
        # boolean schema. Keep the reference policy before simplifying false.
        annotations = {"title", "description", "examples", "$comment"}
        if not (siblings.keys() - annotations):
            return resolved
        resolved_siblings = _dereference_children(siblings, side, seen, position)
        return resolved_siblings if resolved else False
    resolved_mapping = _mapping(resolved)
    if siblings and resolved_mapping is not None:
        resolved_siblings = _dereference_children(siblings, side, seen, position)
        annotations = {"title", "description", "default", "examples", "$comment"}
        conflicts = (resolved_mapping.keys() & resolved_siblings.keys()) - annotations
        if any(
            not _same_json_tree(
                _compatibility_shape({key: resolved_mapping[key]}),
                _compatibility_shape({key: resolved_siblings[key]}),
                numeric_equivalent=True,
            )
            if side.standard_only
            else not _same_json_tree(resolved_mapping[key], resolved_siblings[key])
            for key in conflicts
        ):
            # JSON Schema $ref siblings intersect; overriding the target can hide
            # a tighter bound or change the meaning of a composed schema.
            return {"allOf": [resolved_mapping, resolved_siblings]}
        return {**resolved_mapping, **resolved_siblings}
    return resolved


def _issue(issues: list[str], pointer: str, message: str) -> None:
    issues.append(f"{pointer}: {message}")


def _type_set(value: object) -> set[str]:
    if isinstance(value, str):
        return {value}
    values = _sequence(value)
    return {item for item in values or [] if isinstance(item, str)}


def _constraint_break(base: Mapping[str, Any], current: Mapping[str, Any]) -> str | None:
    checks: tuple[tuple[str, str], ...] = (
        ("minimum", ">"),
        ("exclusiveMinimum", ">"),
        ("maximum", "<"),
        ("exclusiveMaximum", "<"),
        ("minLength", ">"),
        ("minItems", ">"),
        ("maxLength", "<"),
        ("maxItems", "<"),
        ("minProperties", ">"),
        ("maxProperties", "<"),
        ("minContains", ">"),
        ("maxContains", "<"),
    )
    for key, direction in checks:
        old = base.get(key)
        new = current.get(key)
        if new is None:
            continue
        if old is None:
            return f"{key} was tightened from unbounded to {new!r}"
        if not isinstance(old, (int, float)) or not isinstance(new, (int, float)):
            continue
        if (direction == ">" and new > old) or (direction == "<" and new < old):
            return f"{key} was tightened from {old!r} to {new!r}"
    for key in ("pattern", "multipleOf"):
        if key in current and base.get(key) != current.get(key):
            return f"{key} changed from {base.get(key)!r} to {current.get(key)!r}"
    if base.get("uniqueItems") is not True and current.get("uniqueItems") is True:
        return "uniqueItems was tightened to true"
    return None


def _compatibility_shape(schema: object, *, standard_only: bool = True) -> object:
    """Keep constraints, excluding Nexus only for the original standard surface."""
    if isinstance(schema, list):
        return [_compatibility_shape(item, standard_only=standard_only) for item in schema]
    mapping = _mapping(schema)
    if mapping is None:
        return schema
    result: dict[str, Any] = {}
    for key, value in mapping.items():
        if key in {"title", "description", "examples", "$comment"}:
            continue
        if key in {"properties", "patternProperties", "$defs", "definitions", "dependentSchemas"}:
            children = _mapping(value)
            result[key] = (
                {
                    name: _compatibility_shape(child, standard_only=standard_only)
                    for name, child in children.items()
                    if not standard_only or key != "properties" or name != "nexus"
                }
                if children is not None
                else value
            )
        elif key in {
            "oneOf",
            "anyOf",
            "allOf",
            "prefixItems",
            "items",
            "contains",
            "additionalProperties",
            "unevaluatedProperties",
            "unevaluatedItems",
            "propertyNames",
            "not",
            "if",
            "then",
            "else",
        }:
            result[key] = _compatibility_shape(value, standard_only=standard_only)
        else:
            # In particular, const/enum contain data, not nested schemas.
            result[key] = value
    return result


def _same_json_tree(left: object, right: object, *, numeric_equivalent: bool = False) -> bool:
    """Exact schema trees by default; JSON value sites may equate 1 and 1.0.

    Booleans never participate in number equality, including in literal containers.
    The existing response-intersection proof keeps its exact-tree default.
    """
    if type(left) is not type(right):
        return (
            numeric_equivalent
            and type(left) in (int, float)
            and type(right) in (int, float)
            and left == right
        )
    if isinstance(left, Mapping) and isinstance(right, Mapping):
        return (
            all(isinstance(key, str) for key in (*left, *right))
            and left.keys() == right.keys()
            and all(
                _same_json_tree(value, right[key], numeric_equivalent=numeric_equivalent)
                for key, value in left.items()
            )
        )
    if isinstance(left, list) and isinstance(right, list):
        return len(left) == len(right) and all(
            _same_json_tree(a, b, numeric_equivalent=numeric_equivalent)
            for a, b in zip(left, right, strict=True)
        )
    return left == right


def _closed_schema_tree(value: object) -> bool:
    """Exact-tree implication needs no unresolved or dynamic reference semantics."""
    if isinstance(value, Mapping):
        return not {
            "$ref",
            "$dynamicRef",
            "$recursiveRef",
            "$dynamicAnchor",
            "$recursiveAnchor",
        }.intersection(value) and all(_closed_schema_tree(child) for child in value.values())
    if isinstance(value, list):
        return all(_closed_schema_tree(child) for child in value)
    return True


def _object_conjuncts(schema: object) -> list[Mapping[str, Any]] | None:
    """Only property declarations, required names and object allOf are modelled.

    Do not flatten additional/pattern/unevaluated properties, conditionals or
    unknown object keywords: they can constrain a newly declared name or change
    evaluation. Property *values* stay opaque and must remain identical below.
    """
    node = _mapping(schema)
    if node is None or node.keys() - {"type", "properties", "required", "allOf"}:
        return None
    if "type" in node and node["type"] != "object":
        return None
    if "properties" in node and (
        not isinstance(node["properties"], Mapping)
        or not all(isinstance(name, str) for name in node["properties"])
    ):
        return None
    if "required" in node and (
        not isinstance(node["required"], list)
        or not all(isinstance(name, str) for name in node["required"])
    ):
        return None
    parts: list[Mapping[str, Any]] = [node]
    if "allOf" in node:
        children = _sequence(node["allOf"])
        if not children:
            return None
        for child in children:
            resolved = _object_conjuncts(child)
            if resolved is None:
                return None
            parts.extend(resolved)
    return parts


def _required_string_tags(parts: list[Mapping[str, Any]]) -> dict[str, frozenset[str]]:
    """Intersect actual required const/enum constraints, ignoring OAS metadata."""
    required = {name for part in parts for name in part.get("required", [])}
    tags: dict[str, frozenset[str]] = {}
    for name in required:
        values: set[str] | None = None
        for part in parts:
            if name not in part.get("properties", {}):
                continue
            prop = _mapping(part["properties"][name])
            if (
                prop is None
                or prop.keys() - {"type", "const", "enum"}
                or ("type" in prop and prop["type"] != "string")
            ):
                break
            allowed: set[str] | None = None
            if "enum" in prop:
                if not isinstance(prop["enum"], list) or not all(
                    isinstance(value, str) for value in prop["enum"]
                ):
                    break
                allowed = set(prop["enum"])
            if "const" in prop:
                if not isinstance(prop["const"], str):
                    break
                allowed = {prop["const"]} if allowed is None else allowed & {prop["const"]}
            if allowed is not None:
                values = allowed if values is None else values & allowed
        else:
            if values:
                tags[name] = frozenset(values)
    return tags


def _optional_declarations_only(old: object, new: object, old_names: set[str]) -> bool:
    left, right = _mapping(old), _mapping(new)
    if left is None or right is None or left.keys() != right.keys():
        return False
    for key in left:
        if key == "properties":
            before, after = left[key], right[key]
            if not before.keys() <= after.keys():
                return False
            for name, schema in after.items():
                if name in before:
                    if not _same_json_tree(before[name], schema, numeric_equivalent=True):
                        return False
                elif name in old_names or not isinstance(schema, Mapping | bool):
                    return False
        elif key == "allOf":
            if len(left[key]) != len(right[key]) or not all(
                _optional_declarations_only(a, b, old_names)
                for a, b in zip(left[key], right[key], strict=True)
            ):
                return False
        elif not _same_json_tree(left[key], right[key], numeric_equivalent=True):
            return False
    return True


def _request_dialects_support_closure(side: _SchemaSide | None) -> bool:
    """Certify a conservative document-wide dialect envelope before erasing refs.

    The resolver flattens schemas, losing ancestor/target dialect declarations.
    Inspect its original document snapshots, including every loaded reference
    document. Any unknown declaration disables only the new request proof.
    This deliberately also rejects unknown declarations in unused/literal data;
    it does not attempt to reconstruct mixed-dialect schema resource scopes.
    """
    if side is None:
        return False
    supported = {
        "https://spec.openapis.org/oas/3.1/dialect/base",
        "https://json-schema.org/draft/2020-12/schema",
    }

    def known(value: object) -> bool:
        return isinstance(value, str) and value in supported

    def declarations_known(value: object) -> bool:
        if isinstance(value, Mapping):
            return ("$schema" not in value or known(value["$schema"])) and all(
                declarations_known(child) for child in value.values()
            )
        if isinstance(value, list):
            return all(declarations_known(child) for child in value)
        return True

    documents = [side.document, *(side.documents or {}).values()]
    for document in documents:
        if document is None:
            return False
        if document.get("openapi") == "3.1.0" and OpenAPIV31SpecValidator.schema_validator.is_valid(
            document
        ):
            # Validate the resource's OAS envelope, not just an annotation that
            # is also legal in a bare JSON Schema. The bundled meta-schema only
            # resolves its own definitions: it never follows document $refs or
            # fetches a schema dialect. Keep normal repository resolution above.
            dialect = document.get(
                "jsonSchemaDialect", "https://spec.openapis.org/oas/3.1/dialect/base"
            )
        else:
            # A bare schema (even with OAS-looking annotations) needs its own
            # explicit supported declaration; it cannot borrow an OAS default.
            dialect = document.get("$schema")
        if not known(dialect) or not declarations_known(document):
            return False
    return True


def _closed_tagged_request_extension(
    base: Mapping[str, Any],
    current: Mapping[str, Any],
    *,
    base_side: _SchemaSide | None,
    current_side: _SchemaSide | None,
) -> bool:
    """Prove Old <= New for one narrow, evaluation-preserving request subset.

    A false parent unevaluatedProperties closes each old selected branch. Its
    valid objects cannot contain a newly declared name. With all old property
    predicates/required names unchanged, adding optional declarations therefore
    preserves both validity and evaluated names on every old valid input.
    Required disjoint string tags preserve exactly one successful branch.
    This is a proof, not the partial comparator's absence of findings.
    """
    if not all(_request_dialects_support_closure(side) for side in (base_side, current_side)):
        return False
    old = _mapping(_compatibility_shape(base, standard_only=False))
    new = _mapping(_compatibility_shape(current, standard_only=False))
    if old is None or new is None or not _closed_schema_tree([old, new]):
        return False
    allowed = {"oneOf", "unevaluatedProperties", "discriminator", "type"}
    if old.keys() - allowed or new.keys() - allowed:
        return False
    if (
        old.get("unevaluatedProperties") is not False
        or new.get("unevaluatedProperties") is not False
    ):
        return False
    if not _same_json_tree(
        {k: v for k, v in old.items() if k != "oneOf"},
        {k: v for k, v in new.items() if k != "oneOf"},
    ) or ("type" in old and old["type"] != "object"):
        return False
    variants = [_sequence(side.get("oneOf")) for side in (old, new)]
    if any(not branches for branches in variants):
        return False
    branches = [list(side or []) for side in variants]
    parts = [[_object_conjuncts(branch) for branch in side] for side in branches]
    if any(
        not group or not any(p.get("type") == "object" for p in group)
        for side in parts
        for group in side
    ):
        return False
    tags = [[_required_string_tags(group or []) for group in side] for side in parts]
    common = set.intersection(*(set(tag) for side in tags for tag in side))
    for name in common:
        indexes = [{tag[name]: i for i, tag in enumerate(side)} for side in tags]
        if any(len(index) != len(side) for index, side in zip(indexes, branches, strict=True)):
            continue
        if indexes[0].keys() != indexes[1].keys():
            continue
        values = list(indexes[0])
        if any(a & b for i, a in enumerate(values) for b in values[i + 1 :]):
            continue
        if all(
            _optional_declarations_only(
                branches[0][i],
                branches[1][indexes[1][value]],
                {
                    field
                    for part in parts[0][i] or []
                    for field in (*part.get("properties", {}), *part.get("required", []))
                },
            )
            for value, i in indexes[0].items()
        ):
            return True
    return False


class _DefaultExemption(NamedTuple):
    """Request-body defaults are non-load-bearing on a permanent placeholder.

    The proof is the frozen baseline itself: an operation whose 503 is exactly
    the shared placeholder component and whose declared ``error.code`` is the
    ``operation_not_implemented`` literal. The baseline may also declare 2xx
    responses, but a deployment that answers this placeholder never serves them:
    the operation is declared permanently unimplemented and is unreachable, so no
    consumer can depend on one of its request defaults. An operation that already
    shipped, or whose 503 is an inline retryable replacement, does not qualify.
    """

    notices: list[str]
    permanent_placeholder: bool


def _baseline_permanent_placeholder(
    document: Mapping[str, Any],
    path_name: str,
    method: str,
    side: _SchemaSide,
) -> bool:
    if not _exact_placeholder_503(document, path_name, method, "503"):
        return False
    component = _mapping(_resolve_local_pointer(document, _PLACEHOLDER_503_REF[1:]))
    if component is None:
        return False
    content = _mapping(component.get("content")) or {}
    media = _mapping(content.get("application/json"))
    if media is None or "schema" not in media:
        return False
    return _declared_error_codes(media["schema"], side) == {_PLACEHOLDER_ERROR_CODE}


def _compare_schema(
    base: object,
    current: object,
    pointer: str,
    issues: list[str],
    *,
    direction: str = "request",
    base_side: _SchemaSide | None = None,
    current_side: _SchemaSide | None = None,
    exemption: _DefaultExemption | None = None,
) -> None:
    standard_only = all(
        side.standard_only for side in (base_side, current_side) if side is not None
    )
    if base_side is not None:
        base = _dereference_schema(base, base_side)
    if current_side is not None:
        current = _dereference_schema(current, current_side)
    if isinstance(base, bool) or isinstance(current, bool):
        accepted, constrained = (base, current) if direction == "request" else (current, base)
        if (constrained is False and accepted is not False) or (
            accepted is True and constrained is not True
        ):
            _issue(issues, pointer, "boolean schema acceptance/guarantee changed")
        return
    base_schema = _mapping(base)
    current_schema = _mapping(current)
    if base_schema is None or current_schema is None:
        if type(base) is not type(current):
            _issue(issues, pointer, "schema node kind changed")
        return

    # A response intersection that explicitly retains the complete resolved
    # baseline can only strengthen its guarantees. This narrow proof does not
    # flatten arbitrary composition, widen requests, or affect OpenAI defaults.
    if direction == "response" and not standard_only and _closed_schema_tree(base_schema):
        variants = _sequence(current_schema.get("allOf"))
        if variants is not None and any(
            _same_json_tree(variant, base_schema) for variant in variants
        ):
            return

    base_ref = base_schema.get("$ref")
    current_ref = current_schema.get("$ref")
    if base_ref != current_ref:
        _issue(issues, pointer, f"$ref changed from {base_ref!r} to {current_ref!r}")

    base_types = _type_set(base_schema.get("type"))
    current_types = _type_set(current_schema.get("type"))
    type_break = (
        bool(current_types) and (not base_types or not base_types <= current_types)
        if direction == "request"
        else bool(base_types) and (not current_types or not current_types <= base_types)
    )
    if type_break:
        _issue(
            issues, pointer, f"type changed from {sorted(base_types)} to {sorted(current_types)}"
        )

    constrained, accepted = (
        (current_schema, base_schema) if direction == "request" else (base_schema, current_schema)
    )
    if (
        "const" in constrained
        and (
            "const" not in accepted
            or not _same_json_tree(constrained["const"], accepted["const"], numeric_equivalent=True)
        )
        and not _same_json_tree(
            accepted.get("enum"), [constrained["const"]], numeric_equivalent=True
        )
    ):
        _issue(issues, pointer, "const value changed")

    default_change: str | None = None
    if ("default" in base_schema) != ("default" in current_schema):
        default_change = "default presence changed"
    elif "default" in base_schema and not _same_json_tree(
        base_schema.get("default"), current_schema.get("default"), numeric_equivalent=True
    ):
        default_change = (
            f"default changed from {base_schema.get('default')!r} "
            f"to {current_schema.get('default')!r}"
        )
    if default_change is not None:
        if direction == "request" and exemption is not None and exemption.permanent_placeholder:
            exemption.notices.append(
                f"{pointer}: {default_change}; accepted because the baseline operation's "
                f"503 is the permanent {_PLACEHOLDER_ERROR_CODE} placeholder"
            )
        else:
            _issue(issues, pointer, default_change)

    if base_schema.get("format") != current_schema.get("format"):
        _issue(
            issues,
            pointer,
            f"format changed from {base_schema.get('format')!r} "
            f"to {current_schema.get('format')!r}",
        )

    base_enum = _sequence(constrained.get("enum"))
    current_enum = _sequence(accepted.get("enum"))
    if base_enum is not None:
        values = current_enum
        if "const" in accepted:
            values = [accepted["const"]]
        missing = [
            value
            for value in values or []
            if not any(_same_json_tree(value, old, numeric_equivalent=True) for old in base_enum)
        ]
        if values is None or missing:
            _issue(
                issues,
                pointer,
                f"enum values {'removed' if direction == 'request' else 'added'}: {missing!r}",
            )

    base_required = set(cast(Iterable[str], base_schema.get("required", [])))
    current_required = set(cast(Iterable[str], current_schema.get("required", [])))
    changed_required = (
        current_required - base_required
        if direction == "request"
        else base_required - current_required
    )
    if changed_required:
        label = (
            "new required fields"
            if direction == "request"
            else "response required guarantees removed"
        )
        _issue(issues, pointer, f"{label}: {sorted(changed_required)}")

    if (
        accepted.get("additionalProperties") is not False
        and constrained.get("additionalProperties") is False
    ):
        _issue(issues, pointer, "additionalProperties acceptance/guarantee changed")

    constraint_issue = _constraint_break(accepted, constrained)
    if constraint_issue is not None:
        _issue(issues, pointer, constraint_issue)

    base_properties = _mapping(base_schema.get("properties"))
    current_properties = _mapping(current_schema.get("properties"))
    if base_properties is not None:
        for name, base_property in base_properties.items():
            if standard_only and name == "nexus":
                continue
            property_pointer = f"{pointer}/properties/{name}"
            if current_properties is None or name not in current_properties:
                _issue(issues, property_pointer, "property removed")
            else:
                _compare_schema(
                    base_property,
                    current_properties[name],
                    property_pointer,
                    issues,
                    direction=direction,
                    base_side=base_side,
                    current_side=current_side,
                    exemption=exemption,
                )

    # A newly named property replaces the previous additionalProperties domain.
    # This can narrow a request or widen a response even when it is optional.
    for name, current_property in (current_properties or {}).items():
        if (standard_only and name == "nexus") or name in (base_properties or {}):
            continue
        _compare_schema(
            base_schema.get("additionalProperties", True),
            current_property,
            f"{pointer}/properties/{name}",
            issues,
            direction=direction,
            base_side=base_side,
            current_side=current_side,
            exemption=exemption,
        )

    for definitions_key in ("$defs", "definitions"):
        base_definitions = _mapping(base_schema.get(definitions_key))
        current_definitions = _mapping(current_schema.get(definitions_key))
        if base_definitions is None:
            continue
        for name, base_definition in base_definitions.items():
            definition_pointer = f"{pointer}/{definitions_key}/{name}"
            if current_definitions is None or name not in current_definitions:
                _issue(issues, definition_pointer, "schema definition removed")
            else:
                _compare_schema(
                    base_definition,
                    current_definitions[name],
                    definition_pointer,
                    issues,
                    direction=direction,
                    base_side=base_side,
                    current_side=current_side,
                    exemption=exemption,
                )

    for keyword in (
        "if",
        "then",
        "else",
        "not",
        "dependentRequired",
        "dependentSchemas",
        "unevaluatedProperties",
        "contains",
        "prefixItems",
    ):
        if not _same_json_tree(
            base_schema.get(keyword), current_schema.get(keyword), numeric_equivalent=True
        ):
            _issue(
                issues,
                f"{pointer}/{keyword}",
                "conditional or composed constraint changed; compatibility needs explicit proof",
            )
    if "items" in base_schema or "items" in current_schema:
        _compare_schema(
            base_schema.get("items", True),
            current_schema.get("items", True),
            f"{pointer}/items",
            issues,
            direction=direction,
            base_side=base_side,
            current_side=current_side,
            exemption=exemption,
        )
    old_additional = base_schema.get("additionalProperties", True)
    new_additional = current_schema.get("additionalProperties", True)
    if isinstance(old_additional, Mapping) or isinstance(new_additional, Mapping):
        _compare_schema(
            old_additional,
            new_additional,
            f"{pointer}/additionalProperties",
            issues,
            direction=direction,
            base_side=base_side,
            current_side=current_side,
            exemption=exemption,
        )
    for keyword in ("oneOf", "anyOf", "allOf"):
        base_variants = _sequence(base_schema.get(keyword))
        current_variants = _sequence(current_schema.get(keyword))
        if base_variants is None:
            if current_variants is not None and direction == "request":
                _issue(
                    issues,
                    f"{pointer}/{keyword}",
                    "composition introduced; compatibility needs explicit proof",
                )
            continue
        if current_variants is None:
            if direction == "response":
                _issue(issues, f"{pointer}/{keyword}", "response composition guarantee removed")
            continue
        if len(current_variants) != len(base_variants):
            _issue(
                issues,
                f"{pointer}/{keyword}",
                "variant count changed; compatibility needs explicit proof",
            )
            continue
        if keyword == "oneOf":
            # oneOf is not monotone in its branches: widening a request branch
            # can reject old inputs by making two branches match. Require the
            # resolved constraints to stay equal unless compatibility is proven.
            if not _same_json_tree(
                _compatibility_shape(base_variants, standard_only=standard_only),
                _compatibility_shape(current_variants, standard_only=standard_only),
                numeric_equivalent=True,
            ) and not (
                direction == "request"
                and not standard_only
                and _closed_tagged_request_extension(
                    base_schema, current_schema, base_side=base_side, current_side=current_side
                )
            ):
                _issue(
                    issues,
                    f"{pointer}/{keyword}",
                    "exclusive variants changed; compatibility needs explicit proof",
                )
            continue
        for index, variant in enumerate(base_variants):
            _compare_schema(
                variant,
                current_variants[index],
                f"{pointer}/{keyword}/{index}",
                issues,
                direction=direction,
                base_side=base_side,
                current_side=current_side,
                exemption=exemption,
            )


def _compare_parameters(
    base_parameters: object,
    current_parameters: object,
    pointer: str,
    issues: list[str],
    *,
    base_side: _SchemaSide | None = None,
    current_side: _SchemaSide | None = None,
) -> None:
    def indexed(parameters: object) -> dict[tuple[str, str], Mapping[str, Any]]:
        result: dict[tuple[str, str], Mapping[str, Any]] = {}
        for parameter in _sequence(parameters) or []:
            value = _mapping(parameter)
            if value is None:
                continue
            name = value.get("name")
            location = value.get("in")
            if isinstance(name, str) and isinstance(location, str):
                result[(name, location)] = value
        return result

    base_index = indexed(base_parameters)
    current_index = indexed(current_parameters)
    for key, parameter in current_index.items():
        if key not in base_index and parameter.get("required") is True:
            _issue(issues, pointer, f"new required parameter: {key[1]} {key[0]}")
    for key, base_parameter in base_index.items():
        if key not in current_index:
            _issue(issues, pointer, f"parameter removed: {key[1]} {key[0]}")
            continue
        current_parameter = current_index[key]
        if base_parameter.get("required") is not True and current_parameter.get("required") is True:
            _issue(issues, pointer, f"parameter became required: {key[1]} {key[0]}")
        _compare_schema(
            base_parameter.get("schema", {}),
            current_parameter.get("schema", {}),
            f"{pointer}/parameters/{key[1]}/{key[0]}",
            issues,
            base_side=base_side,
            current_side=current_side,
        )


def _compare_openapi(
    base: Mapping[str, Any],
    current: Mapping[str, Any],
    path: str,
    *,
    base_git_ref: str | None = None,
    standard_only: bool = True,
    notices: list[str] | None = None,
) -> list[str]:
    issues: list[str] = []
    collected = notices if notices is not None else []
    # Each comparison owns fresh document snapshots. A process-global cache could
    # hide edits to a shared schema during a later comparison in the same process.
    base_side = _SchemaSide(base, path, base_git_ref, {}, standard_only)
    current_side = _SchemaSide(current, path, None, {}, standard_only)
    # Restrict the walk before expanding refs; the complete document stays on each
    # side so shared parameters, responses and component refs still resolve.
    base_paths = (
        _mapping(
            _dereference_openapi(
                {
                    name: item
                    for name, item in (_mapping(base.get("paths")) or {}).items()
                    if not standard_only or name in _OPENAI_PATHS
                },
                base_side,
            )
        )
        or {}
    )
    current_paths = (
        _mapping(
            _dereference_openapi(
                {
                    name: item
                    for name, item in (_mapping(current.get("paths")) or {}).items()
                    if not standard_only or name in _OPENAI_PATHS
                },
                current_side,
            )
        )
        or {}
    )
    for path_name, base_path_item in base_paths.items():
        if standard_only and path_name not in _OPENAI_PATHS:
            continue
        current_path_item = current_paths.get(path_name)
        if current_path_item is None:
            _issue(issues, f"{path}:paths/{path_name}", "path removed")
            continue
        base_path_mapping = _mapping(base_path_item) or {}
        current_path_mapping = _mapping(current_path_item) or {}
        for method, base_operation in base_path_mapping.items():
            if method not in _HTTP_METHODS:
                continue
            current_operation = _mapping(current_path_mapping.get(method))
            operation_pointer = f"{path}:paths/{path_name}/{method}"
            if current_operation is None:
                _issue(issues, operation_pointer, "operation removed")
                continue
            base_operation_mapping = _mapping(base_operation) or {}
            _compare_parameters(
                [
                    *base_path_mapping.get("parameters", []),
                    *base_operation_mapping.get("parameters", []),
                ],
                [
                    *current_path_mapping.get("parameters", []),
                    *current_operation.get("parameters", []),
                ],
                operation_pointer,
                issues,
                base_side=base_side,
                current_side=current_side,
            )

            base_body = _mapping(base_operation_mapping.get("requestBody"))
            current_body = _mapping(current_operation.get("requestBody"))
            if (
                base_body is None
                and current_body is not None
                and current_body.get("required") is True
            ):
                _issue(issues, operation_pointer, "new required requestBody")
            if base_body is not None:
                if current_body is None:
                    _issue(issues, operation_pointer, "requestBody removed")
                else:
                    if (
                        base_body.get("required") is not True
                        and current_body.get("required") is True
                    ):
                        _issue(issues, operation_pointer, "requestBody became required")
                    base_content = _mapping(base_body.get("content")) or {}
                    current_content = _mapping(current_body.get("content")) or {}
                    for media_type, base_media in base_content.items():
                        current_media = current_content.get(media_type)
                        if current_media is None:
                            _issue(
                                issues,
                                operation_pointer,
                                f"request media type removed: {media_type}",
                            )
                            continue
                        request_exemption = _DefaultExemption(
                            collected,
                            _baseline_permanent_placeholder(base, path_name, method, base_side),
                        )
                        _compare_schema(
                            (_mapping(base_media) or {}).get("schema", {}),
                            (_mapping(current_media) or {}).get("schema", {}),
                            f"{operation_pointer}/requestBody/{media_type}",
                            issues,
                            base_side=base_side,
                            current_side=current_side,
                            exemption=request_exemption,
                        )

            base_responses = _mapping(base_operation_mapping.get("responses")) or {}
            current_responses = _mapping(current_operation.get("responses")) or {}
            for status, base_response in base_responses.items():
                current_response = current_responses.get(status)
                response_pointer = f"{operation_pointer}/responses/{status}"
                if current_response is None:
                    _issue(issues, response_pointer, "response status removed")
                    continue
                if _exact_placeholder_503(base, path_name, method, status):
                    # The baseline declared "permanently not implemented". An
                    # unchanged placeholder must still declare the shared
                    # component's original shape; a replacement must invert that
                    # permanent-absence claim in the header value and the error
                    # code, and may not borrow another operation's response.
                    unchanged = _exact_placeholder_503(current, path_name, method, status)
                    for problem in _placeholder_503_problems(
                        _raw_response(current, path_name, method, status),
                        current,
                        current_side,
                        expected_retry="false" if unchanged else "true",
                        replacement=not unchanged,
                    ):
                        _issue(issues, response_pointer, problem)
                    continue
                base_headers = {
                    name.lower(): value
                    for name, value in (
                        _mapping((_mapping(base_response) or {}).get("headers")) or {}
                    ).items()
                }
                current_headers = {
                    name.lower(): value
                    for name, value in (
                        _mapping((_mapping(current_response) or {}).get("headers")) or {}
                    ).items()
                }
                for name, old_header in base_headers.items():
                    if name not in current_headers:
                        _issue(issues, response_pointer, f"response header removed: {name}")
                        continue
                    old_header_mapping = _mapping(old_header) or {}
                    new_header_mapping = _mapping(current_headers[name]) or {}
                    if (
                        old_header_mapping.get("required") is True
                        and new_header_mapping.get("required") is not True
                    ):
                        _issue(
                            issues, response_pointer, f"response header guarantee removed: {name}"
                        )
                    _compare_schema(
                        old_header_mapping.get("schema", {}),
                        new_header_mapping.get("schema", {}),
                        f"{response_pointer}/headers/{name}",
                        issues,
                        direction="response",
                        base_side=base_side,
                        current_side=current_side,
                    )
                base_content = _mapping((_mapping(base_response) or {}).get("content")) or {}
                current_content = _mapping((_mapping(current_response) or {}).get("content")) or {}
                for media_type, base_media in base_content.items():
                    current_media = current_content.get(media_type)
                    if current_media is None:
                        _issue(
                            issues, response_pointer, f"response media type removed: {media_type}"
                        )
                        continue
                    _compare_schema(
                        (_mapping(base_media) or {}).get("schema", {}),
                        (_mapping(current_media) or {}).get("schema", {}),
                        f"{response_pointer}/{media_type}",
                        issues,
                        direction="response",
                        base_side=base_side,
                        current_side=current_side,
                    )

    if notices is None:
        for notice in collected:
            print(f"notice: {notice}", file=sys.stderr)
    return issues


def _collect_event_types(document: object) -> set[str]:
    event_types: set[str] = set()

    def walk(node: object) -> None:
        mapping = _mapping(node)
        if mapping is not None:
            properties = _mapping(mapping.get("properties"))
            if properties is not None:
                type_schema = _mapping(properties.get("type"))
                if type_schema is not None and isinstance(type_schema.get("const"), str):
                    event_types.add(cast(str, type_schema["const"]))
            for value in mapping.values():
                walk(value)
        else:
            for value in _sequence(node) or []:
                walk(value)

    walk(document)
    return event_types


def _compare_json_schema(
    base: Mapping[str, Any],
    current: Mapping[str, Any],
    path: str,
    *,
    base_git_ref: str | None = None,
    direction: str = "request",
) -> list[str]:
    issues: list[str] = []
    _compare_schema(
        base,
        current,
        path,
        issues,
        direction=direction,
        base_side=_SchemaSide(base, path, base_git_ref, {}),
        current_side=_SchemaSide(current, path, None, {}),
    )
    removed_event_types = _collect_event_types(base) - _collect_event_types(current)
    if removed_event_types:
        _issue(issues, path, f"event types removed: {sorted(removed_event_types)}")
    return issues


def _collect_local_def_refs(node: object, references: set[str]) -> None:
    mapping = _mapping(node)
    if mapping is not None:
        ref = mapping.get("$ref")
        if isinstance(ref, str) and ref.startswith("#/$defs/"):
            references.add(ref.rsplit("/", 1)[-1])
        properties = _mapping(mapping.get("properties"))
        if properties is not None:
            for name, value in properties.items():
                if name != "nexus":
                    _collect_local_def_refs(value, references)
        for key, value in mapping.items():
            if key == "properties":
                continue
            _collect_local_def_refs(value, references)
        return
    for value in _sequence(node) or []:
        _collect_local_def_refs(value, references)


def _reachable_defs(document: Mapping[str, Any], roots: set[str]) -> set[str]:
    definitions = _mapping(document.get("$defs")) or {}
    pending = set(roots)
    seen: set[str] = set()
    while pending:
        name = pending.pop()
        if name in seen:
            continue
        seen.add(name)
        definition = definitions.get(name)
        if definition is not None:
            _collect_local_def_refs(definition, pending)
    return seen


def _compare_realtime_events_schema(
    base: Mapping[str, Any],
    current: Mapping[str, Any],
    path: str,
    *,
    base_git_ref: str | None = None,
) -> list[str]:
    issues: list[str] = []
    base_side = _SchemaSide(base, path, base_git_ref, {})
    current_side = _SchemaSide(current, path, None, {})
    base_defs = _mapping(base.get("$defs")) or {}
    current_defs = _mapping(current.get("$defs")) or {}
    standard_roots = _OPENAI_REALTIME_DEFS & set(base_defs)
    current_union_refs: set[str] = set()
    _collect_local_def_refs(current.get("oneOf"), current_union_refs)
    for name in sorted(standard_roots):
        pointer = f"{path}:$defs/{name}"
        if name not in current_union_refs:
            _issue(issues, pointer, "standard event removed from public union")
        if name not in current_defs:
            _issue(issues, pointer, "standard event definition removed")

    reachable_base = _reachable_defs(base, standard_roots)
    reachable_current = _reachable_defs(current, standard_roots)
    for name in sorted(standard_roots):
        pointer = f"{path}:$defs/{name}"
        base_definition = base_defs.get(name)
        if base_definition is None:
            continue
        if name not in current_defs:
            if name not in standard_roots:
                _issue(issues, pointer, "referenced schema definition removed")
            continue
        _compare_schema(
            base_definition,
            current_defs[name],
            pointer,
            issues,
            direction="request"
            if name
            in {
                "SessionUpdate",
                "InputAudioBufferAppend",
                "InputAudioBufferCommit",
                "InputAudioBufferClear",
            }
            else "response",
            base_side=base_side,
            current_side=current_side,
        )

    base_types = _collect_event_types(
        {"$defs": {name: base_defs[name] for name in reachable_base if name in base_defs}}
    )
    current_subset = {
        "$defs": {name: current_defs[name] for name in reachable_current if name in current_defs}
    }
    removed_event_types = base_types - _collect_event_types(current_subset)
    if removed_event_types:
        _issue(issues, path, f"standard event types removed: {sorted(removed_event_types)}")
    return issues


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", required=True, help="Git ref for the compatibility baseline")
    args = parser.parse_args()

    base_paths = set(
        subprocess.check_output(
            ["git", "-C", str(_ROOT), "ls-tree", "-r", "--name-only", args.base, "--", "contracts"],
            text=True,
        ).splitlines()
    )
    issues: list[str] = []
    for path in _PUBLIC_CONTRACTS:
        if path not in base_paths:
            print(f"skip {path} (new contract)")
            continue
        current_path = _ROOT / path
        if not current_path.exists():
            issues.append(f"{path}: contract removed")
            continue
        base_document = _mapping(_load_base(args.base, path))
        current_document = _mapping(_load_current(path))
        if base_document is None or current_document is None:
            issues.append(f"{path}: document root is not an object")
            continue
        if path.endswith("openapi.yaml"):
            issues.extend(
                _compare_openapi(
                    base_document,
                    current_document,
                    path,
                    base_git_ref=args.base,
                )
            )
        elif path.endswith("realtime-events.schema.json"):
            issues.extend(
                _compare_realtime_events_schema(
                    base_document,
                    current_document,
                    path,
                    base_git_ref=args.base,
                )
            )
        else:
            issues.extend(
                _compare_json_schema(
                    base_document,
                    current_document,
                    path,
                    base_git_ref=args.base,
                    direction="response" if path.endswith("errors.schema.json") else "request",
                )
            )

    if issues:
        print("Breaking OpenAI-compatible contract changes:", file=sys.stderr)
        for issue in issues:
            print(f"  - {issue}", file=sys.stderr)
        return 1
    print("OpenAI-compatible contract compatibility: passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
