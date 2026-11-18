"""A test class must not be defined inside a module-level conditional.

`tests/ci/test_build_sequences_without_minio.py` used to read

```python
if up_build_plan():
    class ComposeNegativeControlTest(unittest.TestCase): ...
```

When the condition was false the class did not exist at all: unittest never
counted it, never listed it as skipped, and the summary still said `OK`. On a
host whose `docker` was momentarily unavailable the same suite reported
`Ran 562` instead of `Ran 567`, both green — "the check did not run" and "the
check passed" became indistinguishable in the only place anyone reads.

The fix is `@unittest.skipUnless(...)` on the class, so the tests exist, run,
and report `skipped` with a reason. This guard keeps the shape from coming
back: it parses the sources, it does not import them, and it needs no service.

Every conditional container is walked at any depth — `if`/`elif`/`else`, loops,
`with`, `match`, `try`/`except`/`finally`/`except*` — and the class body is
walked too, because a method hidden behind a condition makes the *test* vanish
just as quietly. `if TYPE_CHECKING:` gets no exception: imports are not looked
at, and a class written there never exists at runtime, which is exactly the
failure this guard exists to catch.

Out of scope, because a static read cannot see them: `del X`,
`globals()["X"] = type(...)`, and `X = type(...)` building a class without an
`ast.ClassDef` (#995).
"""

from __future__ import annotations

import ast
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SUITE = ROOT / "tests/ci"
GUARD_FILE = Path(__file__).resolve()

# Containers whose body runs only when something holds. `except*` is its own node
# (`ast.TryStar`), not an `ast.Try`, so it is added when the interpreter has it.
BRANCHING: tuple[type[ast.AST], ...] = (
    ast.If,
    ast.For,
    ast.AsyncFor,
    ast.While,
    ast.With,
    ast.AsyncWith,
    ast.Try,
)
if hasattr(ast, "TryStar"):
    BRANCHING += (ast.TryStar,)
CONDITIONAL = (*BRANCHING, ast.Match)

DEFINITION_NODES = (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)


def _conditional_bodies(node: ast.AST) -> list[ast.AST]:
    """The statements this node runs on a condition, one level down."""
    children: list[ast.AST] = []
    for field in ("body", "orelse", "finalbody"):
        children.extend(getattr(node, field, []) or [])
    for handler in getattr(node, "handlers", []) or []:
        children.extend(handler.body)
    for case in getattr(node, "cases", []) or []:
        children.extend(case.body)
    return children


def _walk(statements: list[ast.AST], *, problems: list[str], where: str) -> None:
    for statement in statements:
        if isinstance(statement, DEFINITION_NODES):
            kind = type(statement).__name__
            problems.append(f"{where}: defines {kind} {statement.name} at line {statement.lineno}")
            # A local class/def inside a function is not a module-level hide.
            continue
        if isinstance(statement, CONDITIONAL):
            _walk(_conditional_bodies(statement), problems=problems, where=where)
            continue
        if isinstance(statement, ast.ClassDef):  # pragma: no cover - handled above
            continue


def conditional_definitions(source: str, filename: str = "<string>") -> list[str]:
    """Definitions a module-level or class-body runtime condition can hide."""
    tree = ast.parse(source)
    problems: list[str] = []
    for statement in tree.body:
        if isinstance(statement, CONDITIONAL):
            _walk(_conditional_bodies(statement), problems=problems, where=filename)
    # A method hidden in the class body disappears just as silently as a class.
    for statement in tree.body:
        if isinstance(statement, ast.ClassDef):
            for item in statement.body:
                if isinstance(item, CONDITIONAL):
                    _walk(_conditional_bodies(item), problems=problems, where=filename)
    return problems


class SuiteDefinitionsAreUnconditionalTest(unittest.TestCase):
    """Every class in `tests/ci` is defined unconditionally."""

    def test_no_ci_module_hides_a_class_behind_a_runtime_condition(self) -> None:
        problems: list[str] = []
        for path in sorted(SUITE.glob("*.py")):
            if path.resolve() == GUARD_FILE:
                continue
            problems.extend(conditional_definitions(path.read_text(encoding="utf-8"), path.name))
        self.assertEqual(
            problems, [], "a test class behind a runtime condition disappears silently"
        )

    def test_the_guard_sees_the_shape_it_forbids(self) -> None:
        # 反例: 把类放回模块级 if 里, 必须被这双眼睛看见。
        source = (
            "import unittest\n"
            "if host_can_plan():\n"
            "    class BehindAConditionTest(unittest.TestCase):\n"
            "        def test_x(self) -> None:\n"
            "            pass\n"
        )
        problems = conditional_definitions(source, "sample.py")
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("BehindAConditionTest", problems[0])

    def test_a_try_block_cannot_hide_one_either(self) -> None:
        source = (
            "import unittest\n"
            "try:\n"
            "    import docker  # noqa: F401\n"
            "    class ImportedOnlyTest(unittest.TestCase):\n"
            "        def test_x(self) -> None:\n"
            "            pass\n"
            "except ImportError:\n"
            "    pass\n"
        )
        problems = conditional_definitions(source, "sample.py")
        self.assertEqual(len(problems), 1, problems)

    def test_skip_unless_decorator_is_the_accepted_shape(self) -> None:
        source = (
            "import unittest\n"
            "@unittest.skipUnless(host_can_plan(), 'no plan on this host')\n"
            "class SkippedButPresentTest(unittest.TestCase):\n"
            "    def test_x(self) -> None:\n"
            "        pass\n"
        )
        self.assertEqual(conditional_definitions(source, "sample.py"), [])

    def test_a_nested_condition_cannot_hide_one(self) -> None:
        source = (
            "import unittest\n"
            "if a:\n"
            "    if b:\n"
            "        class NestedTest(unittest.TestCase):\n"
            "            def test_x(self) -> None:\n"
            "                pass\n"
        )
        problems = conditional_definitions(source, "sample.py")
        self.assertEqual(len(problems), 1, problems)

    def test_elif_and_else_branches_cannot_hide_one(self) -> None:
        elif_source = (
            "import unittest\n"
            "if a:\n"
            "    pass\n"
            "elif b:\n"
            "    class ElifTest(unittest.TestCase):\n"
            "        def test_x(self) -> None:\n"
            "            pass\n"
        )
        else_source = (
            "import unittest\n"
            "if a:\n"
            "    pass\n"
            "elif b:\n"
            "    pass\n"
            "else:\n"
            "    class ElseTest(unittest.TestCase):\n"
            "        def test_x(self) -> None:\n"
            "            pass\n"
        )
        self.assertEqual(len(conditional_definitions(elif_source, "sample.py")), 1)
        self.assertEqual(len(conditional_definitions(else_source, "sample.py")), 1)

    def test_loops_with_and_match_cannot_hide_one(self) -> None:
        sources = [
            "while flag:\n    class WhileTest(unittest.TestCase):\n        pass\n",
            "for _ in ():\n    class ForTest(unittest.TestCase):\n        pass\n",
            "with open('x'):\n    class WithTest(unittest.TestCase):\n        pass\n",
            "match value:\n"
            "    case 1:\n"
            "        class MatchTest(unittest.TestCase):\n"
            "            pass\n",
        ]
        for source in sources:
            problems = conditional_definitions("import unittest\n" + source, "sample.py")
            self.assertEqual(len(problems), 1, (source, problems))

    def test_except_star_cannot_hide_one(self) -> None:
        source = (
            "import unittest\n"
            "try:\n"
            "    pass\n"
            "except* ValueError:\n"
            "    class StarTest(unittest.TestCase):\n"
            "        def test_x(self) -> None:\n"
            "            pass\n"
        )
        problems = conditional_definitions(source, "sample.py")
        self.assertEqual(len(problems), 1, problems)

    def test_a_condition_around_a_test_method_is_red(self) -> None:
        source = (
            "import unittest\n"
            "class RealTest(unittest.TestCase):\n"
            "    if host_can_plan():\n"
            "        def test_x(self) -> None:\n"
            "            pass\n"
        )
        problems = conditional_definitions(source, "sample.py")
        self.assertEqual(len(problems), 1, problems)

    def test_type_checking_is_not_an_exception(self) -> None:
        # `if TYPE_CHECKING:` 恒 False, 写在里面的用例运行时永不存在。
        source = (
            "from typing import TYPE_CHECKING\n"
            "import unittest\n"
            "if TYPE_CHECKING:\n"
            "    class NeverExistsTest(unittest.TestCase):\n"
            "        def test_x(self) -> None:\n"
            "            pass\n"
        )
        problems = conditional_definitions(source, "sample.py")
        self.assertEqual(len(problems), 1, problems)

    def test_a_rebound_condition_name_is_no_safer(self) -> None:
        source = (
            "import os\n"
            "import unittest\n"
            "TYPE_CHECKING = bool(os.environ.get('X'))\n"
            "if TYPE_CHECKING:\n"
            "    class HiddenTest(unittest.TestCase):\n"
            "        def test_x(self) -> None:\n"
            "            pass\n"
        )
        problems = conditional_definitions(source, "sample.py")
        self.assertEqual(len(problems), 1, problems)

    def test_a_type_checking_block_may_still_import(self) -> None:
        source = (
            "from typing import TYPE_CHECKING\n"
            "if TYPE_CHECKING:\n"
            "    from collections import OrderedDict\n"
        )
        self.assertEqual(conditional_definitions(source, "sample.py"), [])

    def test_a_definition_inside_type_checking_is_red_at_any_depth(self) -> None:
        source = (
            "from typing import TYPE_CHECKING\n"
            "import unittest\n"
            "if TYPE_CHECKING:\n"
            "    if cond():\n"
            "        class DeepTest(unittest.TestCase):\n"
            "            def test_x(self) -> None:\n"
            "                pass\n"
        )
        problems = conditional_definitions(source, "sample.py")
        self.assertEqual(len(problems), 1, problems)
