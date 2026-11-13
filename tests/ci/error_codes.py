"""Index the wire error codes the backend HTTP handlers can actually emit.

Two ``tests/ci`` guards ask the same question — *is the code written in the
runbook a code this handler really raises, with this status?* — so the parsing
lives here instead of being copied twice:

- ``test_reference_deploy_dsn_coverage.py`` binds each coverage-table row to the
  handler modules that answer its endpoints, so a row may not name another
  capability's code (a real 503 from the audit handler in a binding row is drift).
- ``test_compose_minimal_role_provisioning.py`` checks every status the runbook
  quotes, ``422`` included, together with the pronunciation pair whose wire code
  is composed at the boundary rather than written literally.

The index is built from text only. It reads four shapes the handlers use:

1. a table entry ``"wire": (STATUS, ...)`` — the wire name is the key;
2. a table entry ``"reason": (STATUS, kind, message, "wire", retryable)`` — the
   fourth element is the wire name (``voice_publications.py``);
3. ``"prefix_" + variable`` composition (``pronunciations.py`` builds
   ``pronunciation_<reason>`` from one stable prefix);
4. a table entry whose fourth element is the composed suffix while the prefix is
   applied at the boundary (``pronunciation_sets.py``).

No backend import, no module-level state, no network: the guards run under the
system interpreter like the rest of ``tests/ci``.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
HANDLERS = ROOT / "backend/src/speechnexus_backend/speech/interfaces/http"

# `"name": (503,` on one line or across lines; the status is the first element.
TABLE_ENTRY = re.compile(r'(?m)^\s*"([a-z0-9_]+)"\s*:\s*\(\s*(\d{3})\s*,')
# A module that composes wire codes from one stable prefix: `"pronunciation_" + code`.
COMPOSED_PREFIX = re.compile(r'"([a-z0-9_]+_)"\s*\+\s*\w')
# A five-element entry: status, kind, message, wire, retryable.
FIVE_ELEMENT = re.compile(
    r'(?m)^\s*"[a-z0-9_]+"\s*:\s*\(\s*(\d{3})\s*,\s*"[^"]*"\s*,\s*"[^"]*"\s*,\s*"([a-z0-9_]+)"'
)
# The runbook's own form: a whole code span, e.g. `` `503 binding_unavailable` ``.
# Restricting to fenced code avoids matching prose numbers such as `chmod 700 deploy`.
DOCUMENTED_SPAN = re.compile(r"`(\d{3}) ([a-z][a-z0-9_]+)`")


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def handler_modules() -> list[Path]:
    return sorted(HANDLERS.glob("*.py"))


def module_codes(source: str) -> dict[str, int]:
    """Wire code -> status declared or composed by one module's source."""
    codes: dict[str, int] = {}
    for name, status in TABLE_ENTRY.findall(source):
        codes.setdefault(name, int(status))
    for status, wire in FIVE_ELEMENT.findall(source):
        codes.setdefault(wire, int(status))
    prefixes = COMPOSED_PREFIX.findall(source)
    for prefix in prefixes:
        for name, status in list(codes.items()):
            codes.setdefault(prefix + name, status)
        for status, wire in FIVE_ELEMENT.findall(source):
            codes.setdefault(prefix + wire, int(status))
    return codes


def declarations() -> dict[str, dict[str, int]]:
    """Handler module file name -> its wire code index."""
    return {path.name: module_codes(read(path)) for path in handler_modules()}


def documented_codes(document: str) -> dict[str, int]:
    """Codes the runbook states, mapped to the status it gives them."""
    codes: dict[str, int] = {}
    for status, code in DOCUMENTED_SPAN.findall(document):
        codes.setdefault(code, int(status))
    return codes


def declaring_modules(code: str) -> dict[str, int]:
    """Every handler module that emits ``code``, with the status it gives it."""
    found: dict[str, int] = {}
    for module, codes in declarations().items():
        status = codes.get(code)
        if status is not None:
            found[module] = status
    return found
