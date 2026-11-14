"""The review-PR boundary gate must react to the incident class, and only to it.

CI never reacted to the two author-discipline commands in step 4 of
`docs/operations/agent-delivery-sop.md`. A review PR once carried implementation
that was byte-identical to the reviewed head and merged under a `review:` title,
while the reviewed code landed as a different revision (#738, squash
`f88279dc`). `scripts/dev-env/check-review-pr-boundary.py` is the machine gate.

These tests pin three things:

* the boundary is exactly `docs/reviews/` (measured on 33 merged review PRs; one
  added `docs/validation/` exception would be a silent relaxation, so widening
  it has to change a test rather than only a constant);
* the three signals are independent, so renaming the title or the branch alone
  does not un-gate the check;
* the gate is wired into the one lane that runs for every scope, and that lane
  is required by the protected merge gate.

Standard library only: this module runs in the dependency-free routing lane.
"""

from __future__ import annotations

import ast
import importlib.util
import json
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/dev-env/check-review-pr-boundary.py"
CI = (ROOT / ".github/workflows/ci.yml").read_text()
VALIDATION_DOC = (ROOT / "docs/validation/ci-validation.md").read_text()
SOP_STEP_4 = (
    (ROOT / "docs/operations/agent-delivery-sop.md")
    .read_text()
    .split("4. **按风险审查与验证。**", 1)[1]
    .split("5. **接收并收尾。**", 1)[0]
)

SPEC = importlib.util.spec_from_file_location("review_pr_boundary", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
GUARD = importlib.util.module_from_spec(SPEC)
# dataclasses resolves annotations through sys.modules[cls.__module__], so the
# loaded module has to be registered before it is executed.
sys.modules[SPEC.name] = GUARD
SPEC.loader.exec_module(GUARD)

REPORT = "docs/reviews/2026-11-14-version-ports.md"
BACKEND = "backend/src/speechnexus_backend/workbench/application/version_ports.py"
INCIDENT_PATHS = [
    REPORT,
    BACKEND,
    "backend/src/speechnexus_backend/workbench/infrastructure/postgres_versions.py",
    "tests/unit/backend/test_workbench_postgres_cleanup.py",
]


def verdict_for(
    title: str = "", head_ref: str = "", paths: list[str] | None = None, unavailable: str = ""
) -> Any:
    signals = GUARD.detect_signals(title, head_ref, paths or [])
    return GUARD.judge(signals, paths, unavailable)


class SignalTests(unittest.TestCase):
    def test_title_prefix_is_a_signal(self) -> None:
        for title in ("review: 非作者审查", "review：全角冒号", "  REVIEW: 大小写 "):
            with self.subTest(title=title):
                verdict = verdict_for(title=title, paths=[REPORT])
                self.assertTrue(verdict.applicable)
                self.assertIn("title prefix", verdict.signals)
                self.assertTrue(verdict.ok)

    def test_both_historical_branch_spellings_are_signals(self) -> None:
        for head_ref in ("review/722-carry-impl", "docs/review-709-terminal-exit"):
            with self.subTest(head_ref=head_ref):
                verdict = verdict_for(head_ref=head_ref, paths=[REPORT])
                self.assertTrue(verdict.applicable)
                self.assertIn("branch prefix", verdict.signals)
                self.assertTrue(verdict.ok)

    def test_structure_is_a_third_independent_signal(self) -> None:
        # Title and branch were renamed; the mixed docs/reviews + source change
        # set still identifies the pull request as a review artifact carrier.
        verdict = verdict_for(title="chore: 整理文档", head_ref="chore/727", paths=INCIDENT_PATHS)
        self.assertTrue(verdict.applicable)
        self.assertEqual(verdict.signals, ("docs/reviews mixed with other paths",))
        self.assertFalse(verdict.ok)

    def test_signals_are_not_interchangeable_substitutes_for_each_other(self) -> None:
        signals = GUARD.Signals(title=True, branch=False, structure=False)
        self.assertTrue(signals.declared())
        self.assertTrue(signals.any())
        self.assertFalse(GUARD.Signals(title=False, branch=False, structure=False).any())


class BoundaryTests(unittest.TestCase):
    def test_boundary_is_exactly_the_review_directory(self) -> None:
        # The measurement: 32 of 33 merged review PRs touched one report file and
        # nothing else; the only exception is the incident. No merged review PR
        # touched docs/validation/, so this constant is pinned rather than relaxed.
        self.assertEqual(GUARD.REVIEW_ROOT, "docs/reviews/")
        self.assertTrue(GUARD.is_review_path(REPORT))
        for path in (
            "docs/validation/ci-validation.md",
            "docs/reviews",
            "docs/reviews.md",
            "backend/src/speechnexus_backend/workbench/application/version_ports.py",
        ):
            with self.subTest(path=path):
                self.assertFalse(GUARD.is_review_path(path))

    def test_the_measured_incident_shape_is_red(self) -> None:
        verdict = verdict_for(
            title="review: COMMIT 分类修复的非作者审查",
            head_ref="review/721-commit-classification",
            paths=INCIDENT_PATHS,
        )
        self.assertTrue(verdict.applicable)
        self.assertFalse(verdict.ok)
        self.assertEqual(verdict.reports, (REPORT,))
        self.assertEqual(verdict.violations, tuple(INCIDENT_PATHS[1:]))
        self.assertEqual(verdict.reason, "Review PR changes 3 path(s) outside docs/reviews/.")

    def test_a_compliant_review_passes(self) -> None:
        verdict = verdict_for(title="review: 队列重试审查", head_ref="review/723", paths=[REPORT])
        self.assertTrue(verdict.applicable)
        self.assertTrue(verdict.ok)
        self.assertEqual(verdict.violations, ())

    def test_a_review_without_a_report_is_red(self) -> None:
        verdict = verdict_for(
            title="review: 只改了验证文档",
            head_ref="review/724",
            paths=["docs/validation/ci-validation.md"],
        )
        self.assertFalse(verdict.ok)
        self.assertEqual(verdict.reports, ())
        self.assertEqual(verdict.violations, ("docs/validation/ci-validation.md",))

    def test_a_declared_review_with_an_empty_diff_is_red(self) -> None:
        # The boundary is vacuously satisfied by an empty path set; an empty
        # review PR carries no report, so the implementation additionally
        # requires at least one docs/reviews/ path.
        verdict = verdict_for(title="review: 空", head_ref="review/725", paths=[])
        self.assertTrue(verdict.applicable)
        self.assertFalse(verdict.ok)
        self.assertEqual(verdict.violations, ())
        self.assertEqual(
            verdict.reason, "Review PR reports no file under docs/reviews/ (0 path(s) outside)."
        )

    def test_ordinary_implementation_and_record_pull_requests_are_not_applicable(self) -> None:
        cases = {
            "implementation": (
                "fix: version ports 竞态",
                "fix/726-normal",
                [BACKEND, "README.md"],
            ),
            "validation record": (
                "docs: 验证记录补录",
                "docs/726-record",
                ["docs/validation/ci-validation.md", "docs/plans/notes.md"],
            ),
            "empty change": ("docs: 空", "docs/727", []),
        }
        for label, (title, head_ref, paths) in cases.items():
            with self.subTest(label=label):
                verdict = verdict_for(title=title, head_ref=head_ref, paths=paths)
                self.assertFalse(verdict.applicable)
                self.assertTrue(verdict.ok)
                self.assertEqual(verdict.violations, ())

    def test_an_unreadable_diff_fails_closed_only_for_declared_reviews(self) -> None:
        declared = verdict_for(
            title="review: x", head_ref="review/x", paths=None, unavailable="boom"
        )
        self.assertTrue(declared.applicable)
        self.assertFalse(declared.ok)
        self.assertIn("boom", declared.reason)

        undeclared = verdict_for(title="fix: x", head_ref="fix/x", paths=None, unavailable="boom")
        self.assertFalse(undeclared.applicable)
        self.assertTrue(undeclared.ok)
        self.assertIn("boom", undeclared.reason)


class BaseFetchTests(unittest.TestCase):
    """A network blip must read as a fetch problem, and cost one retry, not two."""

    def setUp(self) -> None:
        self.patches = mock.patch.object(GUARD, "FETCH_BACKOFF_SECONDS", 0.0)
        self.patches.start()
        self.addCleanup(self.patches.stop)

    def test_a_failed_base_fetch_is_retried_once(self) -> None:
        calls: list[tuple[str, ...]] = []

        def fake_git(cwd: Path, *args: str) -> bytes:
            calls.append(args)
            if len(calls) == 1:
                raise subprocess.CalledProcessError(
                    128, ["git", *args], stderr=b"fatal: could not resolve host\n"
                )
            return b""

        with mock.patch.object(GUARD, "_git", fake_git):
            GUARD._fetch_base("a" * 40, Path("/tmp"))
        self.assertEqual(len(calls), 2)

    def test_a_persistent_fetch_failure_names_the_fetch_and_the_exit_code(self) -> None:
        def fake_git(cwd: Path, *args: str) -> bytes:
            raise subprocess.CalledProcessError(
                128, ["git", *args], stderr=b"fatal: could not resolve host: github.com\n"
            )

        with (
            mock.patch.object(GUARD, "_git", fake_git),
            self.assertRaises(GUARD.DiffUnavailableError) as caught,
        ):
            GUARD._fetch_base("a" * 40, Path("/tmp"))
        message = str(caught.exception)
        self.assertIn(f"fetching base {'a' * 40} from origin failed after 2 attempts", message)
        self.assertIn("exit 128", message)
        self.assertIn("could not resolve host", message)

    def test_a_diff_failure_is_named_separately_from_a_fetch_failure(self) -> None:
        def fake_git(cwd: Path, *args: str) -> bytes:
            if args[0] == "diff":
                raise subprocess.CalledProcessError(
                    128, ["git", *args], stderr=b"fatal: bad object HEAD\n"
                )
            return b""

        with (
            mock.patch.object(GUARD, "_git", fake_git),
            self.assertRaises(GUARD.DiffUnavailableError) as caught,
        ):
            GUARD.changed_paths("a" * 40, Path("/tmp"))
        message = str(caught.exception)
        self.assertIn(f"computing git diff {'a' * 40}..HEAD failed", message)
        self.assertIn("bad object HEAD", message)


class GitFixtureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.git("init", "-q", "-b", "main")
        self.write("README.md", "# Initial\n")
        self.write(BACKEND, "v = 1\n")
        self.base = self.commit()

    def git(self, *args: str) -> str:
        return subprocess.check_output(
            [
                "git",
                "-c",
                "core.hooksPath=/dev/null",
                "-c",
                "commit.gpgsign=false",
                "-c",
                "user.name=CI",
                "-c",
                "user.email=ci@example.invalid",
                *args,
            ],
            cwd=self.root,
            text=True,
            stderr=subprocess.PIPE,
        ).strip()

    def write(self, name: str, content: str) -> None:
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)

    def commit(self) -> str:
        self.git("add", ".")
        self.git("commit", "-qm", "Synthetic review-pr fixture")
        return self.git("rev-parse", "HEAD")

    def run_guard(self, *extra: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(SCRIPT), "--event", "pull_request", "--base", self.base, *extra],
            cwd=self.root,
            text=True,
            capture_output=True,
            check=False,
        )

    def test_a_real_violation_diff_is_red_and_names_every_offending_path(self) -> None:
        self.write(REPORT, "# Review\n")
        self.write(BACKEND, "v = 2\n")
        self.commit()
        result = self.run_guard("--title", "review: version ports", "--head-ref", "review/722")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(
            json.loads(result.stdout.splitlines()[0])["violations"],
            ["backend/src/speechnexus_backend/workbench/application/version_ports.py"],
        )
        self.assertIn(
            "::error title=Path outside docs/reviews/::"
            "backend/src/speechnexus_backend/workbench/application/version_ports.py",
            result.stderr,
        )
        # The remediation must be executable, not advice: name the reviewed head
        # form and forbid checking that head out on this branch.
        self.assertIn("git show <reviewed-head>:<path>", result.stderr)
        self.assertIn("/tmp/<name>", result.stderr)
        self.assertIn("git checkout <reviewed-head>", result.stderr)
        self.assertIn("docs/operations/agent-delivery-sop.md", result.stderr)

    def test_a_real_compliant_review_diff_passes(self) -> None:
        self.write(REPORT, "# Review\n")
        self.commit()
        result = self.run_guard("--title", "review: version ports", "--head-ref", "review/722")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(json.loads(result.stdout.splitlines()[0])["ok"])

    def test_a_deleted_source_path_still_counts(self) -> None:
        # Dropping implementation from the review branch is the second half of the
        # incident: the landing commit then differs from the reviewed head.
        self.write(REPORT, "# Review\n")
        (self.root / BACKEND).unlink()
        self.commit()
        result = self.run_guard("--title", "review: cleanup", "--head-ref", "review/723")
        self.assertEqual(result.returncode, 1)
        self.assertIn("version_ports.py", result.stderr)

    def test_a_source_renamed_into_the_review_directory_is_visible(self) -> None:
        self.write("docs/reviews/.keep", "")
        self.commit()
        self.git("mv", BACKEND, "docs/reviews/version_ports.py")
        self.commit()
        result = self.run_guard("--title", "review: rename", "--head-ref", "review/724")
        self.assertEqual(result.returncode, 1)
        self.assertIn("version_ports.py", result.stderr)

    def test_a_renamed_title_and_branch_are_still_caught_by_the_structure(self) -> None:
        # End to end for signal 3: nothing about the PR metadata says "review",
        # only the mixed docs/reviews + source change set does.
        self.write(REPORT, "# Review\n")
        self.write("backend/x.py", "x = 1\n")
        self.commit()
        result = self.run_guard("--title", "chore: 整理文档", "--head-ref", "chore/726")
        self.assertEqual(result.returncode, 1)
        payload = json.loads(result.stdout.splitlines()[0])
        self.assertEqual(payload["signals"], ["docs/reviews mixed with other paths"])
        self.assertEqual(payload["violations"], ["backend/x.py"])

    def test_a_non_pull_request_event_is_skipped_without_a_diff(self) -> None:
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--event", "push"],
            cwd=self.root,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout.splitlines()[0])
        self.assertEqual((payload["applicable"], payload["ok"]), (False, True))

    def test_the_step_summary_records_signal_boundary_and_offenders(self) -> None:
        self.write(REPORT, "# Review\n")
        self.write("backend/x.py", "x = 1\n")
        self.commit()
        summary = self.root / "summary.md"
        result = self.run_guard(
            "--title", "review: x", "--head-ref", "review/725", "--summary", str(summary)
        )
        self.assertEqual(result.returncode, 1)
        rendered = summary.read_text()
        self.assertIn("### Review PR boundary: fail", rendered)
        self.assertIn("- Outside `docs/reviews/`: `backend/x.py`", rendered)

    def test_script_is_compatible_with_runner_python_312(self) -> None:
        ast.parse(SCRIPT.read_text(), feature_version=(3, 12))


class WiringTests(unittest.TestCase):
    def job_block(self, job: str) -> str:
        after = CI.split(f"\n  {job}:\n", 1)[1]
        return re.split(r"\n  [a-z_]+:\n", after, maxsplit=1)[0]

    def changes_job(self) -> str:
        return CI.split("\n  changes:\n", 1)[1].split("\n  docs_format:\n", 1)[0]

    def gate_job(self) -> str:
        return CI.split("\n  python:\n", 1)[1]

    def test_the_guard_lives_in_the_job_that_runs_for_every_scope(self) -> None:
        changes = self.changes_job()
        self.assertIn("python3 scripts/dev-env/check-review-pr-boundary.py", changes)
        # A review PR is a `docs`-scope PR; a job- or step-level `if:` would skip
        # the guard exactly where it is needed (#738).
        self.assertNotIn("\n    if:", changes)
        step = changes.split("- name: Reject review pull requests that carry non-review paths", 1)[
            1
        ]
        self.assertNotIn("\n        if:", step)

    def test_the_guard_receives_the_pull_request_identity_and_the_exact_base(self) -> None:
        changes = self.changes_job()
        for wiring in (
            "EVENT_NAME: ${{ github.event_name }}",
            "BASE_SHA: ${{ github.event.pull_request.base.sha }}",
            "PR_TITLE: ${{ github.event.pull_request.title }}",
            "PR_HEAD_REF: ${{ github.event.pull_request.head.ref }}",
        ):
            with self.subTest(wiring=wiring):
                self.assertIn(wiring, changes)
        command = (
            "          python3 scripts/dev-env/check-review-pr-boundary.py \\\n"
            '            --event="$EVENT_NAME" --base="$BASE_SHA" \\\n'
            '            --title="$PR_TITLE" --head-ref="$PR_HEAD_REF" \\\n'
            '            --summary="$GITHUB_STEP_SUMMARY"'
        )
        self.assertIn(command, changes)

    def test_the_scope_route_is_decided_before_the_guard_turns_the_job_red(self) -> None:
        changes = self.changes_job()
        self.assertLess(
            changes.index("python3 scripts/dev-env/ci-routing.py"),
            changes.index("python3 scripts/dev-env/check-review-pr-boundary.py"),
        )

    def test_the_protected_gate_requires_this_job(self) -> None:
        gate = self.gate_job()
        needs = gate.split("    needs:", 1)[1].split("]", 1)[0]
        self.assertIn("\n        changes,\n", needs)
        self.assertIn("ROUTING_RESULT: ${{ needs.changes.result }}", gate)
        self.assertIn('if [[ "$ROUTING_RESULT" != success ]]', gate)

    def test_the_check_has_exactly_one_invocation(self) -> None:
        # A second copy could drift from the routed one; the lane and the script
        # are the only tenants.
        self.assertEqual(CI.count("python3 scripts/dev-env/check-review-pr-boundary.py"), 1)

    def test_the_validation_record_documents_the_gate(self) -> None:
        self.assertIn("check-review-pr-boundary.py", VALIDATION_DOC)
        self.assertIn("docs/reviews/", VALIDATION_DOC)
        # The gate executes a process contract, so the SOP has to name the same
        # boundary: a report written to docs/validation/ is red here and cannot
        # look compliant in step 4 (#738 review, MF-2).
        self.assertIn("`docs/reviews/` 下的报告文件", SOP_STEP_4)

    def test_both_unfiltered_lanes_are_acknowledged(self) -> None:
        # `delivery_tracking` has no job-level `if:` either; claiming `changes`
        # is the only unfiltered lane is a fact error the docs must not repeat
        # (#738 review, MF-1).
        for job in ("changes", "delivery_tracking"):
            with self.subTest(job=job):
                self.assertNotIn("\n    if:", self.job_block(job))
        self.assertNotIn("唯一没有任何 `if:`", VALIDATION_DOC)
        self.assertNotIn("the one lane", CI)


class SelfTestTests(unittest.TestCase):
    """Remove the check or widen the boundary and these tests must notice."""

    def test_the_boundary_is_the_only_thing_that_makes_the_incident_red(self) -> None:
        # Counterexample run in the delivery session: widening the boundary to
        # every path turns the same change set green, so the boundary is load
        # bearing rather than decorative.
        with mock.patch.object(GUARD, "REVIEW_ROOT", ""):
            relaxed = verdict_for(title="review: x", head_ref="review/722", paths=INCIDENT_PATHS)
            self.assertTrue(relaxed.ok)
            self.assertEqual(relaxed.violations, ())
        self.assertFalse(
            verdict_for(title="review: x", head_ref="review/722", paths=INCIDENT_PATHS).ok
        )


if __name__ == "__main__":
    unittest.main()
