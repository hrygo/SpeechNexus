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


#: 制品文本的判据:DDL 建对象,或部署期授权文本。名字后缀不作数 —— `_V1`、
#: `_GRANTS` 这类常量一样是制品,只有内容能说明它是什么。
_DDL_MARKERS = ("CREATE TABLE", "CREATE SCHEMA", "CREATE INDEX", "ALTER TABLE")
#: 授权文本也只在部署期执行,但它的消费者是 `coordination_grants` 一类的装配模块,
#: 所以常量层面的判据放宽到 GRANT;函数层面只按建对象语句判定。
_ARTIFACT_MARKERS = (*_DDL_MARKERS, "GRANT ")


def _has_artifact_text(node: ast.AST) -> bool:
    return any(
        isinstance(inner, ast.Constant)
        and isinstance(inner.value, str)
        and any(marker in inner.value for marker in _DDL_MARKERS)
        for inner in ast.walk(node)
    )


def _artifact_symbols(tree: ast.Module) -> list[str]:
    symbols: list[str] = []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and _has_artifact_text(node):
            # 制品文本内联在函数里(`sql.SQL("""CREATE TABLE ...""")`)时,函数本身就是制品。
            symbols.append(node.name)
    for node in tree.body:
        targets: list[ast.expr]
        if isinstance(node, ast.AnnAssign):
            targets = [node.target]
            value = node.value
        elif isinstance(node, ast.Assign):
            targets, value = node.targets, node.value
        else:
            continue
        # 名字以 `_DDL` 结尾的常量就是制品(文本可能是字面量,也可能是拼接结果);
        # 其余名字只有内容里出现建对象/授权语句时才算。
        by_name = any(
            isinstance(target, ast.Name) and target.id.endswith("_DDL") for target in targets
        )
        by_text = (
            isinstance(value, ast.Constant)
            and isinstance(value.value, str)
            and any(marker in value.value for marker in _ARTIFACT_MARKERS)
        )
        if not (by_name or by_text):
            continue
        symbols.extend(
            target.id
            for target in targets
            if isinstance(target, ast.Name) and not target.id.startswith("__")
        )
    return symbols


#: 允许消费制品文本的装配点:迁移树(已在 `_product_trees` 里排除)、workbench 的
#: 一次性 owner 迁移步,以及声明清单内部彼此的复用。业务运行时代码不在其中。
_ALLOWED_OWNERS = ("backend/src/speechnexus_backend/workbench/migrate.py",)


def _non_migration_consumers(
    symbol: str, definition: str, trees: dict[str, ast.Module], declared: list[str]
) -> list[str]:
    consumers: list[str] = []
    allowed = {str(ROOT / item) for item in (*declared, *_ALLOWED_OWNERS)}
    for path, tree in trees.items():
        if path == definition or path in allowed:
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
            symbols = _artifact_symbols(ast.parse(path.read_text(encoding="utf-8")))
            if not symbols:
                empty.append(definition)
                continue
            consumers = {
                symbol: _non_migration_consumers(symbol, str(path), trees, declared)
                for symbol in symbols
            }
            hits = {symbol: paths for symbol, paths in consumers.items() if paths}
            if hits:
                offenders[definition] = hits
        self.assertEqual(empty, [], f"declaration without an artifact constant: {empty}")
        self.assertEqual(offenders, {}, f"declared artifact has a business consumer: {offenders}")
