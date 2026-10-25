"""Generated HTTP validation entrypoints; do not edit."""

from collections.abc import Generator, Iterable
from typing import Any

from .http_validation import (
    contract_for,
    iter_http_events,
    validate_http_event,
    validate_http_request,
    validate_http_response,
)


def contract() -> dict[str, Any]:
    return contract_for("workbench")


def validate_request(operation: str, value: Any) -> Any:
    return validate_http_request("workbench", operation, value)


def validate_response(operation: str, status: int, value: Any) -> Any:
    return validate_http_response("workbench", operation, status, value)


def validate_event(operation: str, value: Any) -> Any:
    return validate_http_event("workbench", operation, value)


def iter_events(
    operation: str, chunks: Iterable[bytes], request: Any, *, max_event_bytes: int
) -> Generator[Any]:
    return iter_http_events(
        "workbench", operation, chunks, request, max_event_bytes=max_event_bytes
    )
