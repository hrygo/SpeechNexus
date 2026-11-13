"""Meta-guard: no configuration key may be silently inert without a disposition.

Issue #660 upgraded the known sample -- a configured
``SPEECHNEXUS_VOICE_PUBLISHER_DATABASE_URL`` that started clean and still answered
``503 voice_draft_unavailable`` -- into a named startup refusal, then scanned the
remaining "configured A only works with C" keys. The scan itself is the fragile
part: the next silent key will be added by someone who never reads this card. This
guard keeps the scan from drifting and keeps every silent row disposed of.

It performs four checks, all text-only (system interpreter, no service import, no
container, no database):

1. **Inventory.** Every optional selector field declared in a scanned settings
   module (``SecretStr`` / ``Path`` / ``AnyHttpUrl`` / ``str`` annotated ``| None``)
   must appear as a row key or in :data:`EXEMPT` with a reason. A newly added
   ``*_DATABASE_URL`` or companion key therefore fails until it is classified.
2. **Evidence.** Every row cites ``<path>::<sentinel>``; the path must exist and a
   ``.py`` sentinel must be a test function in that file, a ``.md`` sentinel must
   appear verbatim. Renaming the test or rewording the documented sentence turns
   the guard red instead of silently keeping a stale citation.
3. **Silent rows.** A row classified ``静默无效`` must carry a disposition of
   ``已修 #<issue>`` or ``已开 issue #<issue>``; ``已修`` additionally requires
   executable test evidence. Adding a silent row without disposing of it is red.
4. **Document sync.** ``docs/operations/configuration-prerequisites.md`` carries the
   same table for operators; its keys, behaviour and disposition cells must match
   the module row for row.

What it cannot see: it cannot prove that a ``点名拒绝`` row's message names the
keys (that is asserted by the cited unit tests), it cannot see a pair that was
never declared in settings at all, and it does not scan numeric/concurrency knobs
(Compose forwards their defaults, so "configured" is not observable at startup --
the documented knob group states exactly that).
"""

from __future__ import annotations

import ast
import re
import unittest
from pathlib import Path

from tests.ci.configuration_prerequisites import (
    BACKEND,
    BEHAVIOURS,
    CONTROLLER,
    DOC,
    EXEMPT,
    GATEWAY,
    NAMED,
    ROWS,
    SILENT,
    WORKBENCH,
    Row,
)

ROOT = Path(__file__).resolve().parents[2]
DOC_PATH = ROOT / DOC
HEADING = "## 扫描表"
NEXT_HEADING = "\n## "
TABLE_ROW = re.compile(
    r"(?m)^\|\s*(?P<keys>[^|]*?)\s*\|\s*(?P<pre>[^|]*?)\s*\|"
    r"\s*(?P<behaviour>[^|]*?)\s*\|\s*(?P<disposition>[^|]*?)\s*\|\s*$"
)
DISPOSED = re.compile(r"^(?:已修 #(?P<fixed>\d+)|已开 issue #(?P<tracked>\d+))")
SELECTOR_TYPE = re.compile(r"\b(?:SecretStr|Path|AnyHttpUrl|str)\b")
SCANNED = (
    (BACKEND, "SPEECHNEXUS_"),
    (WORKBENCH, "SPEECHNEXUS_WORKBENCH_"),
    (GATEWAY, "SPEECHNEXUS_GATEWAY_"),
    (CONTROLLER, "SPEECHNEXUS_CONTROLLER_"),
)


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def selector_variables(module: str, prefix: str) -> dict[str, str]:
    """Return ``{env name: field name}`` for every optional selector in ``module``.

    Parsing the class body instead of importing it keeps this guard runnable under
    the system interpreter. Numeric and boolean knobs are excluded on purpose.
    """
    tree = ast.parse(read(ROOT / module))
    found: dict[str, str] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef) or node.name != "Settings":
            continue
        for statement in node.body:
            if not isinstance(statement, ast.AnnAssign) or not isinstance(
                statement.target, ast.Name
            ):
                continue
            annotation = ast.unparse(statement.annotation)
            if "None" not in annotation or SELECTOR_TYPE.search(annotation) is None:
                continue
            field = statement.target.id
            found[prefix + field.upper()] = field
    return found


def documented_rows() -> dict[str, tuple[str, str]]:
    """Parse the operator table between ``## 扫描表`` and the next level-two heading."""
    text = read(DOC_PATH)
    start = text.find(HEADING)
    if start < 0:
        raise AssertionError(f"{DOC} has no {HEADING!r} section")
    end = text.find(NEXT_HEADING, start + len(HEADING))
    section = text[start : len(text) if end < 0 else end]
    rows: dict[str, tuple[str, str]] = {}
    for match in TABLE_ROW.finditer(section):
        keys = match.group("keys").strip()
        if not keys or set(keys) <= {"-", " "}:
            continue
        if "键" in keys:
            continue
        rows[keys] = (match.group("behaviour").strip(), match.group("disposition").strip())
    return rows


def keys_cell(row: Row) -> str:
    if not row.keys:
        return "（数值与并发旋钮组）"
    return "、".join(f"`{key}`" for key in row.keys)


class ConfigurationPrerequisitesTest(unittest.TestCase):
    def test_every_row_cites_existing_evidence(self) -> None:
        for row in ROWS:
            path, _, sentinel = row.evidence.partition("::")
            target = ROOT / path
            self.assertTrue(
                target.is_file(),
                f"{row.keys[0] if row.keys else row.prerequisite}: missing evidence path {path}",
            )
            text = read(target)
            if path.endswith(".py"):
                tree = ast.parse(text)
                names = {
                    node.name
                    for node in ast.walk(tree)
                    if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
                }
                self.assertIn(sentinel, names, f"{path} has no test function {sentinel}")
            else:
                self.assertIn(sentinel, text, f"{path} no longer contains {sentinel!r}")

    def test_every_optional_selector_is_classified(self) -> None:
        covered = {key for row in ROWS for key in row.keys}
        for module, prefix in SCANNED:
            for variable, field in selector_variables(module, prefix).items():
                if (module, field) in EXEMPT:
                    self.assertTrue(
                        EXEMPT[(module, field)].strip(), f"{module}:{field} exemption has no reason"
                    )
                    continue
                self.assertIn(
                    variable,
                    covered,
                    f"{module}:{field} is an unclassified optional selector; add a row "
                    f"to tests/ci/configuration_prerequisites.py or an EXEMPT reason",
                )

    def test_a_silent_row_must_be_disposed(self) -> None:
        for row in ROWS:
            self.assertIn(row.behaviour, BEHAVIOURS, f"unknown behaviour {row.behaviour}")
            if row.behaviour != SILENT:
                continue
            match = DISPOSED.match(row.disposition)
            self.assertIsNotNone(
                match,
                f"{keys_cell(row)} is silent and has no disposition; fix it or open an "
                f"issue and record it here",
            )
            assert match is not None
            if match.group("fixed") is not None:
                path, _, _ = row.evidence.partition("::")
                self.assertTrue(
                    path.endswith(".py"),
                    f"{keys_cell(row)} claims a fix without executable evidence",
                )

    def test_the_operator_table_matches_the_module(self) -> None:
        documented = documented_rows()
        expected = {keys_cell(row): (row.behaviour, row.disposition) for row in ROWS}
        self.assertEqual(
            sorted(expected),
            sorted(documented),
            f"{DOC} and tests/ci/configuration_prerequisites.py disagree about the keys",
        )
        for cell, (behaviour, disposition) in expected.items():
            self.assertEqual(
                (behaviour, disposition),
                documented[cell],
                f"{DOC} row {cell} disagrees with the module",
            )

    def test_the_known_sample_is_a_named_refusal(self) -> None:
        """Regression anchor: the sample from #660 must not drift back to silent."""
        sample = [
            row
            for row in ROWS
            if row.keys and row.keys[0] == "SPEECHNEXUS_VOICE_PUBLISHER_DATABASE_URL"
        ]
        self.assertTrue(sample)
        self.assertTrue(all(row.behaviour == NAMED for row in sample))
        self.assertTrue(all(row.names_keys for row in sample))


if __name__ == "__main__":
    unittest.main()
