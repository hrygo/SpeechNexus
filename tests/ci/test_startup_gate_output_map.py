"""Keep the startup-gate failure-output table and its evidence in one place (#928).

``#926`` found that a document promised one named ``FAIL:`` line while the real
entry point printed a traceback, and that the test guarding it never started the
entry point. Neither mistake is visible from the code that was reviewed: the
promise lived in prose and the evidence in a mocked function. This guard makes both
mechanical:

- the table in ``docs/operations/startup-gate-output.md`` must equal the rows that
  ``tests/ci/startup_gates.py`` renders, so a row cannot be edited on one side only;
- every row whose documents promise an output shape (``一行 FAIL:`` or one named
  line) must name a case that exists in a process-test module — a case that starts
  the real entry point, not a mocked function;
- a row the documents have not been reconciled with (``一致 = 否``) must not name a
  case: evidence is not invented for a promise another card is rewriting;
- the table must be reachable from the two documents an operator reads first, so a
  future edit cannot orphan it.

The module is text and standard library only: it runs under the system interpreter
like every other guard in ``tests/ci`` and never imports the backend.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

from tests.ci.startup_gates import (
    EXACT_LINE,
    GATES,
    ONE_FAIL_LINE,
    PROCESS_TEST_MODULES,
    inconsistent_rows,
    process_evidence_rows,
    render_table,
)

ROOT = Path(__file__).resolve().parents[2]
DOC = ROOT / "docs/operations/startup-gate-output.md"
COMPOSE_MINIMAL = ROOT / "docs/operations/compose-minimal.md"
RUNBOOK = ROOT / "docs/operations/async-job-runbook.md"
OPERATIONS_README = ROOT / "docs/operations/README.md"
START = "<!-- startup-gate-output:start -->"
END = "<!-- startup-gate-output:end -->"
CASE_PATTERN = re.compile(r"^def (test_[a-z0-9_]+)\(", re.MULTILINE)


def documented_table(text: str) -> str:
    """Return the table between the markers, or an empty string when they are gone."""
    start = text.find(START)
    end = text.find(END)
    if start < 0 or end < 0 or end < start:
        return ""
    return text[start + len(START) : end].strip()


def table_problems(rendered: str, documented: str) -> list[str]:
    """Return every reason the documented table no longer matches the source rows."""
    if not documented:
        return [f"{DOC.name} has no table between {START} and {END}"]
    return [] if rendered == documented else ["the documented table is not the rendered rows"]


def registered_cases() -> dict[str, str]:
    """Map every test function name to the module that defines it."""
    found: dict[str, str] = {}
    for relative in PROCESS_TEST_MODULES:
        path = ROOT / relative
        if not path.exists():
            continue
        for name in CASE_PATTERN.findall(path.read_text(encoding="utf-8")):
            found[name] = relative
    return found


class StartupGateTableTests(unittest.TestCase):
    """Run the guard against the real checkout, then against mutations of it."""

    def setUp(self) -> None:
        self.doc = DOC.read_text(encoding="utf-8")
        self.cases = registered_cases()

    def test_the_documented_table_is_the_rendered_rows(self) -> None:
        self.assertEqual(
            table_problems(render_table(), documented_table(self.doc)),
            [],
        )

    def test_a_changed_row_turns_the_comparison_red(self) -> None:
        mutated = render_table().replace("ASGI lifespan", "同步装配", 1)
        self.assertNotEqual(mutated, render_table())
        self.assertTrue(table_problems(mutated, documented_table(self.doc)))

    def test_every_promised_shape_names_a_process_level_case(self) -> None:
        missing = [
            gate.slug
            for gate in process_evidence_rows()
            if gate.case is None or gate.case not in self.cases
        ]
        self.assertEqual(missing, [])

    def test_an_unreconciled_row_cannot_carry_evidence(self) -> None:
        for gate in inconsistent_rows():
            self.assertIsNone(
                gate.case,
                f"{gate.slug} promises something the measured output does not do; "
                "the case belongs to the card that reconciles the documents",
            )
            self.assertIn("#", gate.disposition, f"{gate.slug} must name the owning card")

    def test_every_row_is_complete_and_unique(self) -> None:
        slugs = [gate.slug for gate in GATES]
        self.assertEqual(len(slugs), len(set(slugs)))
        for gate in GATES:
            for field in (gate.unit, gate.stage, gate.observed, gate.promise, gate.agrees):
                self.assertTrue(field.strip(), f"{gate.slug} has an empty column")
            self.assertIn(
                gate.promise_kind,
                {ONE_FAIL_LINE, EXACT_LINE, "无格式承诺", "未验证"},
                f"{gate.slug} carries an unknown promise kind",
            )

    def test_the_table_is_reachable_from_the_documents_an_operator_reads(self) -> None:
        for document in (COMPOSE_MINIMAL, RUNBOOK, OPERATIONS_README):
            text = document.read_text(encoding="utf-8")
            self.assertIn(
                "startup-gate-output.md",
                text,
                f"{document.name} must link the startup-gate output table",
            )


if __name__ == "__main__":  # pragma: no cover - guard entry
    unittest.main()
