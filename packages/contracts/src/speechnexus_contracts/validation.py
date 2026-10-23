"""Offline, non-coercing validation of generated file workflow schemas."""

from __future__ import annotations

import json
from functools import lru_cache
from importlib.resources import files
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker


class ContractValidationError(ValueError):
    """Safe boundary error: excludes input content and validator diagnostics."""


@lru_cache
def _validators() -> dict[str, Draft202012Validator]:
    bundle = json.loads(files(__package__).joinpath("file.schema.json").read_text())
    return {
        name: Draft202012Validator(
            {"$ref": f"#/$defs/{name}", "$defs": bundle["$defs"]},
            format_checker=FormatChecker(),
        )
        for name in bundle["x-nexus-entrypoints"]
    }


def validate(name: str, value: Any) -> Any:
    """Validate without adding defaults, removing fields, or changing scalar types."""
    validators = _validators()
    if name not in validators:
        raise ValueError(f"Unknown file contract: {name}")
    if not validators[name].is_valid(value):
        raise ContractValidationError(f"Invalid {name}")
    return value
