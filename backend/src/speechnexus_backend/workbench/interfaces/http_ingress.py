"""Bounded HTTP JSON intake; wire validation remains in the generated contract."""

from __future__ import annotations

import asyncio
import json
import math
from typing import Any
from uuid import uuid4

from speechnexus_contracts import ContractValidationError
from speechnexus_contracts.workbench_http import validate_request
from starlette.requests import ClientDisconnect, Request


class IngressError(Exception):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def request_id(request: Request) -> str:
    values = request.headers.getlist("x-request-id")
    if len(values) == 1:
        value = values[0]
        if 1 <= len(value) <= 128 and all(32 <= ord(c) <= 126 for c in value):
            return value
    return uuid4().hex


def default_limit(document: dict[str, Any], operation: dict[str, Any]) -> int:
    """The generated limit default for one list operation, never a guessed number."""
    for param in operation["parameters"]:
        if "$ref" in param:
            param = document["components"]["parameters"][param["$ref"].rsplit("/", 1)[1]]
        if param["in"] == "query" and param["name"] == "limit":
            value = param["schema"]["default"]
            if type(value) is not int:
                raise ValueError("invalid generated pagination default")
            return value
    raise ValueError("missing generated pagination default")


def validate_limits(max_json_bytes: int, body_timeout_seconds: float) -> None:
    if type(max_json_bytes) is not int or max_json_bytes < 1:
        raise ValueError("invalid workbench HTTP byte limit")
    if (
        type(body_timeout_seconds) not in (int, float)
        or not math.isfinite(body_timeout_seconds)
        or body_timeout_seconds <= 0
    ):
        raise ValueError("invalid workbench HTTP body deadline")


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _constant(value: str) -> Any:
    raise ValueError("non-finite JSON number")


def _float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError("non-finite JSON number")
    return parsed


async def read_envelope(
    request: Request,
    operation: str,
    *,
    has_body: bool,
    max_json_bytes: int,
    body_timeout_seconds: float,
) -> dict[str, Any]:
    for name in (
        "x-request-id",
        "idempotency-key",
        "content-type",
        "content-length",
        "content-encoding",
    ):
        values = request.headers.getlist(name)
        if len(values) > 1 or any(any(ord(c) < 32 or ord(c) == 127 for c in v) for v in values):
            raise IngressError("malformed_request")
    length = request.headers.get("content-length")
    if length is not None:
        if not length.isascii() or not length.isdecimal():
            raise IngressError("malformed_request")
        if len(length) > 20 or int(length) > max_json_bytes:
            raise IngressError("request_too_large")
    if has_body:
        parts = [p.strip().lower() for p in request.headers.get("content-type", "").split(";")]
        if (
            parts[0] != "application/json"
            or len(parts) > 2
            or (
                len(parts) == 2
                and parts[1].replace('"', "") not in ("charset=utf-8", "charset=utf8")
            )
            or request.headers.get("content-encoding", "identity").lower() != "identity"
        ):
            raise IngressError("unsupported_media_type")
    query: dict[str, str] = {}
    for key, value in request.query_params.multi_items():
        if key in query:
            raise IngressError("malformed_request")
        query[key] = value
    envelope: dict[str, Any] = {
        "path": dict(request.path_params),
        "query": query,
        "headers": {
            k: request.headers[k]
            for k in ("x-request-id", "idempotency-key")
            if k in request.headers
        },
    }
    data = bytearray()
    try:
        async with asyncio.timeout(body_timeout_seconds):
            async for chunk in request.stream():
                if len(data) + len(chunk) > max_json_bytes:
                    raise IngressError("request_too_large")
                data.extend(chunk)
    except TimeoutError:
        raise IngressError("request_timeout") from None
    except ClientDisconnect:
        raise IngressError("malformed_request") from None
    if has_body:
        try:
            body = json.loads(
                data.decode("utf-8"),
                object_pairs_hook=_pairs,
                parse_constant=_constant,
                parse_float=_float,
            )
            # Reject lone surrogates before domain persistence or response serialization.
            json.dumps(body, ensure_ascii=False, allow_nan=False).encode("utf-8")
        except ValueError, UnicodeError, RecursionError:
            raise IngressError("malformed_request") from None
        envelope["body"] = body
    elif data:
        raise IngressError("invalid_request")
    try:
        validate_request(operation, envelope)
    except ContractValidationError:
        raise IngressError("invalid_request") from None
    return envelope
