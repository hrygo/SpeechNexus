"""Shared private deployment-file checks; no runtime authority or side effects."""

from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import Any


def absolute_path(value: str) -> Path:
    path = Path(value)
    if not path.is_absolute() or "\x00" in value:
        raise ValueError
    return path


def private_file(path: Path, limit: int) -> bytes:
    descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC)
    with os.fdopen(descriptor, "rb") as source:
        info = os.fstat(source.fileno())
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_mode & 0o077
            or not 0 < info.st_size <= limit
        ):
            raise ValueError
        content = source.read(limit + 1)
        if not 0 < len(content) <= limit:
            raise ValueError
        return content


def unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def nonfinite(_: str) -> Any:
    raise ValueError


def explicit_fields(value: dict[str, Any], names: set[str]) -> dict[str, Any]:
    if set(value) != names:
        raise ValueError
    return value
