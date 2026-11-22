#!/usr/bin/env python3
"""逻辑 schema guard:物理 DDL profile 必须等价于 canonical 逻辑定义.

判据(#1094 验收标准):物理 DDL 可以调整等价类型拼写、索引 / 约束语法和部署属性,但删除列、
键、约束,改变 NULL 性、唯一性或索引覆盖时必须变红;`schema_meta` 种子里的 revision 与
fingerprint 必须等于当前逻辑定义。

物理 DDL profile 就是仓库里的 `migrations/*.sql`(`0001_*draft.sql` 除外:它是被取代的迁移输入)。
逻辑定义只有一份:`packages/portable-sql/schema/logical.json`,指纹函数来自
`speechnexus_portable_sql.logical_schema`,部署作业与 CI 用的是同一个。

只读:不改任何文件。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
PACKAGE_SOURCE = REPO_ROOT / "packages/portable-sql/src"
LOGICAL = REPO_ROOT / "packages/portable-sql/schema/logical.json"

if str(PACKAGE_SOURCE) not in sys.path:
    sys.path.insert(0, str(PACKAGE_SOURCE))

from speechnexus_portable_sql.logical_schema import (  # noqa: E402 - 需先补 sys.path
    LogicalSchema,
    LogicalTable,
    SchemaDefinitionError,
    fingerprint,
    load_logical_schema,
)

#: 被取代的协议草稿:它按 PostgreSQL 方言写历史 schema,由 #1100/#1102 替换。
DDL_SUFFIX_ALLOWLIST = ("0001_cluster_coordination_draft.sql",)

#: 等价物理类型拼写。只允许改变写法,不允许改变精度或语义。
EQUIVALENT_TYPES = {
    "varchar": "varchar",
    "character varying": "varchar",
    "char": "char",
    "character": "char",
    "decimal": "decimal",
    "numeric": "decimal",
    "number": "decimal",
    "timestamp": "timestamp",
    "datetime": "timestamp",
    "datetime2": "timestamp",
}

TYPE_PATTERN = re.compile(r"^([a-z ]+?)\s*\(([\d, ]+)\)$")
COLUMN_PATTERN = re.compile(
    r"^(?P<name>[a-z][a-z0-9_]*)\s+(?P<type>[a-z0-9 ]+\([\d, ]+\))(?P<rest>.*)$", re.IGNORECASE
)
TABLE_PATTERN = re.compile(
    r"^CREATE\s+TABLE\s+(?P<name>[a-z][a-z0-9_]*)\s*\((?P<body>.*)\)$", re.IGNORECASE | re.DOTALL
)
INDEX_PATTERN = re.compile(
    r"^CREATE\s+(?P<unique>UNIQUE\s+)?INDEX\s+(?P<name>[a-z][a-z0-9_]*)\s+ON\s+"
    r"(?P<table>[a-z][a-z0-9_]*)\s*\((?P<columns>[^)]*)\)$",
    re.IGNORECASE | re.DOTALL,
)
SEED_CLEAR_PATTERN = re.compile(r"^DELETE\s+FROM\s+schema_meta\b", re.IGNORECASE)
SEED_PATTERN = re.compile(
    r"^INSERT\s+INTO\s+schema_meta\s*\([^)]*\)\s*VALUES\s*\((?P<values>.*)\)$",
    re.IGNORECASE | re.DOTALL,
)


@dataclass
class PhysicalColumn:
    name: str
    type: str
    nullable: bool


@dataclass
class PhysicalIndex:
    name: str
    table: str
    columns: tuple[str, ...]
    unique: bool


@dataclass
class PhysicalTable:
    name: str
    columns: list[PhysicalColumn] = field(default_factory=list)
    primary_key: tuple[str, ...] = ()
    uniques: list[tuple[str, ...]] = field(default_factory=list)
    foreign_keys: list[tuple[tuple[str, ...], str, tuple[str, ...]]] = field(default_factory=list)
    checks: list[str] = field(default_factory=list)
    indexes: list[PhysicalIndex] = field(default_factory=list)


@dataclass
class PhysicalSchema:
    tables: dict[str, PhysicalTable] = field(default_factory=dict)
    seed: tuple[str, str] | None = None


class DdlError(ValueError):
    """DDL 无法解析:guard 失败关闭,不猜。"""


def _strip_comments(source: str) -> str:
    return re.sub(r"--[^\n]*", " ", source)


def _split_statements(source: str) -> list[str]:
    statements: list[str] = []
    depth = 0
    current: list[str] = []
    for character in source:
        if character == "(":
            depth += 1
        elif character == ")":
            depth = max(0, depth - 1)
        if character == ";" and depth == 0:
            statement = "".join(current).strip()
            if statement:
                statements.append(statement)
            current = []
            continue
        current.append(character)
    tail = "".join(current).strip()
    if tail:
        statements.append(tail)
    return statements


def _split_top_level(body: str) -> list[str]:
    parts: list[str] = []
    depth = 0
    current: list[str] = []
    for character in body:
        if character == "(":
            depth += 1
        elif character == ")":
            depth -= 1
        if character == "," and depth == 0:
            parts.append("".join(current).strip())
            current = []
            continue
        current.append(character)
    tail = "".join(current).strip()
    if tail:
        parts.append(tail)
    return parts


def _columns_list(text: str) -> tuple[str, ...]:
    return tuple(item.strip().strip('"').lower() for item in text.split(",") if item.strip())


def _normalize_type(raw: str) -> str:
    match = TYPE_PATTERN.match(" ".join(raw.lower().split()))
    if match is None:
        raise DdlError(f"unqualified physical type {raw!r}")
    base = EQUIVALENT_TYPES.get(match.group(1).strip())
    if base is None:
        raise DdlError(f"type {raw!r} is not an allowlisted equivalent spelling")
    arguments = ",".join(part.strip() for part in match.group(2).split(","))
    return f"{base}({arguments})"


def parse_ddl(source: str) -> PhysicalSchema:
    """解析共同 DDL 子集;无法识别的 DDL 直接失败,不静默跳过。"""

    schema = PhysicalSchema()
    for statement in _split_statements(_strip_comments(source)):
        table_match = TABLE_PATTERN.match(statement)
        if table_match is not None:
            table = _parse_table(table_match.group("name").lower(), table_match.group("body"))
            if table.name in schema.tables:
                raise DdlError(f"duplicate table {table.name}")
            schema.tables[table.name] = table
            continue
        index_match = INDEX_PATTERN.match(statement)
        if index_match is not None:
            table_name = index_match.group("table").lower()
            index = PhysicalIndex(
                name=index_match.group("name").lower(),
                table=table_name,
                columns=_columns_list(index_match.group("columns")),
                unique=bool(index_match.group("unique")),
            )
            if table_name not in schema.tables:
                raise DdlError(f"index {index.name} targets unknown table {table_name}")
            schema.tables[table_name].indexes.append(index)
            continue
        if SEED_CLEAR_PATTERN.match(statement):
            # 每个 revision 重新盖章:清掉上一条种子,由本 revision 的 INSERT 决定权威身份。
            schema.seed = None
            continue
        seed_match = SEED_PATTERN.match(statement)
        if seed_match is not None:
            values = [
                value.strip().strip("'") for value in _split_top_level(seed_match.group("values"))
            ]
            if len(values) < 3 or values[0] != "portable":
                raise DdlError("schema_meta seed must start with the 'portable' row")
            schema.seed = (values[1], values[2])
            continue
        if re.match(r"^ALTER\s+TABLE\b", statement, re.IGNORECASE):
            raise DdlError(
                "canonical schema changes use a new revision revision file, not ALTER TABLE"
            )
        if re.match(r"^CREATE\s+", statement, re.IGNORECASE):
            raise DdlError(f"unsupported DDL object: {statement.split()[1].upper()}")
    return schema


def _parse_table(name: str, body: str) -> PhysicalTable:
    table = PhysicalTable(name=name)
    for part in _split_top_level(body):
        upper = part.upper()
        if upper.startswith("PRIMARY KEY"):
            table.primary_key = _columns_list(part[part.index("(") + 1 : part.rindex(")")])
            continue
        if upper.startswith("UNIQUE"):
            table.uniques.append(_columns_list(part[part.index("(") + 1 : part.rindex(")")]))
            continue
        if upper.startswith("FOREIGN KEY"):
            references = re.search(
                r"REFERENCES\s+([a-z][a-z0-9_]*)\s*\(([^)]*)\)", part, re.IGNORECASE
            )
            if references is None:
                raise DdlError(f"{name}: foreign key without REFERENCES")
            columns = _columns_list(part[part.index("(") + 1 : part.index(")")])
            table.foreign_keys.append(
                (
                    columns,
                    references.group(1).lower(),
                    _columns_list(references.group(2)),
                )
            )
            continue
        if upper.startswith("CHECK"):
            table.checks.append(_normalize_check(part[part.index("(") + 1 : part.rindex(")")]))
            continue
        if upper.startswith("CONSTRAINT"):
            raise DdlError(f"{name}: named constraints are not part of the common DDL subset")
        column_match = COLUMN_PATTERN.match(part)
        if column_match is None:
            raise DdlError(f"{name}: cannot parse column definition {part!r}")
        rest = " ".join(column_match.group("rest").split()).upper()
        table.columns.append(
            PhysicalColumn(
                name=column_match.group("name").lower(),
                type=_normalize_type(column_match.group("type")),
                nullable="NOT NULL" not in rest,
            )
        )
    return table


def _normalize_check(text: str) -> str:
    return " ".join(text.upper().split())


def _logical_indexes(table: LogicalTable) -> set[tuple[tuple[str, ...], bool]]:
    return {(index.columns, False) for index in table.indexes}


def compare(logical: LogicalSchema, physical: PhysicalSchema) -> list[str]:
    """返回逻辑 / 物理差异;空列表表示等价。"""

    problems: list[str] = []
    physical_tables = set(physical.tables)
    logical_tables = {table.name for table in logical.tables}

    for missing in sorted(logical_tables - physical_tables):
        problems.append(f"missing-table {missing}")
    for extra in sorted(physical_tables - logical_tables):
        problems.append(f"extra-table {extra}")

    for table in logical.tables:
        parsed = physical.tables.get(table.name)
        if parsed is None:
            continue
        problems.extend(_compare_table(table, parsed))

    if physical.seed is None:
        problems.append("missing-seed schema_meta row")
    else:
        revision, seeded = physical.seed
        if revision != logical.revision:
            problems.append(f"seed-revision {revision} != logical {logical.revision}")
        current = fingerprint(logical)
        if seeded != current:
            problems.append(f"seed-fingerprint {seeded[:12]} != logical {current[:12]}")

    return problems


def _compare_table(logical: LogicalTable, parsed: PhysicalTable) -> list[str]:
    problems: list[str] = []
    physical_columns = {column.name: column for column in parsed.columns}
    logical_columns = {column.name: column for column in logical.columns}

    for name in sorted(set(logical_columns) - set(physical_columns)):
        problems.append(f"{logical.name}: missing-column {name}")
    for name in sorted(set(physical_columns) - set(logical_columns)):
        problems.append(f"{logical.name}: extra-column {name}")

    for name in sorted(set(physical_columns) & set(logical_columns)):
        expected = logical_columns[name]
        actual = physical_columns[name]
        if actual.type != expected.type:
            problems.append(f"{logical.name}.{name}: type {actual.type} != {expected.type}")
        if actual.nullable != expected.nullable:
            problems.append(
                f"{logical.name}.{name}: nullable {actual.nullable} != {expected.nullable}"
            )

    if parsed.primary_key != logical.primary_key:
        problems.append(
            f"{logical.name}: primary key {parsed.primary_key} != {logical.primary_key}"
        )
    if set(parsed.uniques) != set(logical.uniques):
        problems.append(f"{logical.name}: unique keys {sorted(parsed.uniques)} differ")
    if set(parsed.foreign_keys) != {
        (fk.columns, fk.references_table, fk.references_columns) for fk in logical.foreign_keys
    }:
        problems.append(f"{logical.name}: foreign keys differ")
    if set(parsed.checks) != {_normalize_check(check) for check in logical.checks}:
        problems.append(f"{logical.name}: check constraints differ")
    if {(index.columns, False) for index in parsed.indexes} != _logical_indexes(logical):
        problems.append(f"{logical.name}: index coverage differs")

    return problems


def schema_files(root: Path) -> list[Path]:
    return sorted(
        path
        for path in (root / "migrations").glob("*.sql")
        if path.name not in DDL_SUFFIX_ALLOWLIST
    )


def render_ddl(schema: LogicalSchema) -> str:
    """从逻辑定义渲染 canonical DDL;selftest 用同一份定义,夹具不会漂移。"""

    chunks: list[str] = []
    for table in schema.tables:
        lines = [f"CREATE TABLE {table.name} ("]
        for column in table.columns:
            suffix = "" if column.nullable else " NOT NULL"
            lines.append(f"    {column.name} {column.type.upper()}{suffix},")
        if table.primary_key:
            lines.append(f"    PRIMARY KEY ({', '.join(table.primary_key)}),")
        for unique in table.uniques:
            lines.append(f"    UNIQUE ({', '.join(unique)}),")
        for foreign_key in table.foreign_keys:
            lines.append(
                f"    FOREIGN KEY ({', '.join(foreign_key.columns)}) REFERENCES "
                f"{foreign_key.references_table} ({', '.join(foreign_key.references_columns)}),"
            )
        last = lines[-1]
        lines[-1] = last[:-1] if last.endswith(",") else last
        lines.append(");")
        chunks.append("\n".join(lines))
        for index in table.indexes:
            chunks.append(
                f"CREATE INDEX {index.name} ON {table.name} ({', '.join(index.columns)});"
            )
    chunks.append("DELETE FROM schema_meta WHERE meta_id = 'portable';")
    chunks.append(
        "INSERT INTO schema_meta (meta_id, schema_rev, schema_fingerprint, authority_epoch)"
        f"\nVALUES ('portable', '{schema.revision}', '{fingerprint(schema)}', 1);"
    )
    return "\n\n".join(chunks) + "\n"


def selftest() -> list[str]:
    """注入式自检:删除列 / 键 / 约束、改 NULL 性、改索引覆盖、改种子都必须变红。"""

    problems: list[str] = []
    schema = load_logical_schema(LOGICAL)
    clean = render_ddl(schema)
    if compare(schema, parse_ddl(clean)):
        problems.append("clean DDL was rejected")

    mutations: tuple[tuple[str, str], ...] = (
        ("missing-column", clean.replace("    schema_rev VARCHAR(64) NOT NULL,\n", "")),
        ("nullable", clean.replace("schema_rev VARCHAR(64) NOT NULL", "schema_rev VARCHAR(64)")),
        (
            "primary-key",
            clean.replace("    PRIMARY KEY (meta_id)\n", ""),
        ),
        (
            "extra-column",
            clean.replace(
                "    PRIMARY KEY (meta_id)",
                "    extra_col VARCHAR(8) NOT NULL,\n    PRIMARY KEY (meta_id)",
            ),
        ),
        ("seed-fingerprint", clean.replace(fingerprint(schema), "0" * 64)),
        ("seed-revision", clean.replace(f"'{schema.revision}'", "'9999'")),
        (
            "index-coverage",
            clean + "\nCREATE INDEX idx_extra ON coordination_jobs (job_state);\n",
        ),
    )
    for label, source in mutations:
        try:
            parsed = parse_ddl(source)
        except DdlError as error:
            problems.append(f"{label}: mutation did not parse ({error})")
            continue
        if not compare(schema, parsed):
            problems.append(f"{label}: mutation was accepted")

    for label, source in (
        ("ALTER TABLE", clean + "\nALTER TABLE schema_meta ADD x VARCHAR(8);\n"),
        (
            "missing-table",
            clean.replace("CREATE TABLE coordination_outbox", "CREATE TABLE outbox_x"),
        ),
    ):
        try:
            parsed = parse_ddl(source)
        except DdlError:
            continue
        if not compare(schema, parsed):
            problems.append(f"{label}: mutation was accepted")

    return problems


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=REPO_ROOT)
    parser.add_argument("--logical", type=Path, default=LOGICAL)
    parser.add_argument("--selftest", action="store_true")
    args = parser.parse_args(argv)

    if args.selftest:
        problems = selftest()
        for problem in problems:
            print(f"SELFTEST FAIL: {problem}", file=sys.stderr)
        print("selftest OK: every logical drift is rejected" if not problems else "SELFTEST FAIL")
        return 1 if problems else 0

    try:
        logical = load_logical_schema(args.logical)
    except (SchemaDefinitionError, json.JSONDecodeError) as error:
        print(f"RESULT FAIL: logical schema is invalid: {error}", file=sys.stderr)
        return 1

    physical = PhysicalSchema()
    try:
        for path in schema_files(args.root):
            parsed = parse_ddl(path.read_text(encoding="utf-8"))
            physical.tables.update(parsed.tables)
            if parsed.seed is not None:
                physical.seed = parsed.seed
    except DdlError as error:
        print(f"RESULT FAIL: {error}", file=sys.stderr)
        return 1

    problems = compare(logical, physical)
    for problem in problems:
        print(f"PROBLEM {problem}", file=sys.stderr)
    if problems:
        print("RESULT FAIL: physical DDL profile is not the canonical logical schema")
        return 1
    print(
        f"tables={len(physical.tables)} revision={logical.revision} "
        f"fingerprint={fingerprint(logical)[:12]}"
    )
    print("RESULT OK: physical DDL equals the canonical logical schema")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
