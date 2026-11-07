"""Guard the delivery-matrix freshness job against removal or path drift."""

from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = (ROOT / ".github/workflows/ci.yml").read_text()
JOB = "delivery_tracking"
SNAPSHOT = "docs/plans/github-issues-snapshot.json"
MATRIX = "docs/plans/delivery-tracking.md"
GENERATOR = "scripts/dev-env/refresh-implementation-tracker.py"
FILTER_INPUTS = (
    MATRIX,
    "docs/plans/delivery-issues.json",
    SNAPSHOT,
    GENERATOR,
    "scripts/dev-env/delivery_tracker.py",
)
DIFF_COMMAND = f"git diff --exit-code -- {MATRIX}"


def job_block(job_id: str) -> str:
    marker = f"\n  {job_id}:\n"
    start = WORKFLOW.index(marker)
    remainder = WORKFLOW[start + len(marker) :]
    following = re.search(r"^  [A-Za-z0-9_-]+:\n", remainder, re.MULTILINE)
    end = start + len(marker) + (following.start() if following else len(remainder))
    return WORKFLOW[start:end]


def step_run(job_id: str, name: str) -> str:
    block = job_block(job_id)
    marker = f"      - name: {name}\n"
    start = block.index(marker) + len(marker)
    body = block[start:]
    run_marker = "        run: |\n"
    run_start = body.index(run_marker) + len(run_marker)
    lines = []
    for line in body[run_start:].splitlines(keepends=True):
        if line.strip() and not line.startswith("          "):
            break
        lines.append(line)
    return "".join(lines)


class DeliveryTrackingWorkflowTests(unittest.TestCase):
    def test_job_regenerates_from_the_snapshot_then_rejects_drift(self) -> None:
        self.assertIn('name: "Docs: delivery matrix freshness"', job_block(JOB))
        script = step_run(JOB, "Regenerate the delivery matrix from the committed snapshot")
        self.assertIn(f"--snapshot {SNAPSHOT}", script)
        self.assertIn(f"--output {MATRIX}", script)
        self.assertIn(DIFF_COMMAND, script)
        self.assertNotIn("gh api", script)
        self.assertLess(script.index(GENERATOR), script.index(DIFF_COMMAND))

    def test_path_filter_covers_every_render_input(self) -> None:
        block = job_block(JOB)
        inputs = block.split("inputs=(", 1)[1].split(")", 1)[0]
        for path in FILTER_INPUTS:
            with self.subTest(path=path):
                self.assertIn(path, inputs)

    def test_required_gate_depends_on_the_freshness_job(self) -> None:
        needs = job_block("python").split("needs:", 1)[1].split("if:", 1)[0]
        self.assertIn(JOB, needs)


if __name__ == "__main__":
    unittest.main()
