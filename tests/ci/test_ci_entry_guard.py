"""Exercise the structural guard for the reference-acceptance entry point.

The guard is a **structure** check: site inventory, registry anchors, probe
coverage, read-only argv, workflow wiring and the output contract. These tests pin
the behaviour it claims, including the cases the previous revision got wrong
(keyword hand-off, truthiness of a result object, deleting a registry row).
"""

from __future__ import annotations

import importlib.util
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from types import ModuleType
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/dev-env/ci-entry-guard.py"
ENTRY = ROOT / "scripts/dev-env/reference-acceptance.py"
WORKFLOW = ROOT / ".github/workflows/ci.yml"
DOC = ROOT / "docs/operations/reference-acceptance.md"


def _load_guard() -> ModuleType:
    spec = importlib.util.spec_from_file_location("ci_entry_guard_under_test", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


GUARD = _load_guard()


def audit(
    source: str, *, baseline: tuple[object, ...] = (), require_sites: bool = False
) -> list[str]:
    problems = GUARD.audit_sources(
        textwrap.dedent(source),
        label="fixture.py",
        baseline=baseline,
        require_sites=require_sites,
    )
    return [str(problem) for problem in problems]


SITE = GUARD.SiteRow("step:('docker', 'compose', 'up', '-d')", "fixture", "probe:gate_verdict-fail")


class AnchorTests(unittest.TestCase):
    """The registries and the documentation block must not drift apart."""

    def test_the_checked_in_anchor_matches_the_script(self) -> None:
        self.assertEqual(GUARD.check_anchor(), [])
        self.assertEqual(GUARD.check_counts(), [])

    def test_a_tampered_documentation_block_is_red(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            copied = Path(directory) / "reference-acceptance.md"
            text = DOC.read_text(encoding="utf-8")
            copied.write_text(
                text.replace("| `step_gate:argv` |", "| `step_gate:removed` |"), encoding="utf-8"
            )
            problems = GUARD.check_anchor(copied)
        self.assertTrue(problems, "改文档表的行必须红")
        self.assertIn("不一致", problems[0])

    def test_a_missing_anchor_is_red(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            copied = Path(directory) / "doc.md"
            copied.write_text("# 没有锚点\n", encoding="utf-8")
            problems = GUARD.check_anchor(copied)
        self.assertTrue(problems)
        self.assertIn("tables --write", problems[0])

    def test_a_shrunk_table_without_touching_the_pin_is_red(self) -> None:
        shrunk = {
            "MUTATE_SITES": 7,
            "READ_ARGUMENTS": 3,
            "WRITE_TOKENS": 7,
            "PROBE_CASES": 9,
            "DECLARED_DEFERRALS": 1,
            "SELFTEST_CASES": len(GUARD.SELFTEST_CASES),
        }
        with mock.patch.object(GUARD, "PINNED_COUNTS", shrunk):
            problems = GUARD.check_counts()
        self.assertTrue(problems)
        self.assertIn("MUTATE_SITES", problems[0])

    def test_every_registration_points_at_something_that_exists(self) -> None:
        self.assertEqual(GUARD.audit_evidence(), [])
        broken = (GUARD.SiteRow("step_x:argv", "fixture", "test:test_that_does_not_exist"),)
        with mock.patch.object(GUARD, "MUTATE_SITES", broken):
            problems = GUARD.audit_evidence()
        self.assertTrue(problems)
        self.assertIn("不存在的 tests/ci 用例", problems[0])


class SiteInventoryTests(unittest.TestCase):
    def test_the_real_entry_matches_its_registry(self) -> None:
        problems, summary = GUARD.run_sites(ENTRY)
        self.assertEqual(problems, [], summary)
        self.assertIn(f"{len(GUARD.MUTATE_SITES)} registered", summary)

    def test_a_deleted_write_command_is_red(self) -> None:
        source = textwrap.dedent(
            """
            def step(ctx):
                started = ctx.mutate(('docker', 'compose', 'up', '-d'))
                return started.ok
            """
        )
        problems = GUARD.audit_sources(source, label="fixture.py", baseline=(SITE,))
        self.assertEqual(problems, [])
        without = source.replace("ctx.mutate(('docker', 'compose', 'up', '-d'))", "1")
        problems = GUARD.audit_sources(without, label="fixture.py", baseline=(SITE,))
        self.assertTrue(problems, "删掉写命令必须红")
        self.assertIn("登记的写命令消失了", problems[0])

    def test_an_added_write_command_is_red(self) -> None:
        source = textwrap.dedent(
            """
            def step(ctx):
                started = ctx.mutate(('docker', 'compose', 'up', '-d'))
                other = ctx.mutate(('docker', 'compose', 'down'))
                return started.ok, other.ok
            """
        )
        problems = GUARD.audit_sources(source, label="fixture.py", baseline=(SITE,))
        self.assertTrue(problems)
        self.assertIn("未登记的写命令", problems[0])

    def test_the_receiver_name_does_not_hide_a_site(self) -> None:
        source = (
            "def step(ctx):\n"
            "    started = ctx.runner.mutate(('docker', 'compose', 'up', '-d'))\n"
            "    return started\n"
        )
        self.assertEqual(audit(source, baseline=(SITE,)), [])
        problems = audit(source)
        self.assertTrue(problems)
        self.assertIn("未登记的写命令", problems[0])

    def test_unrelated_read_methods_are_not_entry_calls(self) -> None:
        source = textwrap.dedent(
            """
            def step(ctx, handle):
                chunk = handle.read(1 << 20)
                response = handle.read().decode()
                return chunk, response
            """
        )
        self.assertEqual(audit(source), [])

    def test_a_discarded_result_is_red(self) -> None:
        source = "def step(ctx):\n    ctx.mutate(('docker', 'compose', 'up', '-d'))\n"
        problems = audit(source, baseline=(SITE,))
        self.assertTrue(problems)
        self.assertIn("被丢弃", problems[0])

    def test_a_bare_result_name_in_a_condition_is_red(self) -> None:
        for test in (
            "if started:",
            "if not started:",
            "if started is not None:",
            "if started is None:",
            "assert started",
        ):
            with self.subTest(test=test):
                source = (
                    "def step(ctx):\n"
                    "    started = ctx.mutate(('docker', 'compose', 'up', '-d'))\n"
                    f"    {test}\n"
                    "        return 'ok'\n"
                )
                problems = audit(source, baseline=(SITE,))
                self.assertTrue(problems, f"{test} 必须红")

    def test_reading_ok_is_still_green(self) -> None:
        source = textwrap.dedent(
            """
            def step(ctx):
                started = ctx.mutate(('docker', 'compose', 'up', '-d'))
                if not started.ok:
                    raise RuntimeError('failed')
                return started.code
            """
        )
        self.assertEqual(audit(source, baseline=(SITE,)), [])

    def test_a_hand_off_to_a_helper_is_green(self) -> None:
        source = textwrap.dedent(
            """
            def step(ctx):
                applied = ctx.mutate(('docker', 'compose', 'up', '-d'))
                judge(result=applied)
                judge(applied)
                return applied
            """
        )
        self.assertEqual(audit(source, baseline=(SITE,)), [])

    def test_read_boundary(self) -> None:
        cases = {
            "literal down": ("ctx.read(('docker', 'compose', 'down'))", True),
            "literal run": ("ctx.read(('docker', 'compose', 'run', 'api'))", True),
            "sh -c": ("ctx.read(('sh', '-c', 'touch /tmp/x'))", True),
            "python -c": ("ctx.read(('python3', '-c', 'print(1)'))", True),
            "uv run": ("ctx.read(('uv', 'run', '--locked', 'python', 'x.py'))", False),
            "uv run then compose run": (
                "ctx.read(('uv', 'run', 'docker', 'compose', 'run', '-d', 'api'))",
                True,
            ),
            "registered builder": ("ctx.read(psql_argv(ctx, 'SELECT 1'))", False),
            "unknown builder": ("ctx.read(shoot())", True),
            "variable argv": ("argv = ('docker', 'compose', 'down')\n    ctx.read(argv)", None),
            "value wrapper": ("ctx.read(('git', '-C', str(ctx.repo_root), 'status'))", False),
        }
        for name, (statement, expected_red) in cases.items():
            with self.subTest(name):
                if statement.startswith("argv ="):
                    source = f"def step(ctx):\n    {statement}\n"
                else:
                    source = f"def step(ctx):\n    {statement}\n"
                problems = audit(source)
                if expected_red is None or expected_red:
                    self.assertTrue(problems, f"{name} 必须红")
                else:
                    self.assertEqual(problems, [], f"{name} 必须绿")

    def test_an_empty_module_cannot_vacuously_pass(self) -> None:
        problems = GUARD.audit_sources(
            "def nothing(ctx):\n    return 1\n", label="fixture.py", baseline=(), require_sites=True
        )
        self.assertTrue(problems)
        self.assertIn("空转", problems[0])


class SelftestCorpusTests(unittest.TestCase):
    def test_the_corpus_passes(self) -> None:
        self.assertEqual(GUARD.selftest(), [])

    def test_weakening_the_write_tokens_breaks_the_corpus(self) -> None:
        with mock.patch.object(GUARD, "WRITE_TOKENS", ("run",)):
            problems = GUARD.selftest()
        self.assertTrue(problems, "把写词表缩成 {run} 必须让自检红")
        self.assertIn("read-literal-write", "\n".join(problems))


class ProbeRegistryTests(unittest.TestCase):
    def test_the_real_entry_passes_every_registered_case(self) -> None:
        problems, summary = GUARD.run_probe(ENTRY)
        self.assertEqual(problems, [], summary)
        self.assertIn(f"{len(GUARD.PROBE_CASES)} cases", summary)

    def test_an_emptied_judgement_function_is_red(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            copied = Path(directory) / "entry.py"
            text = ENTRY.read_text(encoding="utf-8")
            start = text.index("def migration_verify(")
            end = text.index("\ndef ", start + 1)
            copied.write_text(
                text[:start]
                + "def migration_verify(head, database_version):\n    return None\n"
                + text[end:],
                encoding="utf-8",
            )
            problems, _summary = GUARD.run_probe(copied)
        self.assertTrue(problems)
        self.assertIn("migration_verify", "\n".join(problems))

    def test_a_probe_row_without_an_implementation_is_red(self) -> None:
        extra = GUARD.ProbeRow("ghost-case", "migration_verify", "fixture")
        # The anchor is regenerated by the same tables, so patching only the
        # registry also proves the anchor check sees the drift.
        with mock.patch.object(GUARD, "PROBE_CASES", (*GUARD.PROBE_CASES, extra)):
            problems, _summary = GUARD.run_probe(ENTRY)
        self.assertTrue(problems)
        message = "\n".join(problems)
        self.assertIn("不一致", message)
        self.assertIn("ghost-case", message)

    def test_an_emptied_bucket_names_probe_is_red(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            copied = Path(directory) / "entry.py"
            text = ENTRY.read_text(encoding="utf-8")
            emptied = text.replace(
                "def bucket_names(ctx: Context) -> list[str]:",
                "def bucket_names(ctx: Context) -> list[str]:\n    return []",
                1,
            )
            copied.write_text(emptied, encoding="utf-8")
            problems, _summary = GUARD.run_probe(copied)
        self.assertTrue(problems)
        self.assertIn("bucket_names", "\n".join(problems))

    def test_renaming_a_probed_function_points_at_the_registry(self) -> None:
        table = tuple(
            GUARD.ProbeRow(row.case, "environment_gaps", row.expectation, row.must_raise)
            if row.function == "env_gaps"
            else row
            for row in GUARD.PROBE_CASES
        )
        with mock.patch.object(GUARD, "PROBE_CASES", table):
            problems, _summary = GUARD.run_probe(ENTRY)
        self.assertTrue(problems)
        message = "\n".join(problems)
        self.assertIn("找不到入口函数", message)
        self.assertIn("PROBE_CASES", message)


class WiringTests(unittest.TestCase):
    def test_the_checked_in_workflow_runs_every_guard_as_the_first_command(self) -> None:
        problems, summary = GUARD.run_wiring(WORKFLOW)
        self.assertEqual(problems, [], summary)
        self.assertIn("4 guard steps", summary)

    def test_an_echo_only_replacement_is_red(self) -> None:
        text = WORKFLOW.read_text(encoding="utf-8")
        replaced = text.replace(
            "          python3 scripts/dev-env/ci-entry-guard.py sites",
            '          echo "python3 scripts/dev-env/ci-entry-guard.py sites"',
        )
        self.assertNotEqual(text, replaced)
        with tempfile.TemporaryDirectory() as directory:
            copied = Path(directory) / "ci.yml"
            copied.write_text(replaced, encoding="utf-8")
            problems, _summary = GUARD.run_wiring(copied)
        self.assertTrue(problems)
        message = "\n".join(problems)
        self.assertIn("echo", message)
        self.assertIn("sites", message)

    def test_a_missing_guard_subcommand_is_red(self) -> None:
        text = WORKFLOW.read_text(encoding="utf-8")
        stripped = text.replace(
            "      - name: Probe the registered pure judgement functions\n"
            "        # `--dry-run` returns from every state-changing step before judging, so an\n"
            "        # apply-path judgement that was emptied (migration_verify returning None\n"
            "        # forever) would stay green. `probe` imports the entry and calls the\n"
            "        # functions registered in PROBE_CASES with inputs that must fail; deleting a\n"
            "        # case, or the implementation behind it, is red because the registry and its\n"
            "        # documentation anchor are compared on every run.\n"
            "        run: |\n"
            "          python3 scripts/dev-env/ci-entry-guard.py probe\n",
            "",
        )
        self.assertNotEqual(text, stripped)
        with tempfile.TemporaryDirectory() as directory:
            copied = Path(directory) / "ci.yml"
            copied.write_text(stripped, encoding="utf-8")
            problems, _summary = GUARD.run_wiring(copied)
        self.assertTrue(problems)
        self.assertIn("probe", "\n".join(problems))


DRY_RUN_LOG = textwrap.dedent(
    """\
    steps=7 order=tools, migrate, roles, buckets, stack, gate, health mode=dry-run gate=compose
    [1/7] tools — 工具与版本
          结论: PLAN — 版本已确认
    [2/7] migrate — 迁移
          结论: PLAN — 待执行
    [3/7] roles — 角色
          结论: PLAN — 待执行
    [4/7] buckets — 桶
          结论: PLAN — 待执行
    [5/7] stack — 栈
          结论: PLAN — 待执行
    [6/7] gate — 闸门
          待输入: gateway 未 healthy；需要它先运行
          结论: PLAN — 待输入
    [7/7] health — 健康
          结论: PLAN — 待执行
    步骤记录: tools=PLAN migrate=PLAN roles=PLAN buckets=PLAN stack=PLAN gate=PLAN health=PLAN
    reference acceptance: DRY-RUN PASS steps=7 pending=1
    """
)

GATEWAY_DEFERRAL = "gateway 未 healthy；需要它先运行"
REWORDED_DEFERRAL = "gateway 尚未 healthy（需先启动）；复用其网络"
NEW_LANE_DEFERRAL = "缺少 gateway lane 配置: /run/speechnexus-private/gateway-file-voice.json"

APPLY_LOG = textwrap.dedent(
    """\
    steps=7 order=tools, migrate, roles, buckets, stack, gate, health mode=apply gate=direct
    [1/7] tools — 工具与版本
          结论: PASS — 版本已确认
    [2/7] migrate — 迁移
          结论: PASS — 已在 head
    [3/7] roles — 角色
          结论: PASS — 已安装
    [4/7] buckets — 桶
          结论: PASS — 已确认
    [5/7] stack — 栈
          结论: PASS — 已启动
    [6/7] gate — 闸门
          结论: PASS — 通过
    [7/7] health — 健康
          结论: PASS — 四个探针通过
    步骤记录: tools=PASS migrate=PASS roles=PASS buckets=PASS stack=PASS gate=PASS health=PASS
    reference acceptance: PASS steps=7
    """
)


class LogContractTests(unittest.TestCase):
    def check(self, text: str, *, mode: str = "dry-run") -> tuple[list[str], str]:
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / "run.log"
            log.write_text(text, encoding="utf-8")
            problems, summary = GUARD.check_log(log, GUARD.EXPECTED_STEP_ORDER, expect_mode=mode)
        return [str(problem) for problem in problems], str(summary)

    def test_the_contract_passes(self) -> None:
        problems, summary = self.check(DRY_RUN_LOG)
        self.assertEqual(problems, [], summary)
        self.assertIn("1 deferrals", summary)

    def test_an_apply_mode_log_is_parseable(self) -> None:
        problems, _summary = self.check(APPLY_LOG, mode="apply")
        self.assertEqual(problems, [])

    def test_a_reworded_deferral_stays_green(self) -> None:
        problems, _summary = self.check(DRY_RUN_LOG.replace(GATEWAY_DEFERRAL, REWORDED_DEFERRAL))
        self.assertEqual(problems, [])

    def test_a_new_deferral_is_red_with_the_registration_line(self) -> None:
        problems, _summary = self.check(
            DRY_RUN_LOG.replace(GATEWAY_DEFERRAL, NEW_LANE_DEFERRAL).replace(
                "pending=1", "pending=1"
            )
        )
        self.assertTrue(problems)
        message = "\n".join(problems)
        self.assertIn("未登记的延后项", message)
        self.assertIn("DECLARED_DEFERRALS", message)

    def test_a_deferral_that_mentions_another_condition_is_red(self) -> None:
        problems, _summary = self.check(
            DRY_RUN_LOG.replace(GATEWAY_DEFERRAL, "gateway 不 healthy：镜像未构建、模型未下载")
        )
        self.assertTrue(problems, "换了原因却包含 gateway/healthy 的延后项必须红")
        self.assertIn("未登记的延后项", "\n".join(problems))

    def test_step_ids_may_contain_digits(self) -> None:
        problems, _summary = self.check(
            DRY_RUN_LOG.replace("buckets,", "db2,")
            .replace("[4/7] buckets", "[4/7] db2")
            .replace("buckets=PLAN", "db2=PLAN")
        )
        # The order no longer matches the contract (that is expected), but the header
        # must still be *parsed* rather than reported as missing.
        self.assertNotIn("缺少机器可读首行", "\n".join(problems))

    def test_order_drift_is_red(self) -> None:
        problems, _summary = self.check(
            DRY_RUN_LOG.replace("buckets, stack, gate", "buckets, gate, stack")
        )
        self.assertTrue(problems)
        self.assertIn("order=", "\n".join(problems))

    def test_a_pending_mismatch_is_red(self) -> None:
        problems, _summary = self.check(DRY_RUN_LOG.replace("pending=1", "pending=0"))
        self.assertTrue(problems)
        self.assertIn("pending", "\n".join(problems))

    def test_a_dry_run_claiming_pass_is_red(self) -> None:
        problems, _summary = self.check(
            DRY_RUN_LOG.replace("结论: PLAN — 待执行", "结论: PASS — 待执行", 1)
        )
        self.assertTrue(problems)
        self.assertIn("PLAN", "\n".join(problems))

    def test_a_missing_log_is_an_error_not_a_pass(self) -> None:
        with self.assertRaises(FileNotFoundError):
            GUARD.check_log(Path("/nonexistent/log.txt"), GUARD.EXPECTED_STEP_ORDER)


class EntrypointTests(unittest.TestCase):
    def test_the_dry_run_contract_and_guards_agree_on_the_step_order(self) -> None:
        module = GUARD._load_entry(ENTRY)
        self.assertEqual([step.ident for step in module.STEPS], list(GUARD.EXPECTED_STEP_ORDER))

    def test_the_documented_contract_mentions_every_guard(self) -> None:
        text = DOC.read_text(encoding="utf-8")
        for name in ("sites", "probe", "wiring", "log"):
            with self.subTest(guard=name):
                self.assertIn(f"`{name}`", text)


if __name__ == "__main__":
    unittest.main()
