"""TTS 取消/准入屏障只有一个键定义点和一种载体:advisory lock 不得回来。

两处站点(准入的 ``tts_settlement_sql.insert_tts_settlement`` 与取消的
``tts_settlement_postgres.request_cancellation``)必须串行化同一个 ``operation_id``。它们
写的是两张不同的表,取消行在「还没有人取消」时不存在,所以没有可以共用的天然行:
``SELECT … FOR UPDATE`` 在行不存在时不锁任何东西,而 ``due()`` 只从结算行读 ``abort_reason``
—— 原因没被快照进去就没有补救路径。载体因此是 ``coordination_keyed_locks`` 的那一行。

这个守卫把两条主张变成可复算的断言:

* 键文本 ``speechnexus:tts-cancel`` 在后端源码里只出现**一次**(``tts_cancel_barrier`` 的
  ``_KEY_PREFIX``)。两处各拼一份就会在某次改动后算出两个桶号,互斥静默裂成两把;
* 两个站点模块里不再有 ``pg_advisory_xact_lock``,而且各自只调用一次同一个入口
  ``lock_tts_cancel`` —— 少一次是「这条路径不再取锁」,多一次是「又拼了一份」,都不是恒等。

文末的注入用例在临时树里把两处各改坏一次(复制一份键前缀 / 把 advisory lock 放回去),
同一个判据分别报出违规;干净副本仍然为空。
"""

from __future__ import annotations

import ast
import shutil
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BACKEND_RELATIVE = "backend/src/speechnexus_backend"
INFRASTRUCTURE = "speech/infrastructure"

#: 键文本的唯一字面量所在模块。
OWNER = f"{INFRASTRUCTURE}/tts_cancel_barrier.py"
#: 键前缀。全仓只允许出现一次。
KEY_PREFIX = "speechnexus:tts-cancel"
#: 两个站点:各自只允许调用 ``lock_tts_cancel``。
SITES = (
    f"{INFRASTRUCTURE}/tts_settlement_sql.py",
    f"{INFRASTRUCTURE}/tts_settlement_postgres.py",
)
#: 唯一的取锁入口。
ENTRY_POINT = "lock_tts_cancel"
#: 被替换掉的载体。
ADVISORY = "pg_advisory_xact_lock"


def _modules(root: Path) -> list[tuple[str, Path]]:
    backend = root / BACKEND_RELATIVE
    return [(str(path.relative_to(backend)), path) for path in sorted(backend.rglob("*.py"))]


def _docstring_nodes(tree: ast.Module) -> set[int]:
    """模块、类与函数的 docstring 节点:它们是说明,不是第二份拼法。"""

    nodes: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", ())
            if body and isinstance(body[0], ast.Expr):
                value = body[0].value
                if isinstance(value, ast.Constant) and isinstance(value.value, str):
                    nodes.add(id(value))
    return nodes


def _key_prefix_texts(root: Path) -> list[str]:
    hits: list[str] = []
    for _, path in _modules(root):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        docstrings = _docstring_nodes(tree)
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and KEY_PREFIX in node.value
                and id(node) not in docstrings
            ):
                hits.append(node.value)
    return hits


def _entry_point_calls(path: Path) -> int:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return sum(
        1
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and getattr(node.func, "id", None) == ENTRY_POINT
    )


def violations(root: Path) -> list[str]:
    """键定义点与两处站点的载体判据;空列表才是合格。"""

    problems: list[str] = []
    hits = _key_prefix_texts(root)
    if len(hits) != 1:
        problems.append(f"key prefix has {len(hits)} definition points: {hits}")
    owner = root / BACKEND_RELATIVE / OWNER
    if not owner.is_file():
        problems.append(f"{OWNER} is missing")
    for site in SITES:
        path = root / BACKEND_RELATIVE / site
        if not path.is_file():
            problems.append(f"{site} is missing")
            continue
        source = path.read_text(encoding="utf-8")
        if ADVISORY in source:
            problems.append(f"{site} still builds an advisory lock")
        calls = _entry_point_calls(path)
        if calls != 1:
            problems.append(f"{site} calls {ENTRY_POINT} {calls} times")
    return problems


class TtsCancelBarrierTest(unittest.TestCase):
    def test_the_sites_share_one_key_definition_point_and_one_carrier(self) -> None:
        self.assertEqual(violations(ROOT), [])

    def test_a_second_copy_of_the_key_prefix_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            copy = Path(temporary) / "repo"
            shutil.copytree(ROOT / "backend/src", copy / "backend/src")
            duplicated = copy / BACKEND_RELATIVE / SITES[0]
            source = duplicated.read_text(encoding="utf-8")
            injected = source.replace(
                '    "submit_intent_id",',
                '    "speechnexus:tts-cancel:copy",\n    "submit_intent_id",',
                1,
            )
            self.assertNotEqual(injected, source)
            duplicated.write_text(injected, encoding="utf-8")

            problems = violations(copy)

            self.assertTrue(any("definition points" in problem for problem in problems), problems)

    def test_an_advisory_lock_back_on_one_site_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            copy = Path(temporary) / "repo"
            shutil.copytree(ROOT / "backend/src", copy / "backend/src")
            restored = copy / BACKEND_RELATIVE / SITES[1]
            restored.write_text(
                restored.read_text(encoding="utf-8")
                + '\nADVISORY = "SELECT pg_advisory_xact_lock(1)"\n',
                encoding="utf-8",
            )

            problems = violations(copy)

            self.assertTrue(any("advisory lock" in problem for problem in problems), problems)

    def test_a_site_that_stops_calling_the_entry_point_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            copy = Path(temporary) / "repo"
            shutil.copytree(ROOT / "backend/src", copy / "backend/src")
            dropped = copy / BACKEND_RELATIVE / SITES[1]
            source = dropped.read_text(encoding="utf-8")
            injected = source.replace(f"{ENTRY_POINT}(conn,", "pass  # (conn,", 1)
            self.assertNotEqual(injected, source)
            dropped.write_text(injected, encoding="utf-8")

            problems = violations(copy)

            self.assertTrue(
                any("calls lock_tts_cancel 0 times" in problem for problem in problems),
                problems,
            )


if __name__ == "__main__":
    unittest.main()
