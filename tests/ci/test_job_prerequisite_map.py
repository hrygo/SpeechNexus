"""Keep the operator map and the shipped prerequisite table one table.

``docs/operations/compose-minimal.md`` §9.1 lists, row by row, which missing
prerequisite produces which public ``detail`` and what an operator runs. The shipped
rows live in ``speech/domain/job_prerequisites.py`` as plain literals, so this guard
can read both without importing the backend -- ``tests/ci`` runs under the system
interpreter, like every other text guard here.

Two directions, both required:

* every shipped row appears in the document with its condition, its exact
  ``detail`` and its exact action, so an implementation change that skips the map
  turns this red;
* the document adds no row of its own and reorders none, so the map never describes
  a condition the code cannot answer (#920).

The red-side mutations are the point: deleting a key name from a row's ``detail``,
adding a document row for a condition that does not exist, or editing the document
text away from the constant fails here before an operator reads a wrong repair.
"""

from __future__ import annotations

import ast
import re
import unittest
from pathlib import Path

from tests.ci.interpreter_floor import source_tree

ROOT = Path(__file__).resolve().parents[2]
PREREQUISITES = ROOT / "backend/src/speechnexus_backend/speech/domain/job_prerequisites.py"
MAP_DOC = ROOT / "docs/operations/compose-minimal.md"
SECTION_START = "### 9.1 「缺什么 → 看到什么 → 做什么」：Job 前置条件映射表"
SECTION_END = "## 10. 停止与清理边界"
ROW = re.compile(r"^\| `(?P<condition>[a-z0-9_]+)` \| `(?P<detail>.+?)` \| (?P<action>.+) \|$")
# Anything an operator answer may never echo: a DSN scheme, host, credential.
LEAK = re.compile(r"postgresql://|mysql://|@|password|secret", re.IGNORECASE)


def shipped_rows() -> list[tuple[str, str, str]]:
    """Read the shipped table as literals, without importing the backend."""
    tree = source_tree(PREREQUISITES)
    for node in tree.body:
        target: ast.expr
        value: ast.expr | None
        if isinstance(node, ast.AnnAssign):
            target, value = node.target, node.value
        elif isinstance(node, ast.Assign) and len(node.targets) == 1:
            target, value = node.targets[0], node.value
        else:
            continue
        if not isinstance(target, ast.Name) or target.id != "JOB_PREREQUISITES" or value is None:
            continue
        rows = ast.literal_eval(value)
        assert isinstance(rows, tuple)
        return [(condition, detail, action) for condition, detail, action in rows]
    raise AssertionError(f"JOB_PREREQUISITES is not a literal assignment in {PREREQUISITES}")


def section_text() -> str:
    """Return the operator map section between its heading and the next chapter."""
    document = MAP_DOC.read_text(encoding="utf-8")
    assert SECTION_START in document, "the operator map section is missing"
    start = document.index(SECTION_START) + len(SECTION_START)
    return document[start : document.index(SECTION_END, start)]


def documented_rows() -> list[tuple[str, str, str]]:
    """Return the map's rows in document order."""
    section = section_text()
    rows = []
    for line in section.splitlines():
        match = ROW.match(line.strip())
        if match is not None:
            rows.append((match["condition"], match["detail"], match["action"]))
    return rows


class JobPrerequisiteMapTests(unittest.TestCase):
    def test_every_shipped_row_is_documented_verbatim(self) -> None:
        documented = {
            condition: (detail, action) for condition, detail, action in documented_rows()
        }
        problems = []
        for condition, detail, action in shipped_rows():
            if condition not in documented:
                problems.append(f"{condition}: missing from the operator map")
                continue
            shown_detail, shown_action = documented[condition]
            if shown_detail != detail:
                problems.append(f"{condition}: the map's detail is not the shipped one")
            if action not in shown_action:
                problems.append(f"{condition}: the map does not show the shipped action")
        self.assertEqual(problems, [], problems)

    def test_the_map_names_exactly_the_shipped_conditions_in_order(self) -> None:
        shipped = [condition for condition, _, _ in shipped_rows()]
        documented = [condition for condition, _, _ in documented_rows()]
        # No document row may describe a condition the code cannot answer, and the
        # order is the table's so a reader can compare them line by line.
        self.assertEqual(documented, shipped)

    def test_no_row_echoes_a_dsn_value_host_or_credential(self) -> None:
        for condition, detail, action in shipped_rows():
            self.assertIsNone(LEAK.search(detail), f"{condition}: {detail}")
            self.assertIsNone(LEAK.search(action), f"{condition}: {action}")

    def test_the_migration_command_is_declared_once_in_the_product_source(self) -> None:
        """The repair command an operator runs has one declaration.

        ``MIGRATION_COMMAND`` is that declaration; the write path, the read path,
        the startup refusal and the operator map all print it from there, so a second
        copy cannot drift. ``deploy/`` is text outside the product package and keeps
        its own gate line, which this guard does not govern.
        """
        literal = "docker compose --profile migration run --rm migrate"
        declaring = sorted(
            path.name
            for path in (ROOT / "backend/src").rglob("*.py")
            if literal in path.read_text(encoding="utf-8")
        )
        self.assertEqual(declaring, ["job_prerequisites.py"])

    def test_the_startup_section_claims_only_what_the_entry_point_does(self) -> None:
        """Two different exit shapes, and the map may not blend them.

        The synchronous assembly refusal prints one ``FAIL:`` line and exits 1 --
        measured. The ASGI lifespan refusal (the Job schema preflight) is ended by
        uvicorn, which logs the exception with its traceback and exits 3; the entry
        point's ``except`` cannot see it (#920 review M1). A map that promised one
        traceback-free line for both was wrong, so the text is pinned here.
        """
        section = section_text()
        self.assertIn("FAIL:", section)
        self.assertIn("uvicorn", section)
        self.assertIn("退出码 3", section)
        self.assertNotIn("不产生 traceback", section)

    def test_a_half_applied_migration_is_not_advertised_as_the_migration_row(self) -> None:
        # R1: only a missing relation/schema is the migration row; a missing column
        # keeps the generic storage sentence, and the map says so.
        section = section_text()
        self.assertIn("42703", section)

    def test_two_conditions_never_share_a_detail(self) -> None:
        details = [detail for _, detail, _ in shipped_rows()]
        self.assertEqual(sorted(details), sorted(set(details)))

    def test_a_missing_row_or_a_deleted_key_name_turns_the_map_red(self) -> None:
        # The guard's own red side: reading the rows it must compare is not vacuous,
        # and each shipped detail really is the text the document prints.
        rows = documented_rows()
        self.assertTrue(rows, "the operator map has no rows")
        document = MAP_DOC.read_text(encoding="utf-8")
        for condition, detail, _ in rows:
            self.assertIn(f"`{condition}`", document)
            self.assertIn(detail, document)


if __name__ == "__main__":
    unittest.main()
