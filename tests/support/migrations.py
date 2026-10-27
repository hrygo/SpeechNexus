"""The migration head, derived once so a new revision cannot break unrelated tests."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
VERSIONS = ROOT / "backend/src/speechnexus_backend/speech/infrastructure/migrations/versions"

_REVISION = re.compile(r'^revision = "([^"]+)"', re.MULTILINE)
_DOWN = re.compile(r'^down_revision = "([^"]+)"', re.MULTILINE)


def head_revision() -> str:
    revisions: set[str] = set()
    parents: set[str] = set()
    for path in VERSIONS.glob("*.py"):
        source = path.read_text(encoding="utf-8")
        revision = _REVISION.search(source)
        if revision is None:
            continue
        revisions.add(revision.group(1))
        down = _DOWN.search(source)
        if down is not None:
            parents.add(down.group(1))
    heads = revisions - parents
    if len(heads) != 1:
        raise RuntimeError(f"Expected exactly one migration head, found {sorted(heads)}")
    return heads.pop()


HEAD = head_revision()
