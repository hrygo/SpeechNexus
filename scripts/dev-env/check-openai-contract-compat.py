"""Reject obvious breaking changes to the public OpenAI-compatible contracts."""

from __future__ import annotations

import argparse
import posixpath
import subprocess
import sys
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any, NamedTuple, cast
from urllib.parse import unquote

import yaml

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
    schema_mapping = _mapping(schema)
    if schema_mapping is None:
        if isinstance(schema, list):
            return [_dereference_schema(item, side, seen) for item in schema]
        return schema
    ref = schema_mapping.get("$ref")
    if not isinstance(ref, str) or "://" in ref:
        if not side.standard_only and "$ref" in schema_mapping:
            raise ValueError("Nonstandard HTTP comparison requires closed repository references")
        return _dereference_schema_children(schema_mapping, side, seen)
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
    resolved = _dereference_schema(
        target,
        _SchemaSide(
            resolved_document, target_path, target_git_ref, side.documents, side.standard_only
        ),
        seen | {identity},
    )
    siblings = {key: value for key, value in schema_mapping.items() if key != "$ref"}
    if isinstance(resolved, bool) and siblings:
        # $ref siblings intersect with its target even when that target is a
        # boolean schema. Keep the reference policy before simplifying false.
        annotations = {"title", "description", "examples", "$comment"}
        if not (siblings.keys() - annotations):
            return resolved
        resolved_siblings = _dereference_schema_children(siblings, side, seen)
        return resolved_siblings if resolved else False
    resolved_mapping = _mapping(resolved)
    if siblings and resolved_mapping is not None:
        resolved_siblings = _dereference_schema_children(siblings, side, seen)
        annotations = {"title", "description", "default", "examples", "$comment"}
        conflicts = (resolved_mapping.keys() & resolved_siblings.keys()) - annotations
        if any(
            resolved_mapping[key] != resolved_siblings[key]
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


def _same_json_tree(left: object, right: object) -> bool:
    """Retain JSON types: Python's True == 1 and False == 0 are not JSON equality."""
    if type(left) is not type(right):
        return False
    if isinstance(left, Mapping) and isinstance(right, Mapping):
        return (
            all(isinstance(key, str) for key in (*left, *right))
            and left.keys() == right.keys()
            and all(_same_json_tree(value, right[key]) for key, value in left.items())
        )
    if isinstance(left, list) and isinstance(right, list):
        return len(left) == len(right) and all(
            _same_json_tree(a, b) for a, b in zip(left, right, strict=True)
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


def _compare_schema(
    base: object,
    current: object,
    pointer: str,
    issues: list[str],
    *,
    direction: str = "request",
    base_side: _SchemaSide | None = None,
    current_side: _SchemaSide | None = None,
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
        and ("const" not in accepted or constrained["const"] != accepted["const"])
        and accepted.get("enum") != [constrained["const"]]
    ):
        _issue(issues, pointer, "const value changed")

    if ("default" in base_schema) != ("default" in current_schema):
        _issue(issues, pointer, "default presence changed")
    elif "default" in base_schema and base_schema.get("default") != current_schema.get("default"):
        _issue(
            issues,
            pointer,
            f"default changed from {base_schema.get('default')!r} "
            f"to {current_schema.get('default')!r}",
        )

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
        missing = [value for value in values or [] if value not in base_enum]
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
        if base_schema.get(keyword) != current_schema.get(keyword):
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
            if _compatibility_shape(
                base_variants, standard_only=standard_only
            ) != _compatibility_shape(current_variants, standard_only=standard_only):
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
) -> list[str]:
    issues: list[str] = []
    # Each comparison owns fresh document snapshots. A process-global cache could
    # hide edits to a shared schema during a later comparison in the same process.
    base_side = _SchemaSide(base, path, base_git_ref, {}, standard_only)
    current_side = _SchemaSide(current, path, None, {}, standard_only)
    # Restrict the walk before expanding refs; the complete document stays on each
    # side so shared parameters, responses and component refs still resolve.
    base_paths = (
        _mapping(
            _dereference_schema(
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
            _dereference_schema(
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
                        _compare_schema(
                            (_mapping(base_media) or {}).get("schema", {}),
                            (_mapping(current_media) or {}).get("schema", {}),
                            f"{operation_pointer}/requestBody/{media_type}",
                            issues,
                            base_side=base_side,
                            current_side=current_side,
                        )

            base_responses = _mapping(base_operation_mapping.get("responses")) or {}
            current_responses = _mapping(current_operation.get("responses")) or {}
            for status, base_response in base_responses.items():
                current_response = current_responses.get(status)
                response_pointer = f"{operation_pointer}/responses/{status}"
                if current_response is None:
                    _issue(issues, response_pointer, "response status removed")
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
