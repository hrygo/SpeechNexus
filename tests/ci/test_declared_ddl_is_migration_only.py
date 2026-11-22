"""声明为 profile 制品的模块,其 DDL 文本必须真的只有迁移树在消费.

声明一个文件不是「把它从基线里划掉」,而是一个可以被复算的主张:该模块里的 `*_DDL`
在业务代码里没有任何消费者。这个测试就是复算器 —— 一旦有业务代码导入并执行这些 DDL,
声明就失效,这里立刻红。
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

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
            # 只看真正的使用点:导入语句把符号从别的模块带进来,那本身就是消费证据。
            if isinstance(node, (ast.ImportFrom,)) and any(
                alias.name == symbol for alias in node.names
            ):
                consumers.append(path)
                break
            if isinstance(node, ast.Name) and node.id == symbol and isinstance(node.ctx, ast.Load):
                consumers.append(path)
                break
    return consumers


@pytest.mark.parametrize("definition", _declared())
def test_declared_ddl_has_no_business_consumer(definition: str) -> None:
    path = ROOT / definition
    assert path.is_file(), f"declared profile artifact is missing: {definition}"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    symbols = _ddl_symbols(tree)
    assert symbols, f"{definition} declares no *_DDL symbol, so the claim is empty"
    trees = _product_trees()
    offenders = {symbol: _non_migration_consumers(symbol, str(path), trees) for symbol in symbols}
    offenders = {symbol: paths for symbol, paths in offenders.items() if paths}
    assert offenders == {}, offenders
