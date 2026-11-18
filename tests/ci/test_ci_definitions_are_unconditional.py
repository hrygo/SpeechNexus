"""A test class must not be defined inside a module-level conditional, nor erased later.

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

#995 named three more shapes, and they do not all hide a class. Measured on a
probe module with `python3 -m unittest discover` and `pytest --collect-only`,
which agreed: `del X` after the class body leaves the name unbound, so neither
collector finds the class and the probe ran 6 tests instead of 8. But
`X = type("X", (unittest.TestCase,), {...})` and `globals()["X"] = type(...)`
do bind a module attribute, and both collectors found those classes — they are
not hidden, so nothing here flags them. Each becomes hidden only when it runs
under a runtime condition: the assignment never happens, the class never
exists, and the count silently drops. `dynamic_definitions` splits on that
line: it catches deletion (`del X`, `del globals()[...]`, `globals().pop(...)`)
and dynamic construction gated by a condition. Unconditional dynamic
construction stays green because it is collected and counted. Rebinding the
name away (`SomeTest = None`) hides the class the same way and is not caught:
the guard follows deletion and condition-gated construction, not every write.
"""

from __future__ import annotations

import ast
import unittest
from collections.abc import Sequence
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


def _is_module_namespace_call(node: ast.AST) -> bool:
    """`globals()` / `locals()`: the runtime handle on a module's own namespace."""
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in ("globals", "locals")
    )


def _is_type_call(node: ast.AST) -> bool:
    """`type(name, bases, dict)` builds a class; `type(value)` only reads its type."""
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "type"
        and len(node.args) >= 2
    )


def _literal_name(target: ast.AST) -> str | None:
    """The module-level name a target writes, when it is spelled out literally."""
    if isinstance(target, ast.Name):
        return target.id
    if (
        isinstance(target, ast.Subscript)
        and _is_module_namespace_call(target.value)
        and isinstance(target.slice, ast.Constant)
        and isinstance(target.slice.value, str)
    ):
        return target.slice.value
    return None


def _writes_module_scope(target: ast.AST) -> bool:
    """Whether this target lands in the module namespace rather than a local one."""
    return isinstance(target, ast.Name) or (
        isinstance(target, ast.Subscript) and _is_module_namespace_call(target.value)
    )


def _class_bindings(tree: ast.Module) -> dict[str, int]:
    """Module names holding a class before any runtime condition is evaluated."""
    names: dict[str, int] = {}
    for statement in tree.body:
        if isinstance(statement, ast.ClassDef):
            names[statement.name] = statement.lineno
        elif isinstance(statement, ast.Assign) and _is_type_call(statement.value):
            for target in statement.targets:
                name = _literal_name(target)
                if name is not None:
                    names[name] = statement.lineno
        elif (
            isinstance(statement, ast.AnnAssign)
            and statement.value is not None
            and _is_type_call(statement.value)
        ):
            name = _literal_name(statement.target)
            if name is not None:
                names[name] = statement.lineno
    return names


def _module_scope(
    statements: Sequence[ast.AST], *, conditional: bool = False
) -> list[tuple[ast.AST, bool]]:
    """Module-scope statements, flagged when a runtime condition gates them.

    Function and class bodies are not entered: a `del` there removes a local or
    class attribute, not the module attribute a collector enumerates.
    """
    found: list[tuple[ast.AST, bool]] = []
    for statement in statements:
        if isinstance(statement, CONDITIONAL):
            found.extend(_module_scope(_conditional_bodies(statement), conditional=True))
            continue
        found.append((statement, conditional))
    return found


def _may_name_a_class(key: ast.AST, classes: dict[str, int]) -> bool:
    """A literal key matters only if it names a class; a computed key may name one."""
    if isinstance(key, ast.Constant) and isinstance(key.value, str):
        return key.value in classes
    return True


def _describe_key(key: ast.AST) -> str:
    if isinstance(key, ast.Constant) and isinstance(key.value, str):
        return repr(key.value)
    return "a computed name"


def _pops_from_namespace(node: ast.Call) -> bool:
    """`globals().pop(...)`: deletion spelled as a method call."""
    return (
        isinstance(node.func, ast.Attribute)
        and node.func.attr == "pop"
        and _is_module_namespace_call(node.func.value)
    )


def dynamic_definitions(source: str, filename: str = "<string>") -> list[str]:
    """Classes a module deletes, or builds only when a runtime condition holds.

    Deletion is red whenever the erased name is a class — defined by `class` or
    bound by an unconditional `type(...)` — because the class then never reaches
    the collector. Construction by `type(...)` is red only when a condition
    gates it: an unconditional one is collected and counted, so it hides nothing.
    """
    tree = ast.parse(source)
    classes = _class_bindings(tree)
    problems: list[str] = []
    for statement, conditional in _module_scope(tree.body):
        if isinstance(statement, ast.Delete):
            for target in statement.targets:
                name = _literal_name(target)
                if isinstance(target, ast.Name) and name in classes:
                    problems.append(
                        f"{filename}: deletes class {name} at line {statement.lineno} "
                        f"(defined at line {classes[name]})"
                    )
                elif (
                    isinstance(target, ast.Subscript)
                    and _is_module_namespace_call(target.value)
                    and _may_name_a_class(target.slice, classes)
                ):
                    problems.append(
                        f"{filename}: deletes {_describe_key(target.slice)} through the "
                        f"module namespace at line {statement.lineno}"
                    )
        elif (
            isinstance(statement, ast.Expr)
            and isinstance(statement.value, ast.Call)
            and _pops_from_namespace(statement.value)
            and statement.value.args
            and _may_name_a_class(statement.value.args[0], classes)
        ):
            problems.append(
                f"{filename}: pops {_describe_key(statement.value.args[0])} out of the "
                f"module namespace at line {statement.lineno}"
            )
        elif (
            conditional
            and isinstance(statement, (ast.Assign, ast.AnnAssign))
            and statement.value is not None
            and _is_type_call(statement.value)
        ):
            targets = statement.targets if isinstance(statement, ast.Assign) else [statement.target]
            names = [name for name in map(_literal_name, targets) if name is not None]
            if any(_writes_module_scope(target) for target in targets):
                problems.append(
                    f"{filename}: builds class {names[0] if names else '<computed>'} with "
                    f"type() only when a runtime condition holds at line {statement.lineno}"
                )
    return problems


class SuiteDefinitionsAreUnconditionalTest(unittest.TestCase):
    """Every class in `tests/ci` is defined unconditionally and stays defined."""

    def test_no_ci_module_hides_a_class_behind_a_runtime_condition(self) -> None:
        problems: list[str] = []
        for path in sorted(SUITE.glob("*.py")):
            if path.resolve() == GUARD_FILE:
                continue
            problems.extend(conditional_definitions(path.read_text(encoding="utf-8"), path.name))
        self.assertEqual(
            problems, [], "a test class behind a runtime condition disappears silently"
        )

    def test_no_ci_module_erases_a_class_or_builds_one_only_conditionally(self) -> None:
        problems: list[str] = []
        for path in sorted(SUITE.glob("*.py")):
            if path.resolve() == GUARD_FILE:
                continue
            problems.extend(dynamic_definitions(path.read_text(encoding="utf-8"), path.name))
        self.assertEqual(
            problems,
            [],
            "a class erased after definition, or built only under a condition, disappears "
            "from collection without a skip",
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

    def test_a_module_level_del_hides_a_defined_class(self) -> None:
        # 实测: `del X` 之后 unittest 与 pytest 都收不到这个类 (6 条而不是 8 条)。
        source = (
            "import unittest\n"
            "class DeletedTest(unittest.TestCase):\n"
            "    def test_x(self) -> None:\n"
            "        pass\n"
            "del DeletedTest\n"
        )
        problems = dynamic_definitions(source, "sample.py")
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("DeletedTest", problems[0])

    def test_a_del_inside_a_module_condition_is_red_too(self) -> None:
        source = (
            "import unittest\n"
            "class SometimesGoneTest(unittest.TestCase):\n"
            "    def test_x(self) -> None:\n"
            "        pass\n"
            "if host_can_plan():\n"
            "    del SometimesGoneTest\n"
        )
        problems = dynamic_definitions(source, "sample.py")
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("SometimesGoneTest", problems[0])

    def test_deleting_a_class_through_the_module_namespace_is_red(self) -> None:
        source = (
            "import unittest\n"
            "class ViaNamespaceTest(unittest.TestCase):\n"
            "    def test_x(self) -> None:\n"
            "        pass\n"
            "del globals()['ViaNamespaceTest']\n"
        )
        problems = dynamic_definitions(source, "sample.py")
        self.assertEqual(len(problems), 1, problems)

    def test_popping_a_class_out_of_the_module_namespace_is_red(self) -> None:
        source = (
            "import unittest\n"
            "class PoppedTest(unittest.TestCase):\n"
            "    def test_x(self) -> None:\n"
            "        pass\n"
            "globals().pop('PoppedTest')\n"
        )
        problems = dynamic_definitions(source, "sample.py")
        self.assertEqual(len(problems), 1, problems)

    def test_a_class_injected_then_deleted_is_red(self) -> None:
        # 绑定不是 ClassDef, 但删掉之后同样从收集里消失。
        source = (
            "import unittest\n"
            "globals()['GoneTest'] = type('GoneTest', (unittest.TestCase,), {})\n"
            "del globals()['GoneTest']\n"
        )
        problems = dynamic_definitions(source, "sample.py")
        self.assertEqual(len(problems), 1, problems)

    def test_a_class_built_by_type_under_a_condition_is_red(self) -> None:
        # 无条件时两个收集器都收得到; 有条件时整个类不会存在。
        source = (
            "import unittest\n"
            "if host_can_plan():\n"
            "    LateTest = type('LateTest', (unittest.TestCase,), {'test_x': lambda s: None})\n"
        )
        problems = dynamic_definitions(source, "sample.py")
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("LateTest", problems[0])

    def test_a_class_injected_into_globals_under_a_condition_is_red(self) -> None:
        source = (
            "import unittest\n"
            "if host_can_plan():\n"
            "    globals()['InjectedTest'] = type(\n"
            "        'InjectedTest', (unittest.TestCase,), {'test_x': lambda s: None}\n"
            "    )\n"
        )
        problems = dynamic_definitions(source, "sample.py")
        self.assertEqual(len(problems), 1, problems)

    def test_an_unconditional_type_built_class_is_collected_so_it_is_green(self) -> None:
        # 实测: 模块属性一赋值, unittest 与 pytest 都收得到, 不是隐藏。
        source = (
            "import unittest\n"
            "BareTest = type('BareTest', (unittest.TestCase,), {'test_x': lambda s: None})\n"
        )
        self.assertEqual(dynamic_definitions(source, "sample.py"), [])

    def test_an_unconditional_globals_injection_is_green(self) -> None:
        source = (
            "import unittest\n"
            "globals()['InjectedTest'] = type('InjectedTest', (unittest.TestCase,), {})\n"
        )
        self.assertEqual(dynamic_definitions(source, "sample.py"), [])

    def test_deleting_a_plain_module_variable_is_green(self) -> None:
        source = "import os\nTEMP = os.environ.get('X')\ndel TEMP\n"
        self.assertEqual(dynamic_definitions(source, "sample.py"), [])

    def test_deleting_a_local_inside_a_function_is_green(self) -> None:
        source = (
            "import unittest\n"
            "class RealTest(unittest.TestCase):\n"
            "    def test_x(self) -> None:\n"
            "        scratch = 1\n"
            "        del scratch\n"
            "        self.assertTrue(True)\n"
        )
        self.assertEqual(dynamic_definitions(source, "sample.py"), [])

    def test_a_plain_globals_assignment_is_green(self) -> None:
        source = "globals()['x'] = 1\n"
        self.assertEqual(dynamic_definitions(source, "sample.py"), [])

    def test_deleting_an_unrelated_globals_entry_is_green(self) -> None:
        source = "globals()['CACHE'] = {}\ndel globals()['CACHE']\n"
        self.assertEqual(dynamic_definitions(source, "sample.py"), [])

    def test_a_type_query_or_annotation_is_not_a_class_factory(self) -> None:
        # `type(1)` 只读类型; `LIMIT: type = int` 只是注解。两者都不建类。
        sources = [
            "import unittest\nif host_can_plan():\n    kind = type(1)\n",
            "import unittest\nif host_can_plan():\n    LIMIT: type = int\n",
            "import unittest\nassert type(1) is int\n",
        ]
        for source in sources:
            self.assertEqual(dynamic_definitions(source, "sample.py"), [], source)
