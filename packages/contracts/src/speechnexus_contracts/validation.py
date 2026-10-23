"""Offline, non-coercing validation of generated file workflow schemas."""

from __future__ import annotations

import json
from functools import lru_cache
from importlib.resources import files
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker

from .profile_registry import PROFILES


class ContractValidationError(ValueError):
    """Safe boundary error: excludes input content and validator diagnostics."""


@lru_cache
def _validators(filename: str = "file.schema.json") -> dict[str, Draft202012Validator]:
    bundle = json.loads(files(__package__).joinpath(filename).read_text())
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


def validate_coordination(name: str, value: Any) -> Any:
    """Internal Job port shapes, with the same offline/no-coercion guarantee."""
    validators = _validators("coordination.schema.json")
    if name not in validators:
        raise ValueError(f"Unknown coordination contract: {name}")
    if not validators[name].is_valid(value):
        raise ContractValidationError(f"Invalid {name}")
    return value


def validate_profile(profile: str, name: str, value: Any) -> Any:
    """Explicit, offline producer/consumer namespace; no filesystem user input."""
    if profile not in PROFILES:
        raise ValueError("Unknown contract profile")
    validators = _validators(f"{profile}.schema.json")
    if name not in validators:
        raise ValueError("Unknown profile contract")
    if not validators[name].is_valid(value):
        raise ContractValidationError(f"Invalid {name}")
    return value
