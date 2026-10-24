"""Private configuration JSON v1; lossless integers, with no public digest protocol."""

from __future__ import annotations

import json
import math
from collections.abc import Iterator
from decimal import Decimal
from typing import Any

from speechnexus_backend.speech.domain.binding_drafts import CreateBindingDraft
from speechnexus_backend.speech.domain.configuration import MAX_DOCUMENT_BYTES, document_digest


def _integer_text(value: int) -> str:
    if value.bit_length() > MAX_DOCUMENT_BYTES * 4:
        raise ValueError("Binding integer cannot fit the bounded document")
    try:
        return str(value)
    except ValueError:
        # The interpreter's global int/string guard is not a source wire bound.
        # Decimal converts exact integers without changing that process-wide guard.
        return format(Decimal(value), "f")


def parse_integer(value: str) -> int:
    try:
        return int(value)
    except ValueError:
        return int(Decimal(value))


def _encoded(value: Any, depth: int = 0) -> Iterator[str]:
    if depth > 64:
        raise ValueError("Binding document is too deep")
    if value is None:
        yield "null"
    elif type(value) is bool:
        yield "true" if value else "false"
    elif type(value) is int:
        yield _integer_text(value)
    elif type(value) is float:
        if not math.isfinite(value):
            raise ValueError("Binding numbers must be finite")
        yield (
            _integer_text(int(value)) if value.is_integer() else json.dumps(value, allow_nan=False)
        )
    elif isinstance(value, str):
        if len(value) > MAX_DOCUMENT_BYTES:
            raise ValueError("Binding string cannot fit the bounded document")
        yield json.dumps(value, ensure_ascii=False)
    elif isinstance(value, list):
        yield "["
        for index, item in enumerate(value):
            if index:
                yield ","
            yield from _encoded(item, depth + 1)
        yield "]"
    elif isinstance(value, dict) and all(isinstance(key, str) for key in value):
        yield "{"
        for index, key in enumerate(sorted(value)):
            if index:
                yield ","
            yield from _encoded(key, depth + 1)
            yield ":"
            yield from _encoded(value[key], depth + 1)
        yield "}"
    else:
        raise ValueError("Binding documents require JSON values")


def encode_json(value: Any, *, max_bytes: int) -> bytes:
    """Serialize bounded source JSON with exact integers, including HTTP projections."""
    if type(max_bytes) is not int or not 1 <= max_bytes <= 1048576:
        raise ValueError("Invalid JSON byte limit")
    try:
        raw = bytearray()
        for piece in _encoded(value):
            chunk = piece.encode("utf-8")
            if len(raw) + len(chunk) > max_bytes:
                raise ValueError
            raw.extend(chunk)
        return bytes(raw)
    except ValueError, TypeError, RecursionError, OverflowError:
        raise ValueError("Invalid binding document") from None


def encode_document(value: Any) -> bytes:
    result = encode_json(value, max_bytes=MAX_DOCUMENT_BYTES)
    document_digest(result)
    return result


def decode_document(raw: bytes) -> dict[str, Any]:
    try:
        document_digest(raw)
        value = json.loads(raw, parse_int=parse_integer)
        if not isinstance(value, dict) or encode_document(value) != raw:
            raise ValueError
        return value
    except ValueError, TypeError, RecursionError, OverflowError:
        raise ValueError("Invalid stored binding document") from None


def check_command(command: CreateBindingDraft) -> None:
    intent = decode_document(command.intent)
    if (
        intent.pop("binding_id") != command.binding_id
        or encode_document(intent) != command.definition
    ):
        raise ValueError("Inconsistent binding command")
