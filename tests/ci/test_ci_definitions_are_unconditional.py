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

Allowed exception: a module-level `if TYPE_CHECKING:` block may hold imports
(those are not `def`/`class`); anything defining at module level behind a
runtime condition is rejected.
"""

from __future__ import annotations

import ast
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SUITE = ROOT / "tests/ci"
GUARD_FILE = Path(__file__).resolve()

# Containers whose body runs at runtime only when some condition holds, one level
# down at a time: every branch of these hides a definition just as well as `if`.
BRANCHING = (
    ast.If,
    ast.For,
    ast.AsyncFor,
    ast.While,
    ast.With,
    ast.AsyncWith,
    ast.Try,
)
if hasattr(ast, "TryStar"):  # `except*` is its own node, not an `ast.Try`
    BRANCHING += (ast.TryStar,)

DEFINITION_NODES = (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)


def imported_type_checking(tree: ast.Module) -> bool:
    """Whether `TYPE_CHECKING` in this module really comes from `typing`.

    Matching the name alone would let `TYPE_CHECKING = bool(os.environ["X"])`
    turn a runtime condition into an accepted one.
    """
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.module == "typing":
            for alias in node.names:
                if (alias.asname or alias.name) == "TYPE_CHECKING":
                    return True
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "typing" and (alias.asname or "") in ("", "typing"):
                    return True
    return False


def _condition_children(node: ast.AST) -> list[ast.AST]:
    """The bodies this node runs conditionally, at any depth of the same shape."""
    children: list[ast.AST] = []
    for field in ("body", "orelse", "finalbody"):
        for statement in getattr(node, field, []) or []:
            children.append(statement)
    for handler in getattr(node, "handlers", []) or []:
        children.extend(handler.body)
    for case in getattr(node, "cases", []) or []:
        children.extend(case.body)
    return children


def _walk_conditional(
    statements: list[ast.AST], *, typing_imported: bool, problems: list[str], where: str
) -> None:
    for statement in statements:
        if isinstance(statement, DEFINITION_NODES):
            kind = type(statement).__name__
            problems.append(f"{where}: defines {kind} {statement.name} at line {statement.lineno}")
            continue  # 函数/类内部的局部定义不算模块级隐藏
        if (
            isinstance(statement, ast.If)
            and typing_imported
            and ast.unparse(statement.test) == "TYPE_CHECKING"
        ):
            # 只允许 import; 类/函数写在恒 False 的分支里在运行时永远不存在。
            for inner in statement.body + statement.orelse:
                if isinstance(inner, DEFINITION_NODES):
                    problems.append(
                        f"{where}: defines {type(inner).__name__} {inner.name} at line "
                        f"{inner.lineno} inside `if TYPE_CHECKING:`"
                    )
            continue
        if isinstance(statement, (*BRANCHING, ast.Match)):
            _walk_conditional(
                _condition_children(statement),
                typing_imported=typing_imported,
                problems=problems,
                where=where,
            )
            continue
        if isinstance(statement, ast.ClassDef):
            continue


def conditional_definitions(source: str, filename: str = "<string>") -> list[str]:
    """Module-level runtime conditions that decide whether a definition exists."""
    tree = ast.parse(source)
    problems: list[str] = []
    typing_imported = imported_type_checking(tree)
    # 模块体: 任何条件容器里的 class/def 都算隐藏。
    for statement in tree.body:
        if isinstance(statement, (*BRANCHING, ast.Match)):
            if (
                isinstance(statement, ast.If)
                and typing_imported
                and ast.unparse(statement.test) == "TYPE_CHECKING"
            ):
                for inner in statement.body + statement.orelse:
                    if isinstance(inner, DEFINITION_NODES):
                        problems.append(
                            f"{filename}: defines {type(inner).__name__} {inner.name} at "
                            f"line {inner.lineno} inside `if TYPE_CHECKING:`"
                        )
                continue
            _walk_conditional(
                _condition_children(statement),
                typing_imported=typing_imported,
                problems=problems,
                where=filename,
            )
    # 类体: 条件包住的方法同样会让用例悄悄消失。
    for statement in tree.body:
        if isinstance(statement, ast.ClassDef):
            for item in statement.body:
                if isinstance(item, (*BRANCHING, ast.Match)):
                    _walk_conditional(
                        _condition_children(item),
                        typing_imported=typing_imported,
                        problems=problems,
                        where=filename,
                    )
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

    def test_type_checking_is_only_trusted_when_it_comes_from_typing(self) -> None:
        rebound = (
            "import os\n"
            "import unittest\n"
            "TYPE_CHECKING = bool(os.environ.get('X'))\n"
            "if TYPE_CHECKING:\n"
            "    class HiddenTest(unittest.TestCase):\n"
            "        def test_x(self) -> None:\n"
            "            pass\n"
        )
        problems = conditional_definitions(rebound, "sample.py")
        self.assertEqual(len(problems), 1, problems)

    def test_no_definition_inside_a_type_checking_block(self) -> None:
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

    def test_a_type_checking_block_may_still_import(self) -> None:
        source = (
            "from typing import TYPE_CHECKING\n"
            "if TYPE_CHECKING:\n"
            "    from collections import OrderedDict\n"
        )
        self.assertEqual(conditional_definitions(source, "sample.py"), [])

    def test_skip_unless_decorator_is_the_accepted_shape(self) -> None:
        source = (
            "import unittest\n"
            "@unittest.skipUnless(host_can_plan(), 'no plan on this host')\n"
            "class SkippedButPresentTest(unittest.TestCase):\n"
            "    def test_x(self) -> None:\n"
            "        pass\n"
        )
        self.assertEqual(conditional_definitions(source, "sample.py"), [])
