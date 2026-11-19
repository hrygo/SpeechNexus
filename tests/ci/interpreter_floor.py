"""The product's interpreter floor, applied to every repository source this suite parses.

#1001: the ``guards`` job ran ``tests/ci`` on the runner's system Python 3.12 while
``pyproject.toml`` declares 3.14 and product sources already use PEP 758
``except A, B:``. A parse failure was reported as broken product code, so the gate
constrained the product's language version instead of checking it. Every parse of a
repository file goes through :func:`source_tree`: below the declared floor it skips
with the interpreter it needs; at or above it a ``SyntaxError`` is a source defect.

Deliberately not routed through the policy: ``ast.parse`` on string-literal
fixtures (they are written in the syntax the guard itself must run), the
``feature_version=(3, 12)`` pins that assert a lightweight entry script stays
3.12-parseable, and this suite's own modules (Ruff keeps them ``py312``).
"""

from __future__ import annotations

import ast
import re
import sys
import tomllib
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PYPROJECT = ROOT / "pyproject.toml"
PYTHON_PIN = ROOT / ".python-version"
FLOOR = re.compile(r">=\s*(\d+)(?:\.(\d+))?(?:\.(\d+))?")


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _version(parts: tuple[int, ...]) -> tuple[int, int, int]:
    """Pad to major.minor.patch so every comparison is total (3.14 < 3.14.1)."""
    padded = (*parts, 0, 0)
    return padded[0], padded[1], padded[2]


def required_python() -> tuple[int, int, int]:
    """The product's declared interpreter floor, read from ``requires-python``.

    A second copy of ``3.14`` in a guard is how the guard starts constraining the
    product instead of checking it (#1001), so the number is read here. A declared
    patch (``>=3.14.1``) is kept rather than rounded down to 3.14.
    """
    declared = tomllib.loads(read(PYPROJECT))["project"]["requires-python"]
    floor = FLOOR.search(declared)
    assert floor is not None, f"requires-python {declared!r} has no >=X.Y floor"
    return _version(tuple(int(part) for part in floor.groups() if part))


REQUIRED_PYTHON = required_python()


def floor_label() -> str:
    """``3.14`` for a floor without a patch, ``3.14.1`` when one is declared."""
    major, minor, patch = REQUIRED_PYTHON
    return f"{major}.{minor}" + (f".{patch}" if patch else "")


def source_ast(path: str | Path, text: str, version: tuple[int, ...]) -> ast.Module:
    """Parse one repository source under the product's interpreter floor."""
    try:
        return ast.parse(text)
    except SyntaxError as error:
        running = _version(version)
        if running < REQUIRED_PYTHON:
            raise unittest.SkipTest(
                f"this guard must run on Python >= {floor_label()} "
                f"(running {running[0]}.{running[1]}); it cannot judge syntax it "
                "cannot parse"
            ) from None
        raise AssertionError(
            f"{path} does not parse on Python {running[0]}.{running[1]}, which meets the "
            f"product floor {floor_label()} declared in pyproject.toml: {error}. Fix the "
            "source or pin a text: probe."
        ) from None


def source_tree(path: str | Path, *, version: tuple[int, ...] | None = None) -> ast.Module:
    """``ast.parse`` for one repository file, under the interpreter-floor policy."""
    return source_ast(path, read(ROOT / path), version or sys.version_info[:3])
