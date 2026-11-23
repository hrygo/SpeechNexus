"""canonical schema 的每一列都必须说清来源,未决项必须显式登记.

`migrations/canonical_source_map.json` 是数据搬迁(#1102)的清单,不是说明文档:它把
canonical 表的每一列映射到草案表的同名列(`identity`)、需要换算的列(`convert`)、需要
派生的列(`derive`)、建表时写死的值(`seed`),或一个写着 `reason` 的 `gap`。

这个测试复算三件事:

1. canonical DDL 里出现的列,映射里一个都不能少,也不能多——
   在 DDL 里加一列却不登记来源,这里立刻红;
2. `identity` / `convert` 声明的 `source` 必须真的出现在它声明的草案文件里——
   草案侧改名,这里立刻红;
3. `gap` 必须带 `reason`——把「还没想清楚」写成空白不算登记。

本文件属于 `tests/ci` 的免依赖守卫车道:只用标准库。
"""

from __future__ import annotations

import ast
import json
import re
import unittest
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MAP = ROOT / "migrations/canonical_source_map.json"

#: `CREATE TABLE name ( ... )` 里的一行列定义:首个 token 是列名,其余是类型与约束。
#: 约束行(`PRIMARY KEY`, `UNIQUE`, `FOREIGN KEY`, `CHECK`)不是列。
_CONSTRAINT_WORDS = frozenset({"PRIMARY", "UNIQUE", "FOREIGN", "CHECK", "CONSTRAINT"})
_CREATE = re.compile(r"CREATE TABLE\s+(\w+)\s*\(", re.IGNORECASE)
_IDENTIFIER = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)")
_KINDS = frozenset({"identity", "convert", "derive", "seed", "gap"})


def _columns(ddl_text: str) -> dict[str, list[str]]:
    """从建表语句里取出每张表的列名(不含约束行)。"""

    tables: dict[str, list[str]] = {}
    for match in _CREATE.finditer(ddl_text):
        table = match.group(1)
        depth, index = 1, match.end()
        while index < len(ddl_text) and depth:
            char = ddl_text[index]
            if char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
            index += 1
        body = ddl_text[match.end() : index - 1]
        columns: list[str] = []
        for line in body.splitlines():
            stripped = line.strip().rstrip(",")
            if not stripped or stripped.startswith("--"):
                continue
            token = _IDENTIFIER.match(stripped)
            if token is None or token.group(1).upper() in _CONSTRAINT_WORDS:
                continue
            columns.append(token.group(1))
        tables[table] = columns
    return tables


@dataclass(frozen=True)
class Column:
    kind: str
    source: str | None
    rule: str | None
    reason: str | None


@dataclass(frozen=True)
class Entry:
    canonical: str
    anchor: dict[str, str] | None
    columns: dict[str, Column]


def _mapping() -> tuple[str, dict[str, Entry]]:
    payload = json.loads(MAP.read_text(encoding="utf-8"))
    entries: dict[str, Entry] = {}
    for table, raw in payload["tables"].items():
        entries[table] = Entry(
            canonical=raw["canonical"],
            anchor=raw.get("anchor"),
            columns={
                name: Column(
                    kind=spec["kind"],
                    source=spec.get("source"),
                    rule=spec.get("rule"),
                    reason=spec.get("reason"),
                )
                for name, spec in raw["columns"].items()
            },
        )
    return str(payload["revision"]), entries


def _symbol_text(path: str, symbol: str) -> str:
    """取出模块级常量 `symbol` 的文本(把它的字符串片段拼接起来)。"""

    tree = ast.parse((ROOT / path).read_text(encoding="utf-8"))
    for node in tree.body:
        targets: list[ast.expr]
        value: ast.expr | None
        if isinstance(node, ast.Assign):
            targets, value = list(node.targets), node.value
        elif isinstance(node, ast.AnnAssign):
            targets, value = [node.target], node.value
        else:
            continue
        if not any(isinstance(item, ast.Name) and item.id == symbol for item in targets):
            continue
        if value is None:
            raise AssertionError(f"{symbol} has no value")
        return "".join(
            part.value
            for part in ast.walk(value)
            if isinstance(part, ast.Constant) and isinstance(part.value, str)
        )
    raise AssertionError(f"{path} has no module constant named {symbol}")


def _declared_columns(anchor: dict[str, str]) -> set[str]:
    """锚点指向的列名集合.

    `symbol`:草案模块里的 DDL 常量 —— 只认那一段文本,别处的同名标识符不算来源。
    `insert`:语句里的列清单 —— 列名在那里是被逐条列出的。
    """

    if anchor["kind"] == "symbol":
        tables = _columns(_symbol_text(anchor["file"], anchor["symbol"]))
        if not tables:
            raise AssertionError(f"{anchor['symbol']} holds no CREATE TABLE text")
        return set(tables.get(anchor["draft_table"], []))
    text = (ROOT / anchor["file"]).read_text(encoding="utf-8")
    found: set[str] = set()
    for match in re.finditer(anchor["pattern"], text):
        found.update(_IDENTIFIER.findall(match.group(1)))
    if not found:
        raise AssertionError(f"anchored statement is gone from {anchor['file']}")
    return found


class CanonicalSourceMapTests(unittest.TestCase):
    def test_every_canonical_column_has_a_declared_source(self) -> None:
        _, tables = _mapping()
        missing: dict[str, list[str]] = {}
        extra: dict[str, list[str]] = {}
        for table, entry in tables.items():
            ddl = _columns((ROOT / entry.canonical).read_text(encoding="utf-8"))
            self.assertIn(table, ddl, f"{table} is not created by {entry.canonical}")
            declared = set(entry.columns)
            actual = set(ddl[table])
            if actual - declared:
                missing[table] = sorted(actual - declared)
            if declared - actual:
                extra[table] = sorted(declared - actual)
        self.assertEqual(missing, {}, f"canonical column without a declared source: {missing}")
        self.assertEqual(extra, {}, f"mapping names a column the DDL does not create: {extra}")

    def test_every_column_entry_names_a_known_kind(self) -> None:
        unknown: dict[str, list[str]] = {}
        for table, entry in _mapping()[1].items():
            for column, spec in entry.columns.items():
                if spec.kind not in _KINDS:
                    unknown.setdefault(table, []).append(column)
        self.assertEqual(unknown, {}, f"column with an unknown kind: {unknown}")

    def test_declared_sources_exist_in_the_draft_definition(self) -> None:
        absent: dict[str, list[str]] = {}
        for table, entry in _mapping()[1].items():
            if entry.anchor is None:
                continue
            available = _declared_columns(entry.anchor)
            for column, spec in entry.columns.items():
                if (
                    spec.kind in {"identity", "convert"}
                    and spec.source
                    and spec.source not in available
                ):
                    absent.setdefault(f"{table}.{column}", []).append(spec.source)
        self.assertEqual(absent, {}, f"declared source column does not exist: {absent}")

    def test_gaps_and_derivations_carry_their_reason(self) -> None:
        blank: list[str] = []
        for table, entry in _mapping()[1].items():
            for column, spec in entry.columns.items():
                if spec.kind in {"gap", "derive", "seed"} and not spec.reason and not spec.rule:
                    blank.append(f"{table}.{column}")
        self.assertEqual(blank, [], f"no reason given for an undecided column: {blank}")

    def test_the_map_covers_the_canonical_revision_it_claims(self) -> None:
        revision, tables = _mapping()
        for table, entry in tables.items():
            name = Path(entry.canonical).name
            self.assertTrue(
                name.startswith(revision),
                f"{table} is mapped against {name} but the map claims revision {revision}",
            )
