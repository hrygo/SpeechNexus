"""The guards that let `reference_acceptance` claim it catches a corrupted entry.

Standard library only: this module runs in the dependency-free routing lane.
Each case is a shape the guard must accept (a correct, equivalent writing) or
reject (a silent false pass), so the gate's teeth do not depend on the entry
happening to look a certain way today.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/dev-env/ci-entry-guard.py"
ENTRY = ROOT / "scripts/dev-env/reference-acceptance.py"
# The routing lane runs this file on the runner's bare Python, so the workflow is
# read as text instead of pulling PyYAML into the dependency-free lane.
CI_TEXT = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
JOB_TEXT = CI_TEXT.split("  reference_acceptance:\n", 1)[1]

SPEC = importlib.util.spec_from_file_location("ci_entry_guard", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
GUARD = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = GUARD
SPEC.loader.exec_module(GUARD)

GATEWAY_DEFERRAL = (
    "gateway 未 healthy；job-dispatch-worker 的 network_mode: service:gateway 需要它先运行"
)
REWORDED_DEFERRAL = (
    "gateway 尚未 healthy；job-dispatch-worker 通过 network_mode: "
    "service:gateway 复用网络，需先运行"
)
NEW_LANE_DEFERRAL = "缺少 gateway lane 配置: x.json"

SAMPLE = """
def step(ctx, other):
    applied = ctx.mutate(("docker", "compose", "up", "-d"))
    if not applied.ok:
        raise RuntimeError("failed")
    return "ok"
"""


def audit(source: str, *, search_root: Path | None = None) -> tuple[list[str], str]:
    with tempfile.TemporaryDirectory() as directory:
        entry = Path(directory) / "entry.py"
        entry.write_text(textwrap.dedent(source), encoding="utf-8")
        problems, summary = GUARD.audit_sites(entry, search_root or ROOT, require_sites=False)
        return list(problems), str(summary)


class AcceptedShapesTests(unittest.TestCase):
    """Every equivalent, correct judgement must stay green (no false red)."""

    def assert_green(self, source: str, *, search_root: Path | None = None) -> None:
        problems, summary = audit(source, search_root=search_root)
        self.assertEqual(problems, [], f"{summary}: {problems}")

    def test_ok_read_in_a_condition_is_a_judgement(self) -> None:
        self.assert_green(
            """
            def step(ctx):
                applied = ctx.mutate(("docker", "compose", "up", "-d"))
                if not applied.ok:
                    raise RuntimeError("failed")
            """
        )

    def test_exit_code_truthiness_is_a_judgement(self) -> None:
        self.assert_green(
            """
            def step(ctx):
                applied = ctx.mutate(("docker", "compose", "up", "-d"))
                if applied.code:
                    raise RuntimeError("failed")
            """
        )

    def test_exit_code_bound_to_a_local_then_compared_is_a_judgement(self) -> None:
        self.assert_green(
            """
            def step(ctx):
                applied = ctx.mutate(("docker", "compose", "up", "-d"))
                returncode = applied.code
                if returncode != 0:
                    raise RuntimeError("failed")
            """
        )

    def test_ok_bound_to_a_local_then_asserted_is_a_judgement(self) -> None:
        self.assert_green(
            """
            def step(ctx):
                applied = ctx.mutate(("docker", "compose", "up", "-d"))
                healthy = applied.ok
                assert healthy, "compose up failed"
            """
        )

    def test_keyword_argument_reaches_a_judging_helper(self) -> None:
        self.assert_green(
            """
            def judge(*, result):
                return not result.ok

            def step(ctx):
                applied = ctx.mutate(("docker", "compose", "up", "-d"))
                if not judge(result=applied):
                    raise RuntimeError("failed")
            """
        )

    def test_positional_argument_reaches_a_judging_helper(self) -> None:
        self.assert_green(
            """
            def judge(result, label):
                if not result.ok:
                    raise RuntimeError(label)

            def step(ctx):
                applied = ctx.mutate(("docker", "compose", "up", "-d"))
                judge(applied, "compose up")
            """
        )

    def test_judgement_moved_to_another_module_stays_resolved(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            (workspace / "checks.py").write_text(
                "def judge(result):\n    if not result.ok:\n        raise RuntimeError('failed')\n",
                encoding="utf-8",
            )
            self.assert_green(
                """
                def step(ctx):
                    applied = ctx.mutate(("docker", "compose", "up", "-d"))
                    judge(applied)
                """,
                search_root=workspace,
            )

    def test_mutate_inside_a_judging_helper_guarded_elsewhere_is_counted_once(self) -> None:
        self.assert_green(
            """
            def surface(result):
                print(result.output())

            def step(ctx):
                applied = ctx.mutate(("docker", "compose", "up", "-d"))
                if not applied.ok:
                    raise RuntimeError("failed")
                surface(applied)
            """
        )

    def test_uv_run_inside_read_stays_green(self) -> None:
        self.assert_green(
            """
            def step(ctx):
                rendered = ctx.read(("uv", "run", "--locked", "python", "provision-job-roles.py"))
                if not rendered.ok:
                    raise RuntimeError("failed")
            """
        )


class RejectedShapesTests(unittest.TestCase):
    """Every silent false pass must be red."""

    def assert_red(self, source: str, fragment: str) -> None:
        problems, _summary = audit(source)
        self.assertTrue(problems, "the guard accepted a false pass")
        self.assertTrue(
            any(fragment in problem for problem in problems),
            f"expected {fragment!r} in {problems}",
        )

    def test_discarded_result_is_red(self) -> None:
        self.assert_red(
            """
            def step(ctx):
                ctx.mutate(("docker", "compose", "up", "-d"))
            """,
            "未被判定",
        )

    def test_annotated_assignment_is_a_site_and_can_be_red(self) -> None:
        self.assert_red(
            """
            def step(ctx):
                applied: object = ctx.mutate(("docker", "compose", "up", "-d"))
                return "ok"
            """,
            "annotated assignment",
        )

    def test_judgement_dropped_but_report_helper_kept_is_red(self) -> None:
        # The B1 regression: the exit-code guard is deleted while a helper still
        # prints the output, so `.ok`/`.code` are never judged.
        self.assert_red(
            """
            def surface(result):
                print(result.output())

            def step(ctx):
                applied = ctx.mutate(("docker", "compose", "up", "-d"))
                surface(applied)
            """,
            "未被判定",
        )

    def test_helper_reading_another_parameters_ok_is_not_a_judgement(self) -> None:
        self.assert_red(
            """
            def annotate(label, other):
                if not other.ok:
                    raise RuntimeError("other failed")
                return label

            def step(ctx):
                applied = ctx.mutate(("docker", "compose", "up", "-d"))
                other = ctx.read(("docker", "version"))
                annotate(applied, other)
            """,
            "未被判定",
        )

    def test_compose_run_inside_read_is_red(self) -> None:
        self.assert_red(
            """
            def step(ctx):
                ctx.read(("docker", "compose", "run", "--rm", "migrate", "alembic", "upgrade"))
            """,
            "携带写命令",
        )

    def test_shell_command_inside_read_is_red(self) -> None:
        self.assert_red(
            """
            def step(ctx):
                ctx.read(("sh", "-c", "touch /tmp/proof"))
            """,
            "携带写命令",
        )

    def test_registry_needs_a_reason(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            entry = Path(directory) / "entry.py"
            entry.write_text(
                textwrap.dedent(
                    """
                    def step(ctx):
                        return ctx.mutate(("docker", "compose", "up", "-d"))
                    """
                ),
                encoding="utf-8",
            )
            key = "step:ctx.mutate(('docker', 'compose', 'up', '-d'))"
            with mock.patch.dict(GUARD.REGISTERED_UNAUDITED, {key: "   "}):
                problems, _summary = GUARD.audit_sites(entry, ROOT, require_sites=False)
            self.assertTrue(any("理由为空" in problem for problem in problems), problems)

    def test_registered_site_with_a_reason_is_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            entry = Path(directory) / "entry.py"
            entry.write_text(
                textwrap.dedent(
                    """
                    def step(ctx):
                        return ctx.mutate(("docker", "compose", "up", "-d"))
                    """
                ),
                encoding="utf-8",
            )
            key = "step:ctx.mutate(('docker', 'compose', 'up', '-d'))"
            with mock.patch.dict(GUARD.REGISTERED_UNAUDITED, {key: "由调用方判定，见 tests"}):
                problems, summary = GUARD.audit_sites(entry, ROOT, require_sites=False)
            self.assertEqual(problems, [], summary)
            self.assertIn("1 registered", summary)


class ProbeTests(unittest.TestCase):
    def test_the_real_entry_passes_every_probe(self) -> None:
        problems, summary = GUARD.probe(ENTRY)
        self.assertEqual(problems, [], summary)

    def test_emptied_apply_judgement_is_red(self) -> None:
        text = ENTRY.read_text(encoding="utf-8")
        start = text.index("def migration_verify(")
        end = text.index("def psql_argv(")
        emptied = (
            "def migration_verify(head: str, database_version: str) -> str | None:\n"
            '    """Return a named failure when the database is not exactly at ``head``."""\n'
            "    return None\n\n\n"
        )
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "reference-acceptance.py"
            target.write_text(text[:start] + emptied + text[end:], encoding="utf-8")
            problems, _summary = GUARD.probe(target)
        self.assertTrue(any("migration_verify" in problem for problem in problems), problems)


class LogContractTests(unittest.TestCase):
    LOG = textwrap.dedent(
        f"""\
        steps=7 order=tools, migrate, roles, buckets, stack, gate, health mode=dry-run gate=compose
        [1/7] tools — 工具
              结论: PLAN — 计划
        [2/7] migrate — 迁移
              结论: PLAN — 计划
        [3/7] roles — 角色
              结论: PLAN — 计划
        [4/7] buckets — 桶
              结论: PLAN — 计划
        [5/7] stack — 栈
              结论: PLAN — 计划
        [6/7] gate — 闸门
              待输入: {GATEWAY_DEFERRAL}
              结论: PLAN — 计划
        [7/7] health — 健康
              结论: PLAN — 计划
        步骤记录: tools=PLAN migrate=PLAN roles=PLAN buckets=PLAN stack=PLAN gate=PLAN health=PLAN
        reference acceptance: DRY-RUN PASS steps=7 pending=1
        """
    )

    def check(self, text: str) -> tuple[list[str], str]:
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / "run.log"
            log.write_text(text, encoding="utf-8")
            problems, summary = GUARD.check_log(log, GUARD.EXPECTED_STEP_ORDER)
            return list(problems), str(summary)

    def test_contract_passes(self) -> None:
        problems, summary = self.check(self.LOG)
        self.assertEqual(problems, [], summary)

    def test_reworded_deferral_stays_green(self) -> None:
        problems, _summary = self.check(self.LOG.replace(GATEWAY_DEFERRAL, REWORDED_DEFERRAL))
        self.assertEqual(problems, [])

    def test_new_deferral_must_be_registered(self) -> None:
        problems, _summary = self.check(
            self.LOG.replace(
                f"      待输入: {GATEWAY_DEFERRAL}",
                f"      待输入: {NEW_LANE_DEFERRAL}\n      待输入: {GATEWAY_DEFERRAL}",
            ).replace("pending=1", "pending=2")
        )
        self.assertTrue(any("未登记的延后项" in problem for problem in problems), problems)

    def test_order_drift_is_red(self) -> None:
        problems, _summary = self.check(
            self.LOG.replace(
                "order=tools, migrate, roles, buckets, stack, gate, health",
                "order=tools, migrate, roles, buckets, gate, stack, health",
            )
        )
        self.assertTrue(any("order=" in problem for problem in problems), problems)

    def test_pending_count_must_match_the_deferral_lines(self) -> None:
        problems, _summary = self.check(self.LOG.replace("pending=1", "pending=0"))
        self.assertTrue(any("pending=0" in problem for problem in problems), problems)

    def test_a_step_claiming_pass_during_a_dry_run_is_red(self) -> None:
        problems, _summary = self.check(
            self.LOG.replace("      结论: PLAN — 计划\n[2/7]", "      结论: PASS — 计划\n[2/7]")
        )
        self.assertTrue(any("必须是 PLAN" in problem for problem in problems), problems)

    def test_entry_step_order_must_match_the_contract(self) -> None:
        problems, summary = GUARD.check_log(Path("/dev/null"), GUARD.EXPECTED_STEP_ORDER)
        self.assertTrue(problems, summary)


class WorkflowWiringTests(unittest.TestCase):
    def step_block(self, name: str) -> str:
        start = JOB_TEXT.index(f"- name: {name}")
        remainder = JOB_TEXT[start + len(name) :]
        return remainder.split("      - name:", 1)[0]

    def test_the_job_runs_all_three_guards_and_the_negative_control(self) -> None:
        for command in (
            "ci-entry-guard.py log",
            "ci-entry-guard.py sites",
            "ci-entry-guard.py probe",
        ):
            with self.subTest(command=command):
                self.assertIn(command, JOB_TEXT)
        control = self.step_block("Reject a corrupted environment instead of passing quietly")
        self.assertIn("FAIL step=tools", control)

    def test_the_job_does_not_hardcode_deploy_private_file_names(self) -> None:
        prepare = self.step_block("Prepare the acceptance environment from the committed example")
        for literal in (
            "gateway-file-asr",
            "gateway-file-tts",
            "transcription-recipe",
            "silero_vad.onnx",
        ):
            with self.subTest(literal=literal):
                self.assertNotIn(literal, prepare)
        self.assertIn("/run/speechnexus-private/", prepare)

    def test_the_entry_point_is_only_executed_never_rewritten(self) -> None:
        for marker in (
            "sed -i",
            ">> scripts/dev-env/reference-acceptance.py",
            "tee scripts/dev-env",
        ):
            with self.subTest(marker=marker):
                self.assertNotIn(marker, JOB_TEXT)
        self.assertIn("scripts/dev-env/reference-acceptance.py --dry-run", JOB_TEXT)

    def test_the_gate_requires_the_job(self) -> None:
        gate = CI_TEXT.split('    name: "Gate: required checks for this change"', 1)[1]
        needs = gate.split("    if:", 1)[0].replace("\n", " ")
        self.assertIn("reference_acceptance", needs)
        self.assertIn(
            "ACCEPTANCE_RESULT: ${{ needs.reference_acceptance.result }}",
            gate,
        )

    def test_the_guard_is_callable_from_the_job_checkout(self) -> None:
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "sites", "--entry", str(ENTRY)],
            capture_output=True,
            text=True,
            check=False,
            cwd=ROOT,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("judged", result.stdout)


class RegistryLineTests(unittest.TestCase):
    def test_the_registration_hint_points_at_a_real_line(self) -> None:
        for table in ("DECLARED_DEFERRALS", "REGISTERED_UNAUDITED"):
            line = GUARD._constant_line(SCRIPT, table)
            self.assertGreater(line, 0, table)
            source_line = SCRIPT.read_text(encoding="utf-8").splitlines()[line - 1]
            self.assertIn(table, source_line)

    def test_deferral_registry_holds_words_not_sentences(self) -> None:
        for slug, words in GUARD.DECLARED_DEFERRALS.items():
            with self.subTest(slug=slug):
                self.assertIsInstance(words, tuple)
                self.assertTrue(words)
                for word in words:
                    self.assertLess(len(word), 24, f"{word!r} 太像整句，改用稳定词")


if __name__ == "__main__":
    unittest.main()
