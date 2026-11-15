"""Prove the doc-reference gate turns red for terminal documents only.

#859: the check had printed `RESULT FAIL` since it was written, because 137 of its
138 findings came from `docs/reviews/` - review records that must keep the paths
and probe output they were written against. A check that is always red is not a
check, so the rule is now: terminal documents are fully gated, process records are
counted and reported against a baseline instead. Each case below drives the real
script against a throwaway git repository and asserts both sides of that rule -
green where the baseline tree must stay green, red where a new terminal reference
must block the merge.
"""

from __future__ import annotations

import ast
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/dev-env/check-doc-refs.py"
BASELINE = ROOT / "scripts/dev-env/doc-refs-baseline.json"
WORKFLOW = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")

JOB = "docs_refs"
GATE = "required_checks"
CI_COMMAND = (
    "python3 scripts/dev-env/check-doc-refs.py --baseline scripts/dev-env/doc-refs-baseline.json"
)
TERMINAL_DOC = "docs/operations/runbook.md"
REVIEW_DOC = "docs/reviews/2026-11-16-review.md"
REAL_SOURCE = "backend/src/speechnexus_backend/real.py"
GHOST_REF = "backend/src/speechnexus_backend/ghost.py:7"
OTHER_GHOST_REF = "backend/src/speechnexus_backend/also_ghost.py:11"


def job_block(job_id: str) -> str:
    """The YAML block of one job, bounded by the next job-level key."""
    marker = f"\n  {job_id}:\n"
    start = WORKFLOW.index(marker)
    remainder = WORKFLOW[start + len(marker) :]
    following = re.search(r"^  [a-z_]+:\n", remainder, re.MULTILINE)
    return WORKFLOW[
        start : start + len(marker) + (following.start() if following else len(remainder))
    ]


class Repository:
    """A throwaway git repository the script can be pointed at."""

    def __init__(self, case: unittest.TestCase) -> None:
        temporary = tempfile.TemporaryDirectory()
        case.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self._git("init", "-q")
        self.write(REAL_SOURCE, "".join(f"LINE_{number} = {number}\n" for number in range(1, 6)))

    def _git(self, *args: str) -> None:
        subprocess.run(["git", *args], cwd=self.root, check=True, capture_output=True, text=True)

    def write(self, relative: str, text: str) -> None:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def track(self) -> None:
        self._git("add", "-A")

    def run(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(SCRIPT), *args],
            cwd=self.root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            env={**os.environ, "PYTHONIOENCODING": "utf-8"},
            check=False,
        )


class ScopeTests(unittest.TestCase):
    """Terminal documents gate; process records only count."""

    def test_a_valid_terminal_reference_passes(self) -> None:
        repo = Repository(self)
        repo.write(TERMINAL_DOC, f"实现见 `{REAL_SOURCE}:3`。\n")
        repo.track()
        result = repo.run()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("SCOPE terminal problems=0", result.stdout)
        self.assertIn("RESULT PASS", result.stdout)

    def test_a_new_terminal_reference_turns_red(self) -> None:
        repo = Repository(self)
        repo.write(TERMINAL_DOC, f"实现见 `{GHOST_REF}`。\n")
        repo.track()
        result = repo.run()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("[MISSING]", result.stdout)
        self.assertIn("[新增,基线外]", result.stdout)
        self.assertIn("SCOPE terminal problems=1 new=1 accepted=0", result.stdout)
        self.assertIn("RESULT FAIL", result.stdout)

    def test_a_record_reference_is_counted_but_does_not_fail(self) -> None:
        repo = Repository(self)
        repo.write(REVIEW_DOC, f"当时指向 `{GHOST_REF}`。\n")
        repo.write(TERMINAL_DOC, f"实现见 `{REAL_SOURCE}:3`。\n")
        repo.track()
        result = repo.run()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("SCOPE terminal problems=0", result.stdout)
        self.assertIn("SCOPE record problems=1 docs/reviews MISSING=1", result.stdout)
        self.assertIn("RESULT PASS", result.stdout)

    def test_the_baseline_reports_the_record_increment_without_failing(self) -> None:
        repo = Repository(self)
        repo.write(TERMINAL_DOC, f"实现见 `{REAL_SOURCE}:3`。\n")
        repo.track()
        self.assertEqual(repo.run("--write-baseline", "baseline.json").returncode, 0)
        repo.write(REVIEW_DOC, f"当时指向 `{GHOST_REF}`。\n")
        repo.track()
        result = repo.run("--baseline", "baseline.json")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("RECORDS docs/reviews MISSING 0->1 (+1)", result.stdout)
        self.assertIn("RESULT PASS", result.stdout)

    def test_a_fenced_reference_is_not_a_reference(self) -> None:
        repo = Repository(self)
        repo.write(
            TERMINAL_DOC,
            "探针输出：\n\n```text\n[MISSING] /private/tmp/sn999/ghost.py:7 不存在\n```\n",
        )
        repo.track()
        result = repo.run()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        # The reference is text inside the fence, not a reference: counted as skipped.
        self.assertIn("refs=0 fenced=1", result.stdout)
        self.assertIn("RESULT PASS", result.stdout)

    def test_a_misplaced_terminal_document_under_reviews_turns_red(self) -> None:
        repo = Repository(self)
        repo.write("docs/reviews/terminal-notes.md", f"实现见 `{REAL_SOURCE}:3`。\n")
        repo.track()
        result = repo.run()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("LAYOUT docs/reviews/terminal-notes.md", result.stdout)
        self.assertIn("RESULT FAIL", result.stdout)


class RatchetTests(unittest.TestCase):
    """The baseline accepts what is already there and nothing more."""

    def test_one_accepted_problem_does_not_hide_the_next_one(self) -> None:
        repo = Repository(self)
        repo.write(TERMINAL_DOC, f"实现见 `{GHOST_REF}`。\n")
        repo.track()
        written = repo.run("--write-baseline", "baseline.json")
        self.assertEqual(written.returncode, 0, written.stdout + written.stderr)
        self.assertIn("terminal=1", written.stdout)
        payload = json.loads((repo.root / "baseline.json").read_text(encoding="utf-8"))
        self.assertEqual(payload["terminal"], [f"{TERMINAL_DOC}::{GHOST_REF}"], payload)

        accepted = repo.run("--baseline", "baseline.json")
        self.assertEqual(accepted.returncode, 0, accepted.stdout + accepted.stderr)
        self.assertIn("[基线已接受]", accepted.stdout)
        self.assertIn("new=0 accepted=1", accepted.stdout)

        repo.write(TERMINAL_DOC, f"实现见 `{GHOST_REF}` 与 `{OTHER_GHOST_REF}`。\n")
        repo.track()
        refused = repo.run("--baseline", "baseline.json")
        self.assertEqual(refused.returncode, 1, refused.stdout + refused.stderr)
        self.assertIn(OTHER_GHOST_REF, refused.stdout)
        self.assertIn("new=1 accepted=1", refused.stdout)
        self.assertIn("RESULT FAIL", refused.stdout)

    def test_a_stale_baseline_entry_is_reported_not_fatal(self) -> None:
        repo = Repository(self)
        repo.write(TERMINAL_DOC, f"实现见 `{GHOST_REF}`。\n")
        repo.track()
        self.assertEqual(repo.run("--write-baseline", "baseline.json").returncode, 0)
        repo.write(TERMINAL_DOC, f"实现见 `{REAL_SOURCE}:3`。\n")
        repo.track()
        result = repo.run("--baseline", "baseline.json")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("BASELINE stale=1", result.stdout)

    def test_an_unreadable_baseline_is_an_environment_error(self) -> None:
        repo = Repository(self)
        repo.write(TERMINAL_DOC, f"实现见 `{REAL_SOURCE}:3`。\n")
        repo.track()
        repo.write("baseline.json", "not json\n")
        result = repo.run("--baseline", "baseline.json")
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("baseline invalid", result.stderr)


class CommittedStateTests(unittest.TestCase):
    """The committed checkout and the committed baseline stay consistent."""

    def test_the_checkout_passes_against_the_committed_baseline(self) -> None:
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--baseline", str(BASELINE)],
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            env={**os.environ, "PYTHONIOENCODING": "utf-8"},
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("SCOPE terminal problems=0", result.stdout)
        self.assertIn("RESULT PASS", result.stdout)

    def test_the_committed_baseline_keeps_terminal_documents_fully_gated(self) -> None:
        payload = json.loads(BASELINE.read_text(encoding="utf-8"))
        self.assertEqual(payload["schema"], 1)
        # An empty `terminal` list is what makes this a full gate instead of a
        # ratchet that already tolerates drift.
        self.assertEqual(payload["terminal"], [], payload)
        # Process-record counts stay in the report; they are not a gate.
        self.assertIn("docs/reviews", payload["records"], payload)
        for verdict, count in payload["records"]["docs/reviews"].items():
            self.assertIsInstance(count, int, verdict)

    def test_the_script_parses_as_the_runner_system_python(self) -> None:
        # The job uses the runner's `python3` (3.12 on ubuntu-24.04) without uv, so
        # 3.14-only syntax would break the lane at import time instead of failing.
        ast.parse(SCRIPT.read_text(encoding="utf-8"), filename=str(SCRIPT), feature_version=(3, 12))


class WorkflowTests(unittest.TestCase):
    """The lane exists, has no scope filter, and the protected gate requires it."""

    def test_the_job_runs_the_check_with_the_committed_baseline(self) -> None:
        block = job_block(JOB)
        self.assertIn('name: "Docs: path:line references"', block)
        self.assertIn(CI_COMMAND, block)
        self.assertEqual(WORKFLOW.count(CI_COMMAND), 1)
        self.assertIn("runs-on: ubuntu-24.04", block)
        self.assertNotIn("continue-on-error", block)
        self.assertNotIn("uv run", block)

    def test_the_job_has_no_scope_filter(self) -> None:
        # A source change can invalidate a terminal document reference just as a
        # prose change can, and a `docs`-scope PR is exactly the one that must
        # reach the check; a job-level `if:` would skip it there.
        self.assertNotIn("\n    if:", job_block(JOB))

    def test_the_protected_gate_requires_its_result(self) -> None:
        gate = WORKFLOW.split(f"\n  {GATE}:\n", 1)[1]
        needs = gate.split("needs:", 1)[1].split("if:", 1)[0]
        self.assertIn(f"\n        {JOB},\n", needs)
        self.assertIn(f"{JOB.upper()}_RESULT: ${{{{ needs.{JOB}.result }}}}", gate)
        self.assertIn(f'if [[ "${JOB.upper()}_RESULT" != success ]]', gate)

    def test_the_validation_record_documents_the_gate(self) -> None:
        document = (ROOT / "docs/validation/ci-validation.md").read_text(encoding="utf-8")
        section = document.split("## 文档引用门禁", 1)[1].split("## 公开语音路由一致性门禁", 1)[0]
        for required in ("docs_refs", "docs/reviews/", "fenced", "doc-refs-baseline.json"):
            with self.subTest(required=required):
                self.assertIn(required, section)
        self.assertNotIn("未接入 CI 的本地报告工具", document)


if __name__ == "__main__":
    unittest.main()
