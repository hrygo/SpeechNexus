"""Bounded SSE framing; no network, reconnect, resource mutation or EOF success claim."""

from __future__ import annotations

import json
from collections.abc import Callable, Generator, Iterable
from typing import Any

from .validation import ContractValidationError


def iter_sse(
    chunks: Iterable[bytes],
    validate: Callable[[Any], Any],
    path: dict[str, str],
    cursor: str | None,
    max_event_bytes: int,
) -> Generator[Any]:
    if type(max_event_bytes) is not int or not 0 < max_event_bytes <= 2**53 - 1:
        raise ValueError("A positive safe SSE byte limit is required")
    line = bytearray()
    data: list[str] = []
    fields: dict[str, str] = {}
    size, first, after_cr = 0, True, False
    previous = int(cursor) if cursor is not None else None
    source = iter(chunks)

    def accept_line() -> Any:
        nonlocal size, first, previous
        text = line.decode("utf-8", errors="strict")
        line.clear()
        if first:
            text = text.removeprefix("\ufeff")
            first = False
        if text:
            if text.startswith(":"):
                return None
            key, separator, value = text.partition(":")
            if separator and value.startswith(" "):
                value = value[1:]
            if key == "data":
                data.append(value)
            elif key in ("id", "event"):
                fields[key] = value
            return None
        size = 0
        if not data:
            fields.clear()
            return None
        value = validate(json.loads("\n".join(data)))
        for key, expected in path.items():
            if value.get(key) != expected:
                raise ValueError("Wrong event resource")
        if ("id" in fields and fields["id"] != value["id"]) or (
            "event" in fields and fields["event"] != value["type"]
        ):
            raise ValueError("Wrong SSE identity")
        number = int(value["id"])
        if previous is not None and number <= previous:
            raise ValueError("Non-increasing SSE sequence")
        previous = number
        data.clear()
        fields.clear()
        return value

    try:
        for chunk in source:
            if not isinstance(chunk, (bytes, bytearray, memoryview)):
                raise ValueError("Invalid SSE bytes")
            for byte in chunk:
                if after_cr:
                    after_cr = False
                    if byte == 10:
                        continue
                # A CR reserves its optional following LF before dispatch.
                size += 2 if byte == 13 else 1
                if size > max_event_bytes:
                    raise ValueError("SSE event exceeds limit")
                if byte in (10, 13):
                    after_cr = byte == 13
                    value = accept_line()
                    if value is not None:
                        yield value
                else:
                    line.append(byte)
        if line or data:
            raise ValueError("Incomplete SSE event")
    except Exception:
        raise ContractValidationError("Invalid SSE stream") from None
    finally:
        close = getattr(source, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                raise ContractValidationError("Invalid SSE stream") from None
