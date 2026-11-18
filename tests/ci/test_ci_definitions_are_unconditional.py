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

# `if TYPE_CHECKING:` only exists for the type checker; it is not a runtime
# condition, and it may not define classes either — but it is legitimate for
# imports, which this walk does not look at.
ALLOWED_CONDITIONS = {"TYPE_CHECKING"}


def _condition_text(node: ast.If) -> str:
    return ast.unparse(node.test)


def _definitions(node: ast.AST) -> list[str]:
    """Class/function definitions written directly in this node's bodies."""
    found: list[str] = []
    bodies: list[list[ast.stmt]] = []
    if isinstance(node, ast.If):
        bodies = [node.body, node.orelse]
    elif isinstance(node, ast.Try):
        bodies = [node.body, node.orelse, node.finalbody] + [
            handler.body for handler in node.handlers
        ]
    else:  # pragma: no cover - callers only pass If/Try
        return found
    for body in bodies:
        for statement in body:
            if isinstance(statement, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                found.append(
                    f"{type(statement).__name__} {statement.name} at line {statement.lineno}"
                )
    return found


def conditional_definitions(source: str, filename: str = "<string>") -> list[str]:
    """Module-level runtime conditions that decide whether a definition exists."""
    tree = ast.parse(source)
    problems: list[str] = []
    for node in tree.body:
        if not isinstance(node, (ast.If, ast.Try)):
            continue
        if isinstance(node, ast.If) and _condition_text(node) in ALLOWED_CONDITIONS:
            continue
        for definition in _definitions(node):
            problems.append(
                f"{filename}: {type(node).__name__} at line {node.lineno} defines {definition}"
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

    def test_skip_unless_decorator_is_the_accepted_shape(self) -> None:
        source = (
            "import unittest\n"
            "@unittest.skipUnless(host_can_plan(), 'no plan on this host')\n"
            "class SkippedButPresentTest(unittest.TestCase):\n"
            "    def test_x(self) -> None:\n"
            "        pass\n"
        )
        self.assertEqual(conditional_definitions(source, "sample.py"), [])
