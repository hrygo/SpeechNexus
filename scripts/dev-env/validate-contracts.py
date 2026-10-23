from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml
from jsonschema.validators import validator_for
from openapi_spec_validator import validate


def load_yaml(path: Path) -> Any:
    with path.open(encoding="utf-8") as stream:
        document = yaml.safe_load(stream)
    if not isinstance(document, Mapping):
        raise ValueError("document root must be a mapping")
    return dict(document)


def resolve_local_refs(node: Any, source: Path) -> None:
    """Turn relative file references into absolute file URIs for validation.

    Contracts are stored as separate files and are consumed by generators that
    resolve relative references against the document location. The validator
    receives an in-memory mapping, so it needs the absolute URI to perform the
    same resolution without relying on the current working directory.
    """
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str) and not ref.startswith(("#", "http://", "https://", "file://")):
            path_part, separator, fragment = ref.partition("#")
            if path_part:
                target = (source.parent / path_part).resolve()
                node["$ref"] = target.as_uri() + (f"#{fragment}" if separator else "")
        for value in node.values():
            resolve_local_refs(value, source)
    elif isinstance(node, list):
        for value in node:
            resolve_local_refs(value, source)


def resolve_json_pointer(document: Any, fragment: str) -> Any:
    """Resolve an RFC 6901 fragment and fail when the target does not exist."""
    if fragment in {"", "/"}:
        return document
    if not fragment.startswith("/"):
        raise ValueError(f"unsupported reference fragment: #{fragment}")

    current = document
    for raw_token in fragment[1:].split("/"):
        token = raw_token.replace("~1", "/").replace("~0", "~")
        if isinstance(current, Mapping):
            if token not in current:
                raise ValueError(f"reference pointer #{fragment} is missing key {token!r}")
            current = current[token]
        elif isinstance(current, list):
            if not token.isdigit() or int(token) >= len(current):
                raise ValueError(f"reference pointer #{fragment} has invalid index {token!r}")
            current = current[int(token)]
        else:
            raise ValueError(f"reference pointer #{fragment} traverses a scalar")
    return current


def validate_local_references(node: Any, source: Path, root: Path, document: Any) -> None:
    """Verify that every local file reference points at an existing fragment."""
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str) and not ref.startswith(("http://", "https://", "file://")):
            path_part, separator, fragment = ref.partition("#")
            target_document = document
            if path_part:
                target = (source.parent / path_part).resolve()
                try:
                    target.relative_to(root)
                except ValueError as error:
                    message = f"{source}: reference escapes contracts root: {ref}"
                    raise ValueError(message) from error
                target_document = load_yaml(target)
            pointer = fragment if separator else ""
            resolve_json_pointer(target_document, pointer)
        for value in node.values():
            validate_local_references(value, source, root, document)
    elif isinstance(node, list):
        for value in node:
            validate_local_references(value, source, root, document)


def validate_contract(path: Path, root: Path) -> None:
    relative = path.relative_to(root)
    document = load_yaml(path)
    validate_local_references(document, path, root, document)
    resolve_local_refs(document, path)
    if path.suffix in {".yaml", ".yml"}:
        validate(document)
        print(f"ok   OpenAPI {relative}")
        return

    validator_for(document).check_schema(document)
    print(f"ok   JSON Schema {relative}")


def main() -> None:
    root = Path(__file__).resolve().parents[2]
    contracts = root / "contracts"
    paths = sorted(
        path
        for path in contracts.glob("**/*")
        if path.is_file()
        and (
            path.name.endswith(("openapi.yaml", "openapi.yml"))
            or path.name.endswith(".schema.json")
        )
    )
    if not paths:
        print("skip contracts (none yet)")
        return
    for path in paths:
        validate_contract(path, root)
    print(f"contract validation: passed ({len(paths)} files)")


if __name__ == "__main__":
    main()
