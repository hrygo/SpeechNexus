"""Private configuration JSON v1; lossless integers, with no public digest protocol."""

from __future__ import annotations

import json
import math
from collections.abc import Iterator, Mapping
from decimal import Decimal
from typing import Any

from speechnexus_contracts.binding_defaults_producer import parse as parse_default_intent

from speechnexus_backend.speech.application.binding_default_rules import validate_declared_defaults
from speechnexus_backend.speech.domain.binding_drafts import (
    BindingDefinitionIdentityError,
    CreateBindingDraft,
)
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


def command_defaults(intent: dict[str, Any]) -> bytes | None:
    return encode_document(intent["parameter_defaults"]) if "parameter_defaults" in intent else None


def require_published_identity(definition: Mapping[str, Any], binding_id: str) -> None:
    """Refuse to publish a stored document that does not name its own binding.

    A published revision is read as a contract ``ModelBindingRevision``, whose
    ``binding_id`` is required. Draft documents written before this build retain
    the identity only in columns; publishing one would publish a document that
    names no binding, so the write path refuses it instead of deferring the
    failure to every catalogue read.
    """
    if definition.get("binding_id") != binding_id:
        raise BindingDefinitionIdentityError(binding_id)


def same_document(stored: bytes, expected: bytes) -> bool:
    """True when ``stored`` is ``expected`` or its pre-identity legacy shape.

    Replay compares the original command document with the one the store holds.
    A revision written before identity was retained differs only by that absent
    field; replay must not turn that into an unknown result. Column identity is
    compared separately, so this comparison is deliberately identity-neutral.
    """
    if stored == expected:
        return True
    try:
        left, right = decode_document(stored), decode_document(expected)
    except ValueError:
        return False
    left.pop("binding_id", None)
    right.pop("binding_id", None)
    return left == right


def check_command(command: CreateBindingDraft) -> None:
    intent = decode_document(command.intent)
    parameters = command_defaults(intent)
    if parameters is not None:
        # Saved producer intents are untrusted on recovery as well as on entry.
        parse_default_intent("BindingDefaultsCreateRequest", intent)
    if parameters != command.parameter_defaults:
        raise ValueError("Inconsistent binding default declaration")
    intent.pop("parameter_defaults", None)
    if intent.pop("binding_id") != command.binding_id:
        raise ValueError("Inconsistent binding command identity")
    definition = decode_document(command.definition)
    declared = definition.pop("binding_id", None)
    if declared is not None and declared != command.binding_id:
        raise ValueError("Stored binding document names another binding")
    if encode_document(definition) != encode_document(intent):
        raise ValueError("Inconsistent binding command")
    if parameters is not None:
        validate_declared_defaults(intent, decode_document(parameters))
