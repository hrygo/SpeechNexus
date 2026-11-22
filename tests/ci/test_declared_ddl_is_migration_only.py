"""声明为 profile 制品的模块,其制品文本必须真的只有迁移树在消费.

声明一个文件不是「把它从基线里划掉」,而是一个可以被复算的主张:该模块里的 `*_DDL`
在业务代码里没有任何消费者。这个测试就是复算器 —— 一旦有业务代码导入并执行这些文本,
声明就失效,这里立刻红。

本文件属于 `tests/ci` 的免依赖守卫车道:只用标准库,不用 pytest fixture 与 parametrize。
"""

from __future__ import annotations

import ast
import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PROFILES = ROOT / "scripts/dev-env/portable-sql-profiles.json"
BACKEND = ROOT / "backend/src"
MIGRATIONS = "/migrations/"


def _declared() -> list[str]:
    payload = json.loads(PROFILES.read_text(encoding="utf-8"))
    return list(payload["paths"])


def _product_trees() -> dict[str, ast.Module]:
    trees: dict[str, ast.Module] = {}
    for path in BACKEND.rglob("*.py"):
        text = str(path)
        if "__pycache__" in text or MIGRATIONS in text:
            continue
        trees[text] = ast.parse(path.read_text(encoding="utf-8"))
    return trees


def _ddl_symbols(tree: ast.Module) -> list[str]:
    symbols: list[str] = []
    for node in tree.body:
        if (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id.endswith("_DDL")
        ):
            symbols.append(node.target.id)
        elif isinstance(node, ast.Assign):
            symbols.extend(
                target.id
                for target in node.targets
                if isinstance(target, ast.Name) and target.id.endswith("_DDL")
            )
    return symbols


def _non_migration_consumers(
    symbol: str, definition: str, trees: dict[str, ast.Module]
) -> list[str]:
    consumers: list[str] = []
    for path, tree in trees.items():
        if path == definition:
            continue
        for node in ast.walk(tree):
            # 导入语句把符号带进来,本身就是消费证据。
            if isinstance(node, ast.ImportFrom) and any(
                alias.name == symbol for alias in node.names
            ):
                consumers.append(path)
                break
            if isinstance(node, ast.Name) and node.id == symbol and isinstance(node.ctx, ast.Load):
                consumers.append(path)
                break
    return consumers


class DeclaredArtifactTests(unittest.TestCase):
    """每条声明都必须能复算出「业务侧零消费者」."""

    def test_every_declared_artifact_is_migration_only(self) -> None:
        declared = _declared()
        self.assertTrue(declared, "no migration-only artifact is declared")
        trees = _product_trees()
        offenders: dict[str, dict[str, list[str]]] = {}
        empty: list[str] = []
        for definition in declared:
            path = ROOT / definition
            self.assertTrue(path.is_file(), f"declared profile artifact is missing: {definition}")
            symbols = _ddl_symbols(ast.parse(path.read_text(encoding="utf-8")))
            if not symbols:
                empty.append(definition)
                continue
            consumers = {
                symbol: _non_migration_consumers(symbol, str(path), trees) for symbol in symbols
            }
            hits = {symbol: paths for symbol, paths in consumers.items() if paths}
            if hits:
                offenders[definition] = hits
        self.assertEqual(empty, [], f"declaration without a *_DDL symbol: {empty}")
        self.assertEqual(offenders, {}, f"declared artifact has a business consumer: {offenders}")
