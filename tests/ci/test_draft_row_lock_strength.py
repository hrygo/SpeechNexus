"""草稿行只有一个锁强度:`SELECT ... FOR UPDATE`.

`speech_voice_drafts` 的一行是准入与删除之间唯一的串行点。同一批代码曾同时用
`FOR SHARE`(准入读取修订前后的共享锁)和 `FOR UPDATE OF d`(钉住创建路径的目标锁),
两种强度落在同一行上,判定就不再是「这条路径怎么锁草稿」,而是逐语句各说各话。

这个测试把主张变成可复算的断言:遍历后端源码里的 SQL 文本,凡锁 `speech_voice_drafts`
行的语句,锁子句只允许 `FOR UPDATE`;并核对每个模块的锁语句条数 —— 删掉一条锁语句是
「没人再锁」,不是「强度统一」,同样变红。

判据纪律要求「断言必须会红」:文末两个注入用例在临时树里把一条语句改回 `FOR SHARE`、
再删掉一条锁语句,分别看到强度断言与条数断言失败;干净副本仍然绿。

本文件属于 `tests/ci` 的免依赖守卫车道:只用标准库,不用 pytest fixture 与 parametrize。
"""

from __future__ import annotations

import ast
import re
import shutil
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend/src/speechnexus_backend"

#: 被锁的行所属的表。判定只看锁子句所在语句里的这张表名。
DRAFT_TABLE = "speech_voice_drafts"
#: 锁子句的写法集合。只允许一个成员。
_LOCK = re.compile(
    r"\bFOR\s+(NO\s+KEY\s+UPDATE|KEY\s+SHARE|SHARE|UPDATE)\b",
    re.IGNORECASE,
)
#: 每个模块锁草稿行的语句条数。
EXPECTED_SITES = {
    "speech/infrastructure/public_job_admission.py": 2,
    "speech/infrastructure/public_jobs_postgres.py": 3,
    "speech/infrastructure/voice_drafts_postgres.py": 1,
    "speech/infrastructure/voice_publications_postgres.py": 1,
    "speech/infrastructure/deletion_executions_postgres.py": 1,
}


class _Text:
    """把一条 Python 表达式还原成它拼出的 SQL 文本(只认字符串拼装)。"""

    def __init__(self, tree: ast.Module) -> None:
        self._assignments: dict[str, ast.expr] = {}
        for node in tree.body:
            if isinstance(node, ast.Assign) and len(node.targets) == 1:
                target = node.targets[0]
                if isinstance(target, ast.Name):
                    self._assignments[target.id] = node.value
        self._resolving: set[str] = set()

    def of(self, node: ast.expr | None) -> str:
        if node is None:
            return ""
        if isinstance(node, ast.Constant):
            return node.value if isinstance(node.value, str) else ""
        if isinstance(node, ast.JoinedStr):
            # f-string 的常量片段仍是 SQL 文本;插值位置填占位符,不影响表名与锁子句。
            return "".join(
                part.value
                if isinstance(part, ast.Constant) and isinstance(part.value, str)
                else "?"
                for part in node.values
            )
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            return self.of(node.left) + self.of(node.right)
        if isinstance(node, ast.Name) and node.id not in self._resolving:
            assigned = self._assignments.get(node.id)
            if assigned is None:
                return ""
            self._resolving.add(node.id)
            try:
                return self.of(assigned)
            finally:
                self._resolving.discard(node.id)
        return ""


def _combined(parent: ast.AST | None, child: ast.expr) -> bool:
    """这条表达式是否已被父表达式拼进同一段文本(那就不再是独立的根)。"""
    if isinstance(parent, ast.JoinedStr):
        return True
    if isinstance(parent, ast.BinOp) and isinstance(parent.op, ast.Add):
        return child is parent.left or child is parent.right
    return False


def _text_roots(tree: ast.Module) -> list[ast.expr]:
    """返回所有「拼出文本」的表达式根节点,不含已被父表达式拼进去的子表达式。"""
    parents: dict[int, ast.AST] = {}
    for holder in ast.walk(tree):
        for child in ast.iter_child_nodes(holder):
            parents[id(child)] = holder
    roots: list[ast.expr] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.expr):
            continue
        owner = parents.get(id(node))
        if isinstance(owner, ast.expr) and _combined(owner, node):
            continue
        roots.append(node)
    return roots


def scan(backend: Path) -> tuple[set[str], dict[str, int]]:
    """返回(草稿行锁子句强度集合, 每模块锁语句条数)。"""
    verbs: set[str] = set()
    sites: dict[str, int] = {}
    for path in sorted(backend.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        text = _Text(tree)
        relative = path.relative_to(backend).as_posix()
        for root in _text_roots(tree):
            sql = text.of(root)
            if DRAFT_TABLE not in sql:
                continue
            clauses = [" ".join(match.group(0).split()).upper() for match in _LOCK.finditer(sql)]
            if not clauses:
                continue
            sites[relative] = sites.get(relative, 0) + 1
            verbs.update(clauses)
    return verbs, sites


def check(backend: Path) -> None:
    """主张本体:一条锁强度 + 每条锁语句都在。"""
    verbs, sites = scan(backend)
    if verbs != {"FOR UPDATE"}:
        raise AssertionError(
            "the draft row must be locked with one strength; found " + repr(sorted(verbs))
        )
    if sites != EXPECTED_SITES:
        raise AssertionError(
            "every module that locks the draft row must keep its lock statements; found "
            + repr(sites)
        )


class DraftRowLockStrengthTests(unittest.TestCase):
    def test_the_draft_row_has_exactly_one_lock_strength(self) -> None:
        check(BACKEND)

    def test_the_locking_modules_are_the_ones_the_path_names(self) -> None:
        """条数表不能漂移成空集:四个模块都必须真的出现在扫描结果里。"""
        _verbs, sites = scan(BACKEND)
        self.assertEqual(set(sites), set(EXPECTED_SITES))
        self.assertGreater(sum(sites.values()), 1)


class InjectedDraftRowLockTests(unittest.TestCase):
    """注入副本:主张被破坏时上面的断言真的会红。"""

    def setUp(self) -> None:
        self.directory = Path(tempfile.mkdtemp(prefix="draft-row-lock-"))
        self.addCleanup(shutil.rmtree, self.directory, True)
        self.backend = self.directory / "speechnexus_backend"
        for relative in EXPECTED_SITES:
            target = self.backend / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(BACKEND / relative, target)
        self.admission = self.backend / "speech/infrastructure/public_job_admission.py"

    def _rewrite(self, old: str, new: str) -> None:
        source = self.admission.read_text(encoding="utf-8")
        self.assertIn(old, source)
        self.admission.write_text(source.replace(old, new, 1), encoding="utf-8")

    def test_the_copied_tree_passes_unchanged(self) -> None:
        check(self.backend)

    def test_one_site_back_to_for_share_fails_the_single_strength_assertion(self) -> None:
        self._rewrite(
            "SELECT 1 FROM speech_voice_drafts WHERE draft_id=? FOR UPDATE",
            "SELECT 1 FROM speech_voice_drafts WHERE draft_id=? FOR SHARE",
        )
        verbs, sites = scan(self.backend)
        self.assertEqual(verbs, {"FOR SHARE", "FOR UPDATE"})
        self.assertEqual(sites, EXPECTED_SITES)
        with self.assertRaisesRegex(AssertionError, "one strength"):
            check(self.backend)

    def test_removing_the_lock_fails_the_site_count_assertion(self) -> None:
        self._rewrite(
            '    cursor.execute("SELECT 1 FROM speech_voice_drafts WHERE draft_id=? FOR UPDATE", '
            "(draft_id,))\n",
            "",
        )
        verbs, sites = scan(self.backend)
        # 强度仍然只有一个:删掉锁不会把强度弄混,只有条数变少。
        self.assertEqual(verbs, {"FOR UPDATE"})
        self.assertEqual(sites["speech/infrastructure/public_job_admission.py"], 1)
        with self.assertRaisesRegex(AssertionError, "keep its lock statements"):
            check(self.backend)


if __name__ == "__main__":
    unittest.main()
