"""Reject obvious breaking changes to the public OpenAI-compatible contracts."""

from __future__ import annotations

import argparse
import subprocess
import sys
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any, cast

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
_COMPONENT_SECTIONS = ("schemas", "parameters", "headers", "requestBodies", "responses")
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
    )
    for key, direction in checks:
        old = base.get(key)
        new = current.get(key)
        if (
            old is None
            or new is None
            or not isinstance(old, (int, float))
            or not isinstance(new, (int, float))
        ):
            continue
        if (direction == ">" and new > old) or (direction == "<" and new < old):
            return f"{key} was tightened from {old!r} to {new!r}"
    for key in ("pattern", "multipleOf"):
        if key in base and base.get(key) != current.get(key):
            return f"{key} changed from {base.get(key)!r} to {current.get(key)!r}"
    if base.get("uniqueItems") is not True and current.get("uniqueItems") is True:
        return "uniqueItems was tightened to true"
    return None


def _compare_schema(base: object, current: object, pointer: str, issues: list[str]) -> None:
    base_schema = _mapping(base)
    current_schema = _mapping(current)
    if base_schema is None or current_schema is None:
        if type(base) is not type(current):
            _issue(issues, pointer, "schema node kind changed")
        return

    base_ref = base_schema.get("$ref")
    current_ref = current_schema.get("$ref")
    if base_ref != current_ref:
        _issue(issues, pointer, f"$ref changed from {base_ref!r} to {current_ref!r}")

    base_types = _type_set(base_schema.get("type"))
    current_types = _type_set(current_schema.get("type"))
    if base_types and current_types and base_types != current_types:
        _issue(
            issues, pointer, f"type changed from {sorted(base_types)} to {sorted(current_types)}"
        )

    if "const" in base_schema and base_schema.get("const") != current_schema.get("const"):
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

    base_enum = _sequence(base_schema.get("enum"))
    current_enum = _sequence(current_schema.get("enum"))
    if base_enum is not None:
        missing = [
            value for value in base_enum if current_enum is None or value not in current_enum
        ]
        if missing:
            _issue(issues, pointer, f"enum values removed: {missing!r}")

    base_required = set(cast(Iterable[str], base_schema.get("required", [])))
    current_required = set(cast(Iterable[str], current_schema.get("required", [])))
    added_required = current_required - base_required
    if added_required:
        _issue(issues, pointer, f"new required fields: {sorted(added_required)}")

    if (
        base_schema.get("additionalProperties") is not False
        and current_schema.get("additionalProperties") is False
    ):
        _issue(issues, pointer, "additionalProperties was tightened to false")

    constraint_issue = _constraint_break(base_schema, current_schema)
    if constraint_issue is not None:
        _issue(issues, pointer, constraint_issue)

    base_properties = _mapping(base_schema.get("properties"))
    current_properties = _mapping(current_schema.get("properties"))
    if base_properties is not None:
        for name, base_property in base_properties.items():
            property_pointer = f"{pointer}/properties/{name}"
            if current_properties is None or name not in current_properties:
                _issue(issues, property_pointer, "property removed")
            else:
                _compare_schema(
                    base_property,
                    current_properties[name],
                    property_pointer,
                    issues,
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
                )

    for keyword in ("items", "if", "then", "else", "not"):
        if keyword in base_schema:
            _compare_schema(
                base_schema[keyword],
                current_schema.get(keyword),
                f"{pointer}/{keyword}",
                issues,
            )
    for keyword in ("oneOf", "anyOf", "allOf"):
        base_variants = _sequence(base_schema.get(keyword))
        current_variants = _sequence(current_schema.get(keyword))
        if base_variants is None:
            continue
        if current_variants is None or len(current_variants) != len(base_variants):
            _issue(issues, f"{pointer}/{keyword}", "variant count changed")
            continue
        for index, variant in enumerate(base_variants):
            _compare_schema(
                variant,
                current_variants[index],
                f"{pointer}/{keyword}/{index}",
                issues,
            )


def _compare_parameters(
    base_parameters: object,
    current_parameters: object,
    pointer: str,
    issues: list[str],
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
        )


def _collect_local_component_refs(node: object, references: set[tuple[str, str]]) -> None:
    mapping = _mapping(node)
    if mapping is not None:
        ref = mapping.get("$ref")
        if isinstance(ref, str) and ref.startswith("#/components/"):
            parts = ref.split("/")
            if len(parts) == 4 and parts[2] in _COMPONENT_SECTIONS:
                references.add((parts[2], parts[3]))
        properties = _mapping(mapping.get("properties"))
        if properties is not None:
            for name, value in properties.items():
                # `nexus` is the explicitly optional enhancement namespace.
                # Its components are not part of the OpenAI compatibility
                # promise even when nested inside a standard request or model.
                if name != "nexus":
                    _collect_local_component_refs(value, references)
        for key, value in mapping.items():
            if key == "properties":
                continue
            _collect_local_component_refs(value, references)
        return
    for value in _sequence(node) or []:
        _collect_local_component_refs(value, references)


def _reachable_components(document: Mapping[str, Any]) -> dict[str, set[str]]:
    paths = _mapping(document.get("paths")) or {}
    components = _mapping(document.get("components")) or {}
    pending: set[tuple[str, str]] = set()
    for path_name, path_item in paths.items():
        if path_name in _OPENAI_PATHS:
            _collect_local_component_refs(path_item, pending)

    seen: set[tuple[str, str]] = set()
    while pending:
        section, name = pending.pop()
        if (section, name) in seen:
            continue
        seen.add((section, name))
        section_values = _mapping(components.get(section)) or {}
        component = section_values.get(name)
        if component is not None:
            _collect_local_component_refs(component, pending)

    reachable: dict[str, set[str]] = {section: set() for section in _COMPONENT_SECTIONS}
    for section, name in seen:
        reachable[section].add(name)
    return reachable


def _compare_openapi(base: Mapping[str, Any], current: Mapping[str, Any], path: str) -> list[str]:
    issues: list[str] = []
    base_paths = _mapping(base.get("paths")) or {}
    current_paths = _mapping(current.get("paths")) or {}
    for path_name, base_path_item in base_paths.items():
        if path_name not in _OPENAI_PATHS:
            continue
        current_path_item = current_paths.get(path_name)
        if current_path_item is None:
            _issue(issues, f"{path}:paths/{path_name}", "path removed")
            continue
        base_path_mapping = _mapping(base_path_item) or {}
        current_path_mapping = _mapping(current_path_item) or {}
        _compare_parameters(
            base_path_mapping.get("parameters", []),
            current_path_mapping.get("parameters", []),
            f"{path}:paths/{path_name}",
            issues,
        )
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
                base_operation_mapping.get("parameters", []),
                current_operation.get("parameters", []),
                operation_pointer,
                issues,
            )

            base_body = _mapping(base_operation_mapping.get("requestBody"))
            current_body = _mapping(current_operation.get("requestBody"))
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
                        )

            base_responses = _mapping(base_operation_mapping.get("responses")) or {}
            current_responses = _mapping(current_operation.get("responses")) or {}
            for status, base_response in base_responses.items():
                current_response = current_responses.get(status)
                response_pointer = f"{operation_pointer}/responses/{status}"
                if current_response is None:
                    _issue(issues, response_pointer, "response status removed")
                    continue
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
                    )

    base_components = _mapping(base.get("components")) or {}
    current_components = _mapping(current.get("components")) or {}
    reachable = _reachable_components(base)
    base_schemas = _mapping(base_components.get("schemas")) or {}
    current_schemas = _mapping(current_components.get("schemas")) or {}
    for name in sorted(reachable["schemas"]):
        base_schema = base_schemas.get(name)
        if base_schema is None:
            continue
        component_pointer = f"{path}:components/schemas/{name}"
        if name not in current_schemas:
            _issue(issues, component_pointer, "component schema removed")
        else:
            _compare_schema(base_schema, current_schemas[name], component_pointer, issues)

    for section in ("parameters", "headers"):
        base_section = _mapping(base_components.get(section)) or {}
        current_section = _mapping(current_components.get(section)) or {}
        for name in sorted(reachable[section]):
            base_value = base_section.get(name)
            if base_value is None:
                continue
            component_pointer = f"{path}:components/{section}/{name}"
            if name not in current_section:
                _issue(issues, component_pointer, f"component {section} entry removed")
                continue
            base_mapping = _mapping(base_value) or {}
            current_mapping = _mapping(current_section[name]) or {}
            if section == "parameters":
                _compare_parameters([base_mapping], [current_mapping], component_pointer, issues)
            else:
                _compare_schema(
                    base_mapping.get("schema", {}),
                    current_mapping.get("schema", {}),
                    component_pointer,
                    issues,
                )

    for section in ("requestBodies", "responses"):
        base_section = _mapping(base_components.get(section)) or {}
        current_section = _mapping(current_components.get(section)) or {}
        for name in sorted(reachable[section]):
            base_value = base_section.get(name)
            if base_value is None:
                continue
            component_pointer = f"{path}:components/{section}/{name}"
            if name not in current_section:
                _issue(issues, component_pointer, f"component {section} entry removed")
                continue
            base_content = _mapping((_mapping(base_value) or {}).get("content")) or {}
            current_content = _mapping((_mapping(current_section[name]) or {}).get("content")) or {}
            for media_type, base_media in base_content.items():
                media_pointer = f"{component_pointer}/{media_type}"
                if media_type not in current_content:
                    _issue(issues, media_pointer, "media type removed")
                    continue
                _compare_schema(
                    (_mapping(base_media) or {}).get("schema", {}),
                    (_mapping(current_content[media_type]) or {}).get("schema", {}),
                    media_pointer,
                    issues,
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
    base: Mapping[str, Any], current: Mapping[str, Any], path: str
) -> list[str]:
    issues: list[str] = []
    _compare_schema(base, current, path, issues)
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
    base: Mapping[str, Any], current: Mapping[str, Any], path: str
) -> list[str]:
    issues: list[str] = []
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
    for name in sorted(reachable_base):
        pointer = f"{path}:$defs/{name}"
        base_definition = base_defs.get(name)
        if base_definition is None:
            continue
        if name not in current_defs:
            if name not in standard_roots:
                _issue(issues, pointer, "referenced schema definition removed")
            continue
        _compare_schema(base_definition, current_defs[name], pointer, issues)

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
            issues.extend(_compare_openapi(base_document, current_document, path))
        elif path.endswith("realtime-events.schema.json"):
            issues.extend(_compare_realtime_events_schema(base_document, current_document, path))
        else:
            issues.extend(_compare_json_schema(base_document, current_document, path))

    if issues:
        print("Breaking OpenAI-compatible contract changes:", file=sys.stderr)
        for issue in issues:
            print(f"  - {issue}", file=sys.stderr)
        return 1
    print("OpenAI-compatible contract compatibility: passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
