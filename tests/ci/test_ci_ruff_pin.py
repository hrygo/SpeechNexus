"""The docs formatting lane must run the same Ruff the full static lane installs.

`docs` scope runs no job that installs the locked `dev` group, so the prose-only
lane re-runs `ruff format --check .` through `uvx` with an explicit pin. A version
drift would let a prose-only pull request pass the light lane and still break
`main` on the next push, which is the defect #673 records.

Standard library only: this module runs in the dependency-free routing lane.
"""

from __future__ import annotations

import re
import tomllib
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CI = (ROOT / ".github/workflows/ci.yml").read_text()
LOCKED_RUFF = next(
    package["version"]
    for package in tomllib.loads((ROOT / "uv.lock").read_text())["package"]
    if package["name"] == "ruff"
)
PIN = re.search(r'^      RUFF_VERSION: "([^"]+)"$', CI, re.MULTILINE)
DOCS_JOB = CI.split("\n  docs_format:\n", 1)[1].split("\n  delivery_tracking:\n", 1)[0]
FORMAT_COMMAND = 'uvx --from "ruff==${RUFF_VERSION}" ruff format --check .'


class RuffPinTests(unittest.TestCase):
    def test_the_lane_pins_the_locked_ruff(self) -> None:
        self.assertIsNotNone(PIN)
        assert PIN is not None
        self.assertEqual(PIN.group(1), LOCKED_RUFF)

    def test_the_lane_runs_the_format_check_and_installs_no_project_dependencies(self) -> None:
        self.assertIn(FORMAT_COMMAND, DOCS_JOB)
        self.assertNotIn("uv sync", DOCS_JOB)
        self.assertNotIn("pnpm install", DOCS_JOB)
        self.assertNotIn("if:", DOCS_JOB.split("steps:", 1)[1])

    def test_the_lane_is_the_only_tenant_of_the_pin(self) -> None:
        # One pin, one command: a second copy would be a copy that can drift.
        self.assertEqual(CI.count('RUFF_VERSION: "'), 1)
        self.assertEqual(CI.count(FORMAT_COMMAND), 1)

    def test_the_merge_gate_requires_the_lane(self) -> None:
        # The lane is only binding because `Gate: required checks for this change`
        # waits on it; a job nobody's gate needs reports red without blocking a merge.
        gate = CI.split("\n  python:\n", 1)[1]
        needs = gate.split("    needs:", 1)[1].split("]", 1)[0]
        self.assertIn("\n        docs_format,\n", needs)
        self.assertIn("DOCS_FORMAT_RESULT: ${{ needs.docs_format.result }}", gate)


if __name__ == "__main__":
    unittest.main()
