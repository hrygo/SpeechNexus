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
import importlib.util
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
_SPEC = importlib.util.spec_from_file_location("check_doc_refs", SCRIPT)
assert _SPEC is not None and _SPEC.loader is not None
CHECK = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(CHECK)

JOB = "docs_refs"
GATE = "required_checks"
CI_COMMAND = (
    "python3 scripts/dev-env/check-doc-refs.py --baseline scripts/dev-env/doc-refs-baseline.json"
)
TERMINAL_DOC = "docs/operations/runbook.md"
REVIEW_DOC = "docs/reviews/2026-11-16-review.md"
REVIEW_BODY = "# 引用门禁的非作者审查（#859）\n\n当时指向 `{ref}`。\n"
REAL_SOURCE = "backend/src/speechnexus_backend/real.py"
DOC_TARGET = "docs/operations/target.md"
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


def bare_code_reference_count() -> int:
    """Terminal references to non-Markdown targets that still carry no assertion.

    Mirrors the counter in `main`, so the report is checked against the tree
    instead of a pinned number. The debt is supposed to reach zero; asserting
    `> 0` would turn red on the day it is finally paid off.
    """
    total = 0
    for root in ("docs", "deploy"):
        for doc in sorted((ROOT / root).rglob("*.md")):
            if CHECK.scope_of(doc.relative_to(ROOT).as_posix()) != CHECK.TERMINAL:
                continue
            refs, _ = CHECK.scan_document(doc)
            for _, line, ref_path, _, assertion in refs:
                if "外部依赖" in line or assertion or ref_path.endswith(".md"):
                    continue
                total += 1
    return total


class Repository:
    """A throwaway git repository the script can be pointed at."""

    def __init__(self, case: unittest.TestCase) -> None:
        temporary = tempfile.TemporaryDirectory()
        case.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self._git("init", "-q")
        self.write(REAL_SOURCE, "".join(f"LINE_{number} = {number}\n" for number in range(1, 6)))
        # A document target long enough to tell the assertion window apart from a
        # real drift: 60 lines, one recognisable marker per line.
        self.write(DOC_TARGET, "".join(f"TARGET_{number} = {number}\n" for number in range(1, 61)))

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
        repo.write(REVIEW_DOC, REVIEW_BODY.format(ref=GHOST_REF))
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
        repo.write(REVIEW_DOC, REVIEW_BODY.format(ref=GHOST_REF))
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

    def test_a_terminal_document_copied_under_a_dated_record_name_turns_red(self) -> None:
        # Only checking the file name would let a real document be parked here and
        # its references silently demoted to records; the content has to look like a
        # review report too.
        repo = Repository(self)
        repo.write(
            "docs/reviews/2026-11-16-speech-api-duplicate.md",
            f"# 语音接口设计\n\n实现见 `{REAL_SOURCE}:3`，另有 `{GHOST_REF}`。\n",
        )
        repo.track()
        result = repo.run()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("LAYOUT docs/reviews/2026-11-16-speech-api-duplicate.md", result.stdout)
        self.assertIn("内容不是审查报告", result.stdout)
        self.assertIn("RESULT FAIL", result.stdout)

    def test_a_dated_record_without_an_issue_reference_turns_red(self) -> None:
        repo = Repository(self)
        repo.write("docs/reviews/2026-11-16-review.md", "# 引用门禁的非作者审查\n\n没有卡号。\n")
        repo.track()
        result = repo.run()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("LAYOUT docs/reviews/2026-11-16-review.md", result.stdout)


class AssertionTests(unittest.TestCase):
    """A doc->doc reference must say *what* sits at the line, not only which line.

    #1038: the six pointers in `docs/releases/mvp-0.1.0.md` were right when they
    were written and had drifted 74-142 lines by the time anyone noticed, while
    the gate stayed green - the line existed, so MISSING/OUT_OF_RANGE never fired.
    These cases pin the replacement rule: the cited line (or the window around it)
    must carry the fragment the reference names.
    """

    def test_a_matching_assertion_passes(self) -> None:
        repo = Repository(self)
        repo.write(TERMINAL_DOC, f"见 `{DOC_TARGET}:30`（该行须含 `TARGET_30`）。\n")
        repo.track()
        result = repo.run()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("ASSERT doc_refs=1 asserted=1", result.stdout)
        self.assertIn("RESULT PASS", result.stdout)

    def test_a_bare_document_reference_turns_red(self) -> None:
        repo = Repository(self)
        repo.write(TERMINAL_DOC, f"见 `{DOC_TARGET}:30`。\n")
        repo.track()
        result = repo.run()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("[UNASSERTED]", result.stdout)
        self.assertIn("ASSERT doc_refs=1 asserted=0", result.stdout)
        self.assertIn("SCOPE terminal problems=1 new=1 accepted=0", result.stdout)
        self.assertIn("RESULT FAIL", result.stdout)

    def test_an_assertion_that_is_not_at_the_line_turns_red(self) -> None:
        # The counterexample the rule exists for: the line still exists and is in
        # range, only its content is not what the document claims.
        repo = Repository(self)
        repo.write(TERMINAL_DOC, f"见 `{DOC_TARGET}:30`（该行须含 `NOT_ANYWHERE_1234`）。\n")
        repo.track()
        result = repo.run()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("[DRIFT]", result.stdout)
        self.assertIn("全文也没有", result.stdout)
        self.assertIn("RESULT FAIL", result.stdout)

    def test_a_wrong_line_number_turns_red(self) -> None:
        repo = Repository(self)
        repo.write(TERMINAL_DOC, f"见 `{DOC_TARGET}:52`（该行须含 `TARGET_3 = 3`）。\n")
        repo.track()
        result = repo.run()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("[DRIFT]", result.stdout)
        self.assertIn("最近在第 3 行（-49 行）", result.stdout)
        self.assertIn("RESULT FAIL", result.stdout)

    def test_the_window_tolerates_a_small_insertion(self) -> None:
        # Ten lines inserted above the target shift it to 40; the reference still
        # names the same content and stays green - the window is what buys that.
        repo = Repository(self)
        repo.write(TERMINAL_DOC, f"见 `{DOC_TARGET}:30`（该行须含 `TARGET_30`）。\n")
        repo.write(
            DOC_TARGET,
            "".join(f"FILLER_{number}\n" for number in range(1, 11))
            + "".join(f"TARGET_{number} = {number}\n" for number in range(1, 61)),
        )
        repo.track()
        result = repo.run()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        # The counter proves the ref was parsed and asserted, not merely ignored:
        # a broken parser would also print RESULT PASS.
        self.assertIn("ASSERT doc_refs=1 asserted=1", result.stdout)
        self.assertIn("RESULT PASS", result.stdout)

    def test_fifteen_inserted_lines_are_still_inside_the_window(self) -> None:
        # Content lands on 45, the last line the +-15 window covers: green. This
        # pins the constant's meaning, not just "a large drift is red".
        repo = Repository(self)
        repo.write(TERMINAL_DOC, f"见 `{DOC_TARGET}:30`（该行须含 `TARGET_30`）。\n")
        repo.write(
            DOC_TARGET,
            "".join(f"FILLER_{number}\n" for number in range(1, 16))
            + "".join(f"TARGET_{number} = {number}\n" for number in range(1, 61)),
        )
        repo.track()
        result = repo.run()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("RESULT PASS", result.stdout)

    def test_sixteen_inserted_lines_are_one_past_the_window(self) -> None:
        # Content lands on 46: one line outside, so DRIFT and the exact distance.
        repo = Repository(self)
        repo.write(TERMINAL_DOC, f"见 `{DOC_TARGET}:30`（该行须含 `TARGET_30`）。\n")
        repo.write(
            DOC_TARGET,
            "".join(f"FILLER_{number}\n" for number in range(1, 17))
            + "".join(f"TARGET_{number} = {number}\n" for number in range(1, 61)),
        )
        repo.track()
        result = repo.run()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("[DRIFT]", result.stdout)
        self.assertIn("最近在第 46 行（+16 行）", result.stdout)

    def test_the_window_catches_the_real_drift(self) -> None:
        # Forty inserted lines put the content at 70, outside +-15: red. #1038's
        # drift was 74-142, an order of magnitude above the window.
        repo = Repository(self)
        repo.write(TERMINAL_DOC, f"见 `{DOC_TARGET}:30`（该行须含 `TARGET_30`）。\n")
        repo.write(
            DOC_TARGET,
            "".join(f"FILLER_{number}\n" for number in range(1, 41))
            + "".join(f"TARGET_{number} = {number}\n" for number in range(1, 61)),
        )
        repo.track()
        result = repo.run()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("[DRIFT]", result.stdout)
        self.assertIn("最近在第 70 行（+40 行）", result.stdout)

    def test_a_bare_code_reference_is_reported_but_not_gated(self) -> None:
        # Code targets are the remaining debt: counted in the report, not required
        # yet. A bare Markdown target is gated, so the two must not be confused.
        repo = Repository(self)
        repo.write(TERMINAL_DOC, f"实现见 `{REAL_SOURCE}:3`。\n")
        repo.track()
        result = repo.run()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("unasserted_code=1", result.stdout)
        self.assertIn("RESULT PASS", result.stdout)

    def test_an_assertion_on_a_record_reference_is_not_checked(self) -> None:
        repo = Repository(self)
        repo.write(REVIEW_DOC, REVIEW_BODY.format(ref=f"{DOC_TARGET}:30"))
        repo.track()
        result = repo.run()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("SCOPE terminal problems=0", result.stdout)
        self.assertIn("RESULT PASS", result.stdout)

    def test_a_dated_audit_file_is_a_record(self) -> None:
        # `docs/audits/` takes dated point-in-time files; a bare reference there
        # must not gate, or a dated audit report could dodge the review-title guard
        # simply by living in the audit directory.
        repo = Repository(self)
        repo.write("docs/audits/2026-11-20-once.md", f"# 探针\n\n见 `{DOC_TARGET}:30`。\n")
        repo.track()
        result = repo.run()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("SCOPE terminal problems=0", result.stdout)
        self.assertIn("RESULT PASS", result.stdout)

    def test_deploy_is_scanned_by_default(self) -> None:
        # `deploy/README.md` is a terminal document; its own references rot like
        # any other, so the default roots cover it without a flag.
        repo = Repository(self)
        repo.write("deploy/README.md", f"实现见 `{DOC_TARGET}:30`。\n")
        repo.track()
        result = repo.run()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("[UNASSERTED]", result.stdout)
        self.assertIn("deploy/README.md", result.stdout)


class ShowModeTests(unittest.TestCase):
    """`--show` is a troubleshooting view: problems with their target line."""

    def test_show_prints_problems_but_not_ok_references(self) -> None:
        repo = Repository(self)
        repo.write(TERMINAL_DOC, f"实现见 `{REAL_SOURCE}:3`。\n见 `{GHOST_REF}`。\n")
        repo.track()
        result = repo.run("--show")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        # The missing target is printed with its own line...
        self.assertIn("ghost.py:7", result.stdout)
        # ...and the healthy reference is not, so a DRIFT is not buried among the
        # thousands of OK rows the old `--show` printed.
        self.assertNotIn(REAL_SOURCE, result.stdout)


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

    def test_every_terminal_document_reference_is_asserted(self) -> None:
        """The corpus guard: no terminal ref to a document is allowed to stay bare.

        The gate turns red for a bare one, so an equal count is the proof that the
        checkout carries none; the count itself keeps the corpus from silently
        shrinking to nothing.
        """
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--baseline", str(BASELINE)],
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            env={**os.environ, "PYTHONIOENCODING": "utf-8"},
            check=False,
        )
        match = re.search(
            r"ASSERT doc_refs=(\d+) asserted=(\d+) window=±(\d+) unasserted_code=(\d+)",
            result.stdout,
        )
        self.assertIsNotNone(match, result.stdout)
        assert match is not None
        doc_refs, asserted, window, unasserted_code = (int(value) for value in match.groups())
        # Scale-independent: ten references and a thousand both pass as long as
        # every document reference carries an assertion, and a corpus that shrank
        # to nothing cannot pass by being trivially consistent.
        self.assertEqual(doc_refs, asserted, result.stdout)
        self.assertGreater(doc_refs, 0, result.stdout)
        # The window is a documented number, not an accident of the default.
        self.assertEqual(window, CHECK.ASSERTION_WINDOW)
        # The debt counter is a count, not a threshold: compare it with the tree
        # itself, so paying the debt down (365 bare code refs today) never turns
        # this red. A fixed `> 0` would fail exactly on the day the work finishes.
        self.assertEqual(unasserted_code, bare_code_reference_count(), result.stdout)

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


class LayoutGuardCorpusTests(unittest.TestCase):
    """The record-layout guard still separates the real corpus, not just a sample."""

    def test_every_review_report_satisfies_the_guard(self) -> None:
        for doc in sorted((ROOT / "docs/reviews").glob("*.md")):
            relative = doc.relative_to(ROOT).as_posix()
            with self.subTest(report=relative):
                self.assertIsNone(
                    CHECK.review_layout_problem(relative, doc.read_text(encoding="utf-8"))
                )

    def test_no_terminal_document_survives_a_copy_into_reviews(self) -> None:
        # This is the claim the guard and `ci-validation.md` make: a terminal
        # document parked under a compliant review file name is rejected. If a new
        # terminal document ever slips through, either its title/heading needs to
        # stay distinguishable from a review report or this predicate needs to be
        # revisited - the guard must not claim more than it does.
        parked = "docs/reviews/2026-11-16-parked.md"
        documents = [
            path
            for path in sorted((ROOT / "docs").rglob("*.md"))
            if CHECK.scope_of(path.relative_to(ROOT).as_posix()) == CHECK.TERMINAL
        ]
        self.assertGreater(len(documents), 100)
        for doc in documents:
            relative = doc.relative_to(ROOT).as_posix()
            with self.subTest(document=relative):
                self.assertIsNotNone(
                    CHECK.review_layout_problem(parked, doc.read_text(encoding="utf-8")),
                    f"{relative} 的副本会绕过 docs/reviews/ 的布局守卫",
                )


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
        for required in (
            "docs_refs",
            "docs/reviews/",
            "fenced",
            "fenced_terminal",
            "doc-refs-baseline.json",
            "一级标题须含审查",
        ):
            with self.subTest(required=required):
                self.assertIn(required, section)
        self.assertNotIn("未接入 CI 的本地报告工具", document)


if __name__ == "__main__":
    unittest.main()
