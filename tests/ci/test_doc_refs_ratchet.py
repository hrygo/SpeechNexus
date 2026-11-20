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
# The #1049 counterexample shape: sixteen inserted lines put `TARGET_30` at line 46,
# one line past the +-15 window, so a reference to line 30 turns `DRIFT`.
FILLER_16 = "".join(f"FILLER_{number}\n" for number in range(1, 17))
TARGETS = "".join(f"TARGET_{number} = {number}\n" for number in range(1, 61))


def job_block(job_id: str) -> str:
    """The YAML block of one job, bounded by the next job-level key."""
    marker = f"\n  {job_id}:\n"
    start = WORKFLOW.index(marker)
    remainder = WORKFLOW[start + len(marker) :]
    following = re.search(r"^  [a-z_]+:\n", remainder, re.MULTILINE)
    return WORKFLOW[
        start : start + len(marker) + (following.start() if following else len(remainder))
    ]


MARKDOWN_REF = re.compile(r"`([\w][\w./-]*\.(?:py|sql|ts|tsx|js|mjs|sh|toml|yaml|yml|json)):(\d+)`")
RECORD_PATH = re.compile(
    r"^docs/(reviews|archive)/|^docs/(validation|audits)/\d{4}-\d{2}-\d{2}-.+\.md$"
)
FENCE_LINE = re.compile(r"^ {0,3}(`{3,}|~{3,})")


def independent_bare_code_refs() -> int:
    """A lower bound on `unasserted_code`, counted without the gate's own parser.

    Deliberately a strict subset of what `main` counts: only backticked paths with
    a known extension, only outside fences, only lines carrying neither an
    assertion nor an external-dependency marker. A regression that makes the
    gate's regex stop matching a whole class of references would drop its own
    counter to zero while this count stayed put, so the two disagree - a check
    that mirrored `main` (same `scope_of`, same `scan_document`) would agree on
    zero and pass.
    """
    total = 0
    for root in ("docs", "deploy"):
        for doc in sorted((ROOT / root).rglob("*.md")):
            rel = doc.relative_to(ROOT).as_posix()
            if RECORD_PATH.search(rel):
                continue
            in_fence = False
            for line in doc.read_text(encoding="utf-8").splitlines():
                if FENCE_LINE.match(line):
                    in_fence = not in_fence
                    continue
                if in_fence or "该行须含" in line or "外部依赖" in line:
                    continue
                total += len(MARKDOWN_REF.findall(line))
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
        self.assertIn("RECORDS new=+1 stale=+0", result.stdout)
        self.assertIn("RECORDS new docs/reviews MISSING=+1", result.stdout)
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


class RecordsBaselineTests(unittest.TestCase):
    """The records baseline is a problem-key set, so the delta cannot go stale.

    #1063: `records` used to store per-directory verdict counts, refreshed by hand
    with `--write-baseline`. Nobody refreshes a display-only delta, so the report
    kept printing "baseline 127 / measured 140" and those 13 read as if the current
    PR had introduced them. The baseline now carries the keys it has seen
    (`<record flow>::<reference>::<verdict>`) and the report prints only the
    difference: `new=+N` for keys that were not in the baseline, `stale=+M` for
    keys that stopped hitting. Neither side gates.
    """

    def test_a_new_record_is_an_increment_not_two_counts(self) -> None:
        # #1063: the baseline used to store absolute counts, refreshed by hand.
        # Nobody refreshes for a display-only delta, so the report showed "baseline
        # 127 / measured 140" and the 13 looked like this PR's doing. With a key
        # set the report can only say +1 - and deleting the record returns to +0.
        repo = self._seeded()
        added = "docs/reviews/2026-11-20-added.md"
        repo.write(added, f"# 新增审查（#1063）\n\n当时指向 `{OTHER_GHOST_REF}`。\n")
        repo.track()
        result = repo.run("--baseline", "baseline.json")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("RECORDS new=+1 stale=+0", result.stdout)
        self.assertIn("RECORDS new docs/reviews MISSING=+1", result.stdout)
        # No "old count -> new count" pair may come back: that pair is the lie.
        self.assertNotIn("->", result.stdout.split("SCOPE record", 1)[1])
        self.assertNotIn("RESULT FAIL", result.stdout)

        (repo.root / added).unlink()
        repo.track()
        back = repo.run("--baseline", "baseline.json")
        self.assertEqual(back.returncode, 0, back.stdout + back.stderr)
        self.assertIn("RECORDS new=+0 stale=+0", back.stdout)
        self.assertIn("SCOPE record problems=1 docs/reviews MISSING=1", back.stdout)

    def test_a_record_problem_that_disappeared_is_reported_as_stale(self) -> None:
        # Requirement 2: a key the baseline accepted and that no longer hits must
        # not be swallowed - it is reported as `stale`, and still does not gate.
        repo = self._seeded()
        (repo.root / REVIEW_DOC).unlink()
        repo.track()
        result = repo.run("--baseline", "baseline.json")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("RECORDS new=+0 stale=+1", result.stdout)
        self.assertIn("RECORDS stale docs/reviews MISSING=+1", result.stdout)
        self.assertIn("计为消失", result.stdout)
        self.assertIn("SCOPE record problems=0", result.stdout)
        self.assertIn("RESULT PASS", result.stdout)

    def test_same_target_at_another_line_is_another_problem(self) -> None:
        # The comparison axis moved from counts to keys, so this pins the key: the
        # record problem count stays 1 on both sides, and a count-based diff would
        # report nothing. Only the target line changed, and that is a new problem.
        repo = self._seeded()
        repo.write(REVIEW_DOC, REVIEW_BODY.format(ref="backend/src/speechnexus_backend/ghost.py:9"))
        repo.track()
        result = repo.run("--baseline", "baseline.json")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("SCOPE record problems=1 docs/reviews MISSING=1", result.stdout)
        self.assertIn("RECORDS new=+1 stale=+1", result.stdout)

    def test_the_record_delta_does_not_move_the_terminal_gate(self) -> None:
        # Requirement 3: the delta is display only. A new record plus a new
        # terminal problem still fails on the terminal problem alone.
        repo = self._seeded()
        repo.write(TERMINAL_DOC, f"实现见 `{GHOST_REF}`。\n")
        repo.write(
            "docs/reviews/2026-11-20-added.md",
            f"# 新增审查（#1063）\n\n当时指向 `{OTHER_GHOST_REF}`。\n",
        )
        repo.track()
        result = repo.run("--baseline", "baseline.json")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("SCOPE terminal problems=1 new=1 accepted=0", result.stdout)
        self.assertIn("RECORDS new=+1", result.stdout)
        self.assertIn("RESULT FAIL", result.stdout)

    def _seeded(self) -> Repository:
        """基线里登记了恰好一条过程记录问题的仓库."""
        repo = Repository(self)
        repo.write(REVIEW_DOC, REVIEW_BODY.format(ref=GHOST_REF))
        repo.track()
        written = repo.run("--write-baseline", "baseline.json")
        self.assertEqual(written.returncode, 0, written.stdout + written.stderr)
        payload = json.loads((repo.root / "baseline.json").read_text(encoding="utf-8"))
        self.assertEqual(payload["records"], [f"{REVIEW_DOC}::{GHOST_REF}::MISSING"], payload)
        return repo


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


class FixModeTests(unittest.TestCase):
    """`--fix` moves the line number, and only when the fragment names one line.

    #1049: sixteen inserted lines turn 21 references red at once, and repairing
    them by hand is one edit per referencing document. The tool may do the
    mechanical part, but only when the fragment identifies exactly one line.
    Picking among several matches would move a pointer to the wrong place while
    the gate went green, which is worse than leaving it red.
    """

    def _drifted(self, case: unittest.TestCase, body: str | None = None) -> Repository:
        repo = Repository(case)
        repo.write(TERMINAL_DOC, body or f"见 `{DOC_TARGET}:30`（该行须含 `TARGET_30`）。\n")
        repo.write(DOC_TARGET, FILLER_16 + TARGETS)
        repo.track()
        return repo

    def _status(self, repo: Repository) -> str:
        return subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=repo.root,
            capture_output=True,
            text=True,
            check=True,
        ).stdout

    def test_dry_run_lists_the_plan_and_writes_nothing(self) -> None:
        repo = self._drifted(self)
        before = (repo.root / TERMINAL_DOC).read_text(encoding="utf-8")
        status_before = self._status(repo)
        result = repo.run("--fix", "--dry-run")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        # The plan names the reference site, the old and new line, and the hit.
        self.assertIn("[FIX]", result.stdout)
        self.assertIn(f"{DOC_TARGET}:30  30 -> 46 (+16)", result.stdout)
        self.assertIn("命中：TARGET_30 = 30", result.stdout)
        self.assertIn("FIX RESULT PASS", result.stdout)
        self.assertIn("--dry-run 未写文件", result.stdout)
        # Nothing on disk moved: same bytes, same `git status`.
        self.assertEqual((repo.root / TERMINAL_DOC).read_text(encoding="utf-8"), before)
        self.assertEqual(self._status(repo), status_before)

    def test_fix_rewrites_only_the_line_number_and_the_gate_passes(self) -> None:
        repo = self._drifted(self)
        before = (repo.root / TERMINAL_DOC).read_text(encoding="utf-8")
        result = repo.run("--fix")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("30 -> 46", result.stdout)
        self.assertIn("SELFCHECK written=1 failures=0 drift_before=1 drift_after=0", result.stdout)
        after = (repo.root / TERMINAL_DOC).read_text(encoding="utf-8")
        self.assertEqual(after, before.replace(f"{DOC_TARGET}:30", f"{DOC_TARGET}:46"))
        # The assertion text is untouched; only the digits moved. Comparing the
        # two changed lines with the number mapped back proves that directly.
        diff = subprocess.run(
            ["git", "diff", "--unified=0"],
            cwd=repo.root,
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        changed = [
            line[1:]
            for line in diff.splitlines()
            if line[:1] in "+-" and not line.startswith(("+++", "---"))
        ]
        self.assertEqual(len(changed), 2, diff)
        self.assertIn(f"{DOC_TARGET}:30", changed[0], diff)
        self.assertIn(f"{DOC_TARGET}:46", changed[1], diff)
        normalized = [re.sub(r"\.md:\d+", ".md:N", line) for line in changed]
        self.assertEqual(normalized[0], normalized[1], diff)
        gate = repo.run()
        self.assertEqual(gate.returncode, 0, gate.stdout + gate.stderr)
        self.assertIn("RESULT PASS", gate.stdout)

    def test_an_ambiguous_fragment_is_left_alone_and_the_rest_is_fixed(self) -> None:
        # `TARGET_30` appears on lines 46 and 77: the tool must not choose. The
        # neighbouring `TARGET_31` reference is unique and still gets repaired.
        repo = self._drifted(
            self,
            f"见 `{DOC_TARGET}:30`（该行须含 `TARGET_30`）。\n"
            f"见 `{DOC_TARGET}:31`（该行须含 `TARGET_31`）。\n",
        )
        repo.write(DOC_TARGET, FILLER_16 + TARGETS + "TARGET_30 = 30\n")
        result = repo.run("--fix")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("[UNFIXABLE：", result.stdout)
        self.assertIn("出现 2 次", result.stdout)
        self.assertIn("46、77", result.stdout)
        lines = (repo.root / TERMINAL_DOC).read_text(encoding="utf-8").splitlines()
        self.assertIn(f"{DOC_TARGET}:30", lines[0], "多个命中时不许改这条")
        self.assertIn(f"{DOC_TARGET}:47", lines[1], "其余引用照改")
        self.assertIn("SELFCHECK written=1 failures=0 drift_before=2 drift_after=1", result.stdout)
        self.assertIn("FIX UNRESOLVED drift=1", result.stdout)
        self.assertIn("RESULT FAIL", result.stdout)

    def test_a_fragment_that_is_gone_is_reported_without_a_rewrite(self) -> None:
        # Zero hits is the other side of "do not guess": there is no line to move
        # the reference to, so the DRIFT stays and the command exits non-zero.
        repo = Repository(self)
        body = f"见 `{DOC_TARGET}:30`（该行须含 `NOT_ANYWHERE_1234`）。\n"
        repo.write(TERMINAL_DOC, body)
        repo.track()
        result = repo.run("--fix")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("出现 0 次", result.stdout)
        self.assertIn("FIX UNRESOLVED drift=1", result.stdout)
        self.assertEqual((repo.root / TERMINAL_DOC).read_text(encoding="utf-8"), body)

    def _scan(self, total: int) -> object:
        return CHECK.Scan(
            results=[],
            scanned=[],
            total=total,
            fenced=0,
            fenced_terminal=0,
            doc_refs=0,
            doc_refs_asserted=0,
            unasserted_code=0,
        )

    def test_self_check_rejects_an_off_by_one_write(self) -> None:
        # The self-check must not reuse the +-15 assertion window: a writer that
        # lands one line off would still look green there, which is exactly the
        # mutation #1049's review ran. Line 31 holds TARGET_31, not TARGET_30.
        repo = Repository(self)
        repo.write(DOC_TARGET, TARGETS)
        repo.track()
        result = {
            "doc": TERMINAL_DOC,
            "doc_line": 1,
            "ref": f"{DOC_TARGET}:30",
            "verdict": "DRIFT",
            "detail": "",
            "target": DOC_TARGET,
            "line": 30,
            "scope": CHECK.TERMINAL,
            "assertion": "TARGET_30",
        }
        plan = CHECK.FixPlan(result, [(30, 32)], 31, "")
        scan = self._scan(1)
        failures = CHECK.self_check(repo.root, [plan], scan, scan)
        self.assertEqual(len(failures), 1, failures)
        self.assertIn("该行没有 `TARGET_30`", failures[0])

    def test_self_check_rejects_a_reference_count_drop(self) -> None:
        # A write that deletes references (refs=2175 -> 2146) leaves every
        # surviving reference valid, so only the total can catch it.
        repo = Repository(self)
        repo.track()
        failures = CHECK.self_check(repo.root, [], self._scan(10), self._scan(9))
        self.assertEqual(len(failures), 1, failures)
        self.assertIn("引用总数 10 -> 9", failures[0])

    def test_dry_run_does_not_report_pass_when_the_gate_is_red(self) -> None:
        # No DRIFT means nothing to fix, but the gate is still red for MISSING.
        # A dry-run that exits 0 here would read as "ready to merge".
        repo = Repository(self)
        repo.write(TERMINAL_DOC, f"实现见 `{GHOST_REF}`。\n")
        repo.track()
        status_before = self._status(repo)
        result = repo.run("--fix", "--dry-run")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("计划外仍有 1 条基线外终态问题", result.stdout)
        self.assertIn("FIX RESULT FAIL", result.stdout)
        self.assertNotIn("FIX RESULT PASS", result.stdout)
        self.assertEqual(self._status(repo), status_before)

    def test_fix_includes_a_review_layout_problem_in_its_exit_code(self) -> None:
        # The module docstring says the exit code follows the gate; layout is part
        # of the gate, so `--fix` must not print PASS while the job would fail.
        repo = Repository(self)
        repo.write("docs/reviews/terminal-notes.md", f"实现见 `{REAL_SOURCE}:3`。\n")
        repo.track()
        written = repo.run("--fix")
        self.assertEqual(written.returncode, 1, written.stdout + written.stderr)
        self.assertIn("LAYOUT docs/reviews/terminal-notes.md", written.stdout)
        self.assertIn("RESULT FAIL", written.stdout)
        dry = repo.run("--fix", "--dry-run")
        self.assertEqual(dry.returncode, 1, dry.stdout + dry.stderr)
        self.assertIn("过程记录布局问题", dry.stdout)

    def test_fix_never_touches_process_records(self) -> None:
        # DRIFT is only produced for terminal documents; `--fix` must keep that
        # scope. A record that cites the same target at an old line stays frozen.
        repo = Repository(self)
        record = "docs/reviews/2026-11-16-review.md"
        record_body = f"# 引用门禁的非作者审查（#1049）\n\n当时指向 `{DOC_TARGET}:3`。\n"
        repo.write(record, record_body)
        repo.write(TERMINAL_DOC, f"见 `{DOC_TARGET}:30`（该行须含 `TARGET_30`）。\n")
        repo.write(DOC_TARGET, FILLER_16 + TARGETS)
        repo.track()
        result = repo.run("--fix")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual((repo.root / record).read_text(encoding="utf-8"), record_body)
        terminal = (repo.root / TERMINAL_DOC).read_text(encoding="utf-8")
        self.assertIn(f"{DOC_TARGET}:46", terminal)

    def test_two_references_on_one_line_are_both_rewritten(self) -> None:
        # The rewrite works on digit spans, right to left: a naive left-to-right
        # replacement would shift the second span and corrupt the line.
        repo = self._drifted(
            self,
            f"见 `{DOC_TARGET}:30`（该行须含 `TARGET_30`）与"
            f"`{DOC_TARGET}:31`（该行须含 `TARGET_31`）。\n",
        )
        result = repo.run("--fix")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        line = (repo.root / TERMINAL_DOC).read_text(encoding="utf-8")
        self.assertIn(f"{DOC_TARGET}:46", line)
        self.assertIn(f"{DOC_TARGET}:47", line)


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


class ShowOkTests(unittest.TestCase):
    """`--show-ok` returns the OK-side excerpts to whoever is troubleshooting.

    #1049: `--show` printing only problems is right, but then "why does this OK
    reference count as OK?" and "did the fragment happen to match another line in
    the same file?" can only be answered by `sed -n` on every reference. The flag
    prints the referenced line verbatim, from the same helper the problem view
    uses, so the two sides cannot disagree.
    """

    def test_show_ok_prints_the_referenced_line_verbatim(self) -> None:
        repo = Repository(self)
        repo.write(TERMINAL_DOC, f"见 `{DOC_TARGET}:30`（该行须含 `TARGET_30`）。\n")
        repo.track()
        result = repo.run("--show", "--show-ok")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        target_line = (repo.root / DOC_TARGET).read_text(encoding="utf-8").splitlines()[29]
        self.assertIn(target_line, result.stdout)
        self.assertIn(f"[OK] {TERMINAL_DOC}:1", result.stdout)
        # The default view still hides OK rows: the flag is the explicit request.
        plain = repo.run("--show")
        self.assertNotIn(target_line, plain.stdout)

    def test_show_ok_covers_code_references_too(self) -> None:
        # The OK side is not only document targets; a bare code reference is the
        # other half of the corpus and needs the same excerpt.
        repo = Repository(self)
        repo.write(TERMINAL_DOC, f"实现见 `{REAL_SOURCE}:3`。\n")
        repo.track()
        result = repo.run("--show-ok")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("LINE_3 = 3", result.stdout)


class ShowTierTests(unittest.TestCase):
    """`--show` keeps the new-vs-accepted marker while printing the excerpt."""

    def test_show_marks_new_and_accepted_terminal_problems(self) -> None:
        repo = Repository(self)
        repo.write(TERMINAL_DOC, f"实现见 `{GHOST_REF}`。\n")
        repo.track()
        written = repo.run("--write-baseline", "baseline.json", "--accept-new-terminal")
        self.assertEqual(written.returncode, 0, written.stdout + written.stderr)
        accepted = repo.run("--baseline", "baseline.json", "--show")
        self.assertEqual(accepted.returncode, 0, accepted.stdout + accepted.stderr)
        self.assertIn("[MISSING]", accepted.stdout)
        self.assertIn("[基线已接受]", accepted.stdout)
        self.assertNotIn("[新增,基线外]", accepted.stdout)
        # Adding a second problem must show both tiers in the same view.
        repo.write(TERMINAL_DOC, f"实现见 `{GHOST_REF}` 与 `{OTHER_GHOST_REF}`。\n")
        repo.track()
        mixed = repo.run("--baseline", "baseline.json", "--show")
        self.assertEqual(mixed.returncode, 1, mixed.stdout + mixed.stderr)
        self.assertIn("[新增,基线外]", mixed.stdout)
        self.assertIn("[基线已接受]", mixed.stdout)


class RatchetTests(unittest.TestCase):
    """The baseline accepts what is already there and nothing more."""

    def test_one_accepted_problem_does_not_hide_the_next_one(self) -> None:
        repo = Repository(self)
        repo.write(TERMINAL_DOC, f"实现见 `{GHOST_REF}`。\n")
        repo.track()
        written = repo.run("--write-baseline", "baseline.json", "--accept-new-terminal")
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
        written = repo.run("--write-baseline", "baseline.json", "--accept-new-terminal")
        self.assertEqual(written.returncode, 0, written.stdout + written.stderr)
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


class WriteBaselineGuardTests(unittest.TestCase):
    """`--write-baseline` cannot silently accept a terminal problem.

    `terminal` is the "already reviewed" allow-list. Writing a problem nobody read
    into it turns the gate off with a single command, so a terminal key that is not
    in the baseline needs `--accept-new-terminal`. Shrinking the list (a registered
    problem is gone) is a tightening, not a widening, and needs no switch.
    """

    def _accepted_one(self) -> Repository:
        repo = Repository(self)
        repo.write(TERMINAL_DOC, f"实现见 `{GHOST_REF}`。\n")
        repo.track()
        written = repo.run("--write-baseline", "baseline.json", "--accept-new-terminal")
        self.assertEqual(written.returncode, 0, written.stdout + written.stderr)
        return repo

    def test_a_new_terminal_problem_is_refused_and_the_file_is_unchanged(self) -> None:
        repo = self._accepted_one()
        before = (repo.root / "baseline.json").read_bytes()
        repo.write(TERMINAL_DOC, f"实现见 `{GHOST_REF}` 与 `{OTHER_GHOST_REF}`。\n")
        repo.track()
        refused = repo.run("--baseline", "baseline.json", "--write-baseline", "baseline.json")
        self.assertEqual(refused.returncode, 1, refused.stdout + refused.stderr)
        self.assertIn("REFUSED", refused.stderr)
        self.assertIn("--accept-new-terminal", refused.stderr)
        # The refused key is listed by name, not hidden behind a count.
        self.assertIn(OTHER_GHOST_REF, refused.stdout)
        self.assertIn("[新增,基线外]", refused.stdout)
        # Byte-for-byte unchanged: a refusal must not leave a half-written baseline.
        self.assertEqual((repo.root / "baseline.json").read_bytes(), before)

    def test_the_flag_accepts_and_reports_how_many(self) -> None:
        repo = self._accepted_one()
        repo.write(TERMINAL_DOC, f"实现见 `{GHOST_REF}` 与 `{OTHER_GHOST_REF}`。\n")
        repo.track()
        written = repo.run(
            "--baseline",
            "baseline.json",
            "--write-baseline",
            "baseline.json",
            "--accept-new-terminal",
        )
        self.assertEqual(written.returncode, 0, written.stdout + written.stderr)
        self.assertIn("ACCEPTED new-terminal=1", written.stdout)
        self.assertIn("已接受 1 条新终态问题", written.stdout)
        payload = json.loads((repo.root / "baseline.json").read_text(encoding="utf-8"))
        self.assertEqual(
            payload["terminal"],
            sorted([f"{TERMINAL_DOC}::{GHOST_REF}", f"{TERMINAL_DOC}::{OTHER_GHOST_REF}"]),
            payload,
        )
        accepted = repo.run("--baseline", "baseline.json")
        self.assertEqual(accepted.returncode, 0, accepted.stdout + accepted.stderr)
        self.assertIn("new=0 accepted=2", accepted.stdout)

    def test_shrinking_an_accepted_problem_needs_no_flag(self) -> None:
        repo = self._accepted_one()
        repo.write(TERMINAL_DOC, f"实现见 `{GHOST_REF}` 与 `{OTHER_GHOST_REF}`。\n")
        repo.track()
        self.assertEqual(
            repo.run(
                "--baseline",
                "baseline.json",
                "--write-baseline",
                "baseline.json",
                "--accept-new-terminal",
            ).returncode,
            0,
        )
        # One of the two accepted problems is gone: rewriting is a tightening.
        repo.write(TERMINAL_DOC, f"实现见 `{GHOST_REF}`。\n")
        repo.track()
        shrunk = repo.run("--baseline", "baseline.json", "--write-baseline", "baseline.json")
        self.assertEqual(shrunk.returncode, 0, shrunk.stdout + shrunk.stderr)
        self.assertNotIn("ACCEPTED", shrunk.stdout)
        payload = json.loads((repo.root / "baseline.json").read_text(encoding="utf-8"))
        self.assertEqual(payload["terminal"], [f"{TERMINAL_DOC}::{GHOST_REF}"], payload)

    def test_a_clean_tree_needs_no_flag(self) -> None:
        repo = Repository(self)
        repo.write(TERMINAL_DOC, f"实现见 `{REAL_SOURCE}:3`。\n")
        repo.track()
        written = repo.run("--write-baseline", "baseline.json")
        self.assertEqual(written.returncode, 0, written.stdout + written.stderr)
        self.assertIn("terminal=0", written.stdout)
        self.assertNotIn("ACCEPTED", written.stdout)

    def test_without_a_baseline_reference_every_terminal_problem_is_new(self) -> None:
        # No `--baseline`: there is nothing to have accepted before, so even the
        # first terminal problem must be accepted explicitly. Say so in the error.
        repo = Repository(self)
        repo.write(TERMINAL_DOC, f"实现见 `{GHOST_REF}`。\n")
        repo.track()
        refused = repo.run("--write-baseline", "baseline.json")
        self.assertEqual(refused.returncode, 1, refused.stdout + refused.stderr)
        self.assertIn("REFUSED", refused.stderr)
        self.assertFalse((repo.root / "baseline.json").exists())

    def test_the_flag_without_write_baseline_is_an_environment_error(self) -> None:
        repo = Repository(self)
        repo.write(TERMINAL_DOC, f"实现见 `{REAL_SOURCE}:3`。\n")
        repo.track()
        result = repo.run("--accept-new-terminal")
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("--accept-new-terminal 需要与 --write-baseline 一起使用", result.stderr)


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
        # The committed records key set matches the checkout: no drift is reported
        # either way, so the delta is a real measurement rather than always zero.
        self.assertIn("RECORDS new=+0 stale=+0", result.stdout)
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
        # The debt counter is a count, not a threshold: paying the debt down (365
        # bare code refs today) must never turn this red, so there is no floor on
        # the number itself. It is instead bounded from below by an independent
        # scan of the corpus - one that does not reuse the gate's regex or scope
        # rules - so a parser regression that silently stops matching shows up as
        # `unasserted_code < lower_bound` rather than as agreement on zero.
        lower_bound = independent_bare_code_refs()
        self.assertGreater(lower_bound, 0, result.stdout)
        self.assertGreaterEqual(unasserted_code, lower_bound, result.stdout)

    def test_the_committed_baseline_keeps_terminal_documents_fully_gated(self) -> None:
        payload = json.loads(BASELINE.read_text(encoding="utf-8"))
        self.assertEqual(payload["schema"], 2)
        # An empty `terminal` list is what makes this a full gate instead of a
        # ratchet that already tolerates drift.
        self.assertEqual(payload["terminal"], [], payload)
        # `records` is a set of problem keys, not counts (#1063): a count is a
        # number that goes stale on its own, a key stays comparable. Each key
        # carries the document, the target with its line number, and the verdict.
        self.assertIsInstance(payload["records"], list, payload)
        self.assertTrue(payload["records"], payload)
        for key in payload["records"]:
            with self.subTest(key=key):
                parts = CHECK.record_key_parts(key)
                self.assertIsNotNone(parts, key)
                assert parts is not None
                doc, ref, verdict = parts
                self.assertRegex(doc, r"^docs/")
                self.assertRegex(ref, r":\d+$")
                self.assertIn(verdict, CHECK.RECORD_VERDICTS)
        # Duplicates are deliberate: a record may cite the same target on several
        # lines, and the document line is not part of the key. The delta counts
        # multiplicity, so removing one occurrence reports `stale=+1` instead of
        # being merged away. The list is sorted to keep the diff stable.
        self.assertEqual(payload["records"], sorted(payload["records"]))

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
            "--fix",
            "--show-ok",
            "--accept-new-terminal",
        ):
            with self.subTest(required=required):
                self.assertIn(required, section)
        self.assertNotIn("未接入 CI 的本地报告工具", document)


if __name__ == "__main__":
    unittest.main()
