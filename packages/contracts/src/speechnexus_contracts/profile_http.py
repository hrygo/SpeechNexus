"""Generated HTTP validation entrypoints; do not edit."""

from typing import Any

from .http_validation import contract_for, validate_http_request, validate_http_response


def contract() -> dict[str, Any]:
    return contract_for("profile")


def validate_request(operation: str, value: Any) -> Any:
    return validate_http_request("profile", operation, value)


def validate_response(operation: str, status: int, value: Any) -> Any:
    return validate_http_response("profile", operation, status, value)
