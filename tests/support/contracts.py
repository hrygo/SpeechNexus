"""Resolve repository contracts without network access, including OpenAPI refs."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any, cast
from urllib.parse import unquote, urlparse

from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

from tests.support.safe_yaml import load_yaml

ROOT = Path(__file__).resolve().parents[2] / "contracts"


def document(path: Path) -> Any:
    path = path.resolve()
    path.relative_to(ROOT)
    node = load_yaml(path.read_text())

    def rewrite(value: Any) -> None:
        if isinstance(value, dict):
            ref = value.get("$ref")
            if isinstance(ref, str) and not ref.startswith(("#", "http:", "https:", "file:")):
                target, separator, fragment = ref.partition("#")
                value["$ref"] = (path.parent / target).resolve().as_uri()
                if separator:
                    value["$ref"] += "#" + fragment
            for child in value.values():
                rewrite(child)
        elif isinstance(value, list):
            for child in value:
                rewrite(child)

    rewrite(node)
    return node


def retrieve(uri: str) -> Resource[Any]:
    parsed = urlparse(uri)
    if parsed.scheme != "file":
        raise ValueError("contract tests never fetch remote resources")
    return Resource.from_contents(
        document(Path(unquote(parsed.path))), default_specification=DRAFT202012
    )


@lru_cache
def validator(name: str, definition: str | None = None) -> Draft202012Validator:
    schema = document(ROOT / name)
    if definition is not None:
        schema.pop("oneOf", None)
        schema = {**schema, **schema["$defs"][definition]}
    return Draft202012Validator(
        schema, registry=cast(Any, Registry)(retrieve=retrieve), format_checker=FormatChecker()
    )
