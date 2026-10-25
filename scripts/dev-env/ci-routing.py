"""Conservative PR routing; uncertain/shared inputs retain full CI verification."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path, PurePosixPath
from typing import Literal

Mode = Literal["docs", "web", "full"]
_ROOT_PROSE = {"AGENTS.md", "README.md", "README.en.md"}
_WEB_SUFFIXES = {".ts", ".tsx", ".css", ".html", ".svg", ".png", ".webp", ".jpg", ".woff2"}


def classify(paths: Sequence[str]) -> Mode:
    if not paths:
        return "full"
    mode: Mode = "docs"
    for name in paths:
        path = PurePosixPath(name)
        if (
            not name
            or path.is_absolute()
            or "\\" in name
            or ".." in path.parts
            or path.as_posix() != name
        ):
            return "full"
        if name in _ROOT_PROSE or (name.startswith("docs/") and path.suffix == ".md"):
            continue
        parts = path.parts
        web_source = (
            len(parts) >= 3
            and parts[0] == "web"
            and (
                parts[1] == "shared"
                or (
                    parts[1] in {"admin", "workbench"}
                    and (parts[2] in {"src", "public"} or parts[2] == "index.html")
                )
            )
        )
        if web_source and path.suffix in _WEB_SUFFIXES:
            mode = "web"
        else:
            return "full"
    return mode


def _git(cwd: Path, *args: str) -> bytes:
    return subprocess.check_output(["git", *args], cwd=cwd, stderr=subprocess.PIPE, timeout=30)


def detect_route(event: str, base: str, cwd: Path) -> tuple[Mode, str]:
    if event != "pull_request":
        return "full", "Main pushes, manual runs and unknown events require full verification."
    if not re.fullmatch(r"[0-9a-f]{40}", base) or base == "0" * 40:
        return "full", "No valid exact base commit; full verification is required."
    try:
        try:
            _git(cwd, "cat-file", "-e", f"{base}^{{commit}}")
        except subprocess.CalledProcessError:
            _git(cwd, "fetch", "--no-tags", "--depth=1", "origin", base)
        changed = _git(cwd, "diff", "--name-only", "--no-renames", "-z", base, "HEAD", "--")
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return "full", "The exact Git diff is unavailable; full verification is required."
    # Keep whitespace/conflict-marker errors fatal, outside the routing fallback.
    _git(cwd, "diff", "--check", base, "HEAD", "--")
    paths = [
        name.decode("utf-8", errors="surrogateescape") for name in changed.split(b"\0") if name
    ]
    mode = classify(paths)
    reasons = {
        "docs": "Only approved prose changed; heavy application checks are out of scope.",
        "web": "Web sources changed; retain Python consumer/unit checks and the complete Web lane.",
        "full": "Shared, unknown, executable or empty changes require every verification lane.",
    }
    return mode, reasons[mode]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--event", required=True)
    parser.add_argument("--base", default="")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--summary", type=Path)
    args = parser.parse_args()
    try:
        mode, reason = detect_route(args.event, args.base, Path.cwd())
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        print("Diff validation failed; inspect git diff --check locally.", file=sys.stderr)
        return 1
    if args.output:
        with args.output.open("a") as handle:
            handle.write(f"mode={mode}\n")
    if args.summary:
        with args.summary.open("a") as handle:
            handle.write(f"### CI route: {mode}\n\n{reason}\n")
    print(json.dumps({"mode": mode, "reason": reason}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
