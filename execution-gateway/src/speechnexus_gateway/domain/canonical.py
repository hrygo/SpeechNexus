"""RFC 8785 canonical JSON and SHA-256 digests for execution commands.

The gateway compares a submitted ``command_digest`` with the canonical form of
the submitted command, so every implementation must agree on exactly one byte
sequence for the same logical command. This module canonicalizes the JSON
subset the internal execution command uses: objects, arrays, strings,
integers, finite numbers, booleans and ``null``.

Values outside that subset raise :class:`UnsupportedCanonicalJsonError`. Producing a
digest for a value this module cannot canonicalize exactly would let two
implementations disagree while both claiming to follow RFC 8785.
"""

from __future__ import annotations

import hashlib
import math
from typing import Final

type JsonScalar = str | int | float | bool | None
type JsonValue = JsonScalar | list[JsonValue] | dict[str, JsonValue]

DIGEST_PREFIX: Final = "sha256:"
_MAX_SAFE_INTEGER: Final = 2**53 - 1
_ESCAPES: Final = {
    '"': '\\"',
    "\\": "\\\\",
    "\b": "\\b",
    "\f": "\\f",
    "\n": "\\n",
    "\r": "\\r",
    "\t": "\\t",
}


class UnsupportedCanonicalJsonError(ValueError):
    """A value RFC 8785 cannot canonicalize unambiguously in this subset."""


def canonical_json(value: JsonValue) -> str:
    """Return the RFC 8785 canonical serialization of ``value``."""
    return _encode(value)


def command_digest(command: JsonValue) -> str:
    """Return the ``sha256:<hex>`` digest of the canonical ``command``."""
    return DIGEST_PREFIX + hashlib.sha256(canonical_json(command).encode("utf-8")).hexdigest()


def _encode(value: JsonValue) -> str:
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, str):
        return _encode_string(value)
    if isinstance(value, int):
        return _encode_integer(value)
    if isinstance(value, float):
        return _encode_number(value)
    if isinstance(value, list):
        return "[" + ",".join(_encode(item) for item in value) + "]"
    if isinstance(value, dict):
        return _encode_object(value)
    raise UnsupportedCanonicalJsonError(f"unsupported canonical JSON value: {type(value).__name__}")


def _encode_integer(value: int) -> str:
    if abs(value) > _MAX_SAFE_INTEGER:
        raise UnsupportedCanonicalJsonError("integer outside the IEEE-754 safe integer range")
    return str(value)


def _encode_object(value: dict[str, JsonValue]) -> str:
    for key in value:
        if not isinstance(key, str):
            raise UnsupportedCanonicalJsonError("object keys must be strings")
    # RFC 8785 orders keys by UTF-16 code units, not by Unicode code points.
    ordered = sorted(value, key=lambda key: key.encode("utf-16-be"))
    members = ",".join(f"{_encode_string(key)}:{_encode(value[key])}" for key in ordered)
    return "{" + members + "}"


def _encode_string(value: str) -> str:
    chunks = ['"']
    for char in value:
        escape = _ESCAPES.get(char)
        if escape is not None:
            chunks.append(escape)
        elif char < "\u0020":
            chunks.append(f"\\u{ord(char):04x}")
        else:
            chunks.append(char)
    chunks.append('"')
    return "".join(chunks)


def _encode_number(value: float) -> str:
    """Serialize a finite float the way ECMAScript ``Number::toString`` does."""
    if not math.isfinite(value):
        raise UnsupportedCanonicalJsonError("NaN and infinity have no canonical JSON form")
    if value == 0:
        return "0"
    sign = "-" if value < 0 else ""
    digits, exponent = _shortest_digits(abs(value))
    count = len(digits)
    if count <= exponent <= 21:
        return sign + digits + "0" * (exponent - count)
    if 0 < exponent <= 21:
        return sign + digits[:exponent] + "." + digits[exponent:]
    if -6 < exponent <= 0:
        return sign + "0." + "0" * (-exponent) + digits
    lead = digits[0] if count == 1 else digits[0] + "." + digits[1:]
    return f"{sign}{lead}e{'+' if exponent - 1 >= 0 else '-'}{abs(exponent - 1)}"


def _shortest_digits(value: float) -> tuple[str, int]:
    """Return shortest round-trip digits and the decimal point position."""
    text = repr(value)
    mantissa, _, exponent_text = text.partition("e")
    exponent = int(exponent_text) if exponent_text else 0
    integer_part, _, fraction_part = mantissa.partition(".")
    combined = integer_part + fraction_part
    leading_zeros = len(combined) - len(combined.lstrip("0"))
    digits = combined.lstrip("0").rstrip("0") or "0"
    point_position = len(integer_part) + exponent - leading_zeros
    return digits, point_position
