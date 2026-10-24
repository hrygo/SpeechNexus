"""Offline HTTP envelopes derived from source operations, without body coercion."""

from __future__ import annotations

import json
import re
from functools import lru_cache
from importlib.resources import files
from typing import Any, cast

from .profile_registry import HTTP_PROFILES
from .validation import ContractValidationError, _validators

MAX_SAFE_INTEGER = 2**53 - 1


@lru_cache
def _bundle(profile: str) -> dict[str, Any]:
    if profile not in HTTP_PROFILES:
        raise ValueError("Unknown HTTP contract profile")
    return cast(
        dict[str, Any],
        json.loads(files(__package__).joinpath(f"{profile}-http.schema.json").read_text()),
    )


def contract_for(profile: str) -> dict[str, Any]:
    """Return an isolated document; importing it never registers application routes."""
    _bundle(profile)
    return cast(
        dict[str, Any],
        json.loads(files(__package__).joinpath(f"{profile}.openapi.json").read_text()),
    )


def _operation(profile: str, operation: str) -> dict[str, Any]:
    metadata = _bundle(profile)["x-nexus-http-operations"]
    if operation not in metadata:
        raise ValueError("Unknown HTTP operation")
    return cast(dict[str, Any], metadata[operation])


def _normalize(value: Any, types: dict[str, dict[str, str]], message: str) -> Any:
    if not isinstance(value, dict):
        return value
    result = value.copy()
    for location, fields in types.items():
        group = value.get(location)
        if not isinstance(group, dict):
            continue
        normalized: dict[str, Any] = {}
        for key, item in group.items():
            if not isinstance(key, str):
                raise ContractValidationError(message)
            name = key.lower() if location == "headers" else key
            if name in normalized:
                raise ContractValidationError(message)
            if fields.get(name) == "integer":
                # HTTP scalar serialization only. JSON bodies never pass through this code.
                if (
                    isinstance(item, str)
                    and len(item) <= 17
                    and re.fullmatch(r"-?(0|[1-9][0-9]*)", item)
                ):
                    item = int(item)
                if (
                    isinstance(item, (int, float))
                    and not isinstance(item, bool)
                    and not -MAX_SAFE_INTEGER <= item <= MAX_SAFE_INTEGER
                ):
                    raise ContractValidationError(message)
            normalized[name] = item
        result[location] = normalized
    return result


def validate_http_request(profile: str, operation: str, value: Any) -> Any:
    metadata = _operation(profile, operation)
    normalized = _normalize(value, metadata["parameters"], "Invalid HTTP request")
    validator = _validators(f"{profile}-http.schema.json")[metadata["request"]]
    if not validator.is_valid(normalized):
        raise ContractValidationError("Invalid HTTP request")
    return value


def validate_http_response(profile: str, operation: str, status: int, value: Any) -> Any:
    operation_metadata = _operation(profile, operation)
    if type(status) is not int or str(status) not in operation_metadata["responses"]:
        raise ContractValidationError("Invalid HTTP response")
    metadata = operation_metadata["responses"][str(status)]
    normalized = _normalize(value, {"headers": metadata["headers"]}, "Invalid HTTP response")
    validator = _validators(f"{profile}-http.schema.json")[metadata["schema"]]
    if not validator.is_valid(normalized):
        raise ContractValidationError("Invalid HTTP response")
    return value
