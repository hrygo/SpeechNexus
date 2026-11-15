#!/usr/bin/env python3
"""Report Python imports of generated contract modules for the consumer guard.

The generated-contract consumer guard must not count a module as consumed just
because a comment, docstring or string literal mentions an import. Python's own
tokenizer decides: only a real ``import``/``from ... import`` statement records
evidence. The output is JSON so the Node guard stays dependency-free.

Usage::

    python3 scripts/dev-env/scan-contract-imports.py --root <repository root>

Output::

    {"imports": {"speechnexus_contracts.admin": [{"file": "backend/...", "line": 3}]},
     "unparsed": ["backend/..."]}
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tokenize
from collections import defaultdict
from pathlib import Path
from tokenize import TokenInfo

SKIP_DIRECTORIES = {
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".runtime",
    ".venv",
    "__pycache__",
    "coverage",
    "dist",
    "node_modules",
    "playwright-report",
    "test-results",
}
CONTRACTS_PACKAGE = Path("packages/contracts")
MODULE_PREFIX = "speechnexus_contracts"
IGNORED_TOKENS = {tokenize.ENCODING, tokenize.NL, tokenize.COMMENT}
STATEMENT_BREAK_TOKENS = {tokenize.NEWLINE, tokenize.INDENT, tokenize.DEDENT}


def iter_python_files(root: Path) -> list[Path]:
    """List ``.py`` files outside the generated package, skipping build trees."""
    files: list[Path] = []
    for directory, subdirectories, filenames in os.walk(root):
        subdirectories[:] = sorted(
            name
            for name in subdirectories
            if not name.startswith(".") and name not in SKIP_DIRECTORIES
        )
        current = Path(directory)
        relative = current.relative_to(root)
        if relative == CONTRACTS_PACKAGE or CONTRACTS_PACKAGE in relative.parents:
            continue
        files.extend(
            current / filename for filename in sorted(filenames) if filename.endswith(".py")
        )
    return files


def tokenize_file(path: Path) -> list[TokenInfo] | None:
    """Tokenize one file, returning ``None`` when Python itself cannot read it."""
    try:
        with path.open("rb") as handle:
            return list(tokenize.tokenize(handle.readline))
    except OSError, SyntaxError, UnicodeDecodeError, tokenize.TokenError, IndentationError:
        return None


def statement_start(previous: TokenInfo | None) -> bool:
    """True when the previous meaningful token ends a prior logical statement."""
    if previous is None:
        return True
    if previous.type in STATEMENT_BREAK_TOKENS:
        return True
    return previous.type == tokenize.OP and previous.string == ";"


def collect_plain_imports(tokens: list[TokenInfo], index: int, found: list[tuple[str, int]]) -> int:
    """Collect the dotted names of one ``import a.b as c, d`` statement."""
    parts: list[str] = []
    line = tokens[index - 1].start[0]

    def flush() -> None:
        if parts:
            found.append(("".join(parts), line))

    while index < len(tokens):
        token = tokens[index]
        if token.type == tokenize.NEWLINE:
            flush()
            return index
        if token.type in {tokenize.NL, tokenize.COMMENT}:
            index += 1
            continue
        if token.type == tokenize.NAME:
            if token.string == "as":
                flush()
                parts.clear()
                index += 2
                continue
            if not parts:
                line = token.start[0]
            parts.append(token.string)
        elif token.type == tokenize.OP and token.string in {".", ","}:
            if token.string == ",":
                flush()
                parts.clear()
            else:
                parts.append(".")
        index += 1
    flush()
    return index


def collect_from_module(tokens: list[TokenInfo], index: int) -> tuple[str, int]:
    """Collect the module of one ``from a.b import ...`` statement."""
    parts: list[str] = []
    while index < len(tokens):
        token = tokens[index]
        if token.type == tokenize.NAME:
            if token.string == "import":
                return "".join(parts), index
            parts.append(token.string)
        elif token.type == tokenize.OP and token.string == ".":
            parts.append(".")
        elif token.type in {tokenize.NEWLINE, tokenize.ENDMARKER}:
            return "".join(parts), index
        index += 1
    return "".join(parts), index


def scan_tokens(tokens: list[TokenInfo]) -> list[tuple[str, int]]:
    """Return generated-module imports declared by real import statements."""
    found: list[tuple[str, int]] = []
    index = 0
    previous: TokenInfo | None = None
    while index < len(tokens):
        token = tokens[index]
        is_import = token.type == tokenize.NAME and token.string in {"import", "from"}
        if is_import and statement_start(previous):
            if token.string == "import":
                index = collect_plain_imports(tokens, index + 1, found)
            else:
                module, import_index = collect_from_module(tokens, index + 1)
                if module and not module.startswith("."):
                    found.append((module, token.start[0]))
                index = import_index + 1
            previous = None
            continue
        if token.type not in IGNORED_TOKENS:
            previous = token
        index += 1
    return found


def collect(root: Path) -> dict[str, object]:
    """Map every generated contract module to its real Python import sites."""
    imports: dict[str, list[dict[str, object]]] = defaultdict(list)
    unparsed: list[str] = []
    for path in iter_python_files(root):
        relative = path.relative_to(root).as_posix()
        tokens = tokenize_file(path)
        if tokens is None:
            unparsed.append(relative)
            continue
        for module, line in scan_tokens(tokens):
            if module != MODULE_PREFIX and not module.startswith(MODULE_PREFIX + "."):
                continue
            occurrence: dict[str, object] = {"file": relative, "line": line}
            if occurrence not in imports[module]:
                imports[module].append(occurrence)
    return {"imports": dict(sorted(imports.items())), "unparsed": sorted(unparsed)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, help="repository root to scan")
    arguments = parser.parse_args()
    json.dump(collect(Path(arguments.root).resolve()), sys.stdout, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
