"""canonical 逻辑 schema:唯一逻辑定义与 fingerprint 计算.

物理 DDL 可以为目标库调整等价类型拼写、索引与约束语法、部署属性;但表、列、主键、唯一键、
外键、NULL 性、CHECK 语义和索引覆盖必须与这里的逻辑定义一致。`fingerprint` 是这些逻辑事实的
SHA-256:物理 DDL profile 变了而逻辑没变,指纹不变;逻辑变了而没提升 revision,指纹和部署
种子会对不上,guard 变红。

逻辑定义只描述共同类型:`VARCHAR(n)`、`CHAR(n)`、`DECIMAL(38,0)`、`TIMESTAMP(6)`。
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

__all__ = [
    "ALLOWED_TYPES",
    "LogicalColumn",
    "LogicalForeignKey",
    "LogicalIndex",
    "LogicalSchema",
    "LogicalTable",
    "SchemaDefinitionError",
    "fingerprint",
    "load_logical_schema",
]

IDENTIFIER = re.compile(r"^[a-z][a-z0-9_]{0,29}$")
REVISION = re.compile(r"^\d{4}$")
#: 共同类型。`DECIMAL` 只允许 `(38,0)`;时间只允许 `TIMESTAMP(6)`;文本长度不超过
#: Oracle 的 4000 字节上限,使字节语义与字符语义收敛到同一更严格约束。
MAX_TEXT_LENGTH = 4000
ALLOWED_TYPES = re.compile(r"^(?:varchar|char)\((\d{1,4})\)$|^decimal\(38,0\)$|^timestamp\(6\)$")


class SchemaDefinitionError(ValueError):
    """逻辑 schema 定义本身不合法(标识符、类型、引用或 revision)。"""


@dataclass(frozen=True, slots=True)
class LogicalColumn:
    name: str
    type: str
    nullable: bool


@dataclass(frozen=True, slots=True)
class LogicalForeignKey:
    columns: tuple[str, ...]
    references_table: str
    references_columns: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class LogicalIndex:
    name: str
    columns: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class LogicalTable:
    name: str
    columns: tuple[LogicalColumn, ...]
    primary_key: tuple[str, ...]
    uniques: tuple[tuple[str, ...], ...]
    foreign_keys: tuple[LogicalForeignKey, ...]
    checks: tuple[str, ...]
    indexes: tuple[LogicalIndex, ...]

    def column(self, name: str) -> LogicalColumn | None:
        for column in self.columns:
            if column.name == name:
                return column
        return None


@dataclass(frozen=True, slots=True)
class LogicalSchema:
    revision: str
    tables: tuple[LogicalTable, ...]

    def table(self, name: str) -> LogicalTable | None:
        for table in self.tables:
            if table.name == name:
                return table
        return None


def _require_identifier(value: object, *, field: str) -> str:
    if not isinstance(value, str) or not IDENTIFIER.match(value):
        raise SchemaDefinitionError(f"{field} must be an ASCII lowercase identifier <= 30 chars")
    return value


def _require_str(value: object, *, field: str) -> str:
    if not isinstance(value, str):
        raise SchemaDefinitionError(f"{field} must be a string")
    return value


def _require_columns(value: object, *, table: str, field: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise SchemaDefinitionError(f"{table}.{field} must be a non-empty list")
    return tuple(_require_identifier(item, field=f"{table}.{field}") for item in value)


def load_logical_schema(path: Path) -> LogicalSchema:
    """读取并校验逻辑定义;未知字段直接拒绝,避免「写了但没被检查」的差异。"""

    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise SchemaDefinitionError("logical schema must be a JSON object")
    revision = _require_str(payload.get("revision"), field="revision")
    if not REVISION.match(revision):
        raise SchemaDefinitionError("revision must be four digits")
    raw_tables = payload.get("tables")
    if not isinstance(raw_tables, list) or not raw_tables:
        raise SchemaDefinitionError("logical schema needs at least one table")

    tables: list[LogicalTable] = []
    for raw in raw_tables:
        if not isinstance(raw, dict):
            raise SchemaDefinitionError("each table must be an object")
        table = _parse_table(raw)
        if any(existing.name == table.name for existing in tables):
            raise SchemaDefinitionError(f"duplicate table {table.name}")
        tables.append(table)

    schema = LogicalSchema(revision, tuple(tables))
    _validate_references(schema)
    return schema


def _parse_table(raw: dict[str, Any]) -> LogicalTable:
    name = _require_identifier(raw.get("name"), field="table name")
    raw_columns = raw.get("columns")
    if not isinstance(raw_columns, list) or not raw_columns:
        raise SchemaDefinitionError(f"{name}.columns must be a non-empty list")

    columns: list[LogicalColumn] = []
    for raw_column in raw_columns:
        if not isinstance(raw_column, dict):
            raise SchemaDefinitionError(f"{name} column must be an object")
        column_name = _require_identifier(raw_column.get("name"), field=f"{name} column")
        column_type = _require_str(raw_column.get("type"), field=f"{name}.{column_name} type")
        if not ALLOWED_TYPES.match(column_type):
            raise SchemaDefinitionError(f"{name}.{column_name} has a non-canonical type")
        if column_type.startswith(("varchar(", "char(")):
            length = int(column_type[column_type.index("(") + 1 : -1])
            if not 1 <= length <= MAX_TEXT_LENGTH:
                raise SchemaDefinitionError(
                    f"{name}.{column_name} length {length} is outside 1..{MAX_TEXT_LENGTH}"
                )
        nullable = raw_column.get("nullable")
        if not isinstance(nullable, bool):
            raise SchemaDefinitionError(f"{name}.{column_name} needs an explicit nullable flag")
        if any(existing.name == column_name for existing in columns):
            raise SchemaDefinitionError(f"{name} has a duplicate column {column_name}")
        columns.append(LogicalColumn(column_name, column_type, nullable))

    raw_uniques = raw.get("uniques", [])
    if not isinstance(raw_uniques, list):
        raise SchemaDefinitionError(f"{name}.uniques must be a list")
    uniques = tuple(_require_columns(item, table=name, field="uniques") for item in raw_uniques)

    raw_foreign_keys = raw.get("foreign_keys", [])
    if not isinstance(raw_foreign_keys, list):
        raise SchemaDefinitionError(f"{name}.foreign_keys must be a list")
    foreign_keys: list[LogicalForeignKey] = []
    for raw_foreign_key in raw_foreign_keys:
        if not isinstance(raw_foreign_key, dict):
            raise SchemaDefinitionError(f"{name} foreign key must be an object")
        references = raw_foreign_key.get("references")
        if not isinstance(references, dict):
            raise SchemaDefinitionError(f"{name} foreign key needs references")
        foreign_keys.append(
            LogicalForeignKey(
                columns=_require_columns(
                    raw_foreign_key.get("columns"), table=name, field="foreign_keys.columns"
                ),
                references_table=_require_identifier(
                    references.get("table"), field="foreign key table"
                ),
                references_columns=_require_columns(
                    references.get("columns"), table=name, field="foreign_keys.references"
                ),
            )
        )

    raw_checks = raw.get("checks", [])
    if not isinstance(raw_checks, list):
        raise SchemaDefinitionError(f"{name}.checks must be a list")
    checks = tuple(_require_str(item, field=f"{name} check") for item in raw_checks)

    raw_indexes = raw.get("indexes", [])
    if not isinstance(raw_indexes, list):
        raise SchemaDefinitionError(f"{name}.indexes must be a list")
    indexes: list[LogicalIndex] = []
    for raw_index in raw_indexes:
        if not isinstance(raw_index, dict):
            raise SchemaDefinitionError(f"{name} index must be an object")
        indexes.append(
            LogicalIndex(
                name=_require_identifier(raw_index.get("name"), field="index name"),
                columns=_require_columns(
                    raw_index.get("columns"), table=name, field="indexes.columns"
                ),
            )
        )

    table = LogicalTable(
        name=name,
        columns=tuple(columns),
        primary_key=(
            _require_columns(raw.get("primary_key"), table=name, field="primary_key")
            if raw.get("primary_key") is not None
            else ()
        ),
        uniques=uniques,
        foreign_keys=tuple(foreign_keys),
        checks=checks,
        indexes=tuple(indexes),
    )
    _validate_table_columns(table)
    return table


def _validate_table_columns(table: LogicalTable) -> None:
    known = {column.name for column in table.columns}
    references: list[tuple[str, tuple[str, ...]]] = [
        ("primary_key", table.primary_key),
        ("uniques", tuple(column for unique in table.uniques for column in unique)),
        ("foreign_keys", tuple(column for fk in table.foreign_keys for column in fk.columns)),
        ("indexes", tuple(column for index in table.indexes for column in index.columns)),
    ]
    for field, names in references:
        unknown = [name for name in names if name not in known]
        if unknown:
            raise SchemaDefinitionError(f"{table.name}.{field} references unknown {unknown}")


def _validate_references(schema: LogicalSchema) -> None:
    tables = {table.name: table for table in schema.tables}
    for table in schema.tables:
        for foreign_key in table.foreign_keys:
            target = tables.get(foreign_key.references_table)
            if target is None:
                raise SchemaDefinitionError(
                    f"{table.name} references unknown table {foreign_key.references_table}"
                )
            target_columns = {column.name for column in target.columns}
            unknown = [
                name for name in foreign_key.references_columns if name not in target_columns
            ]
            if unknown:
                raise SchemaDefinitionError(
                    f"{table.name} foreign key references unknown columns {unknown}"
                )


def canonical_json(schema: LogicalSchema) -> str:
    """指纹输入:稳定、与机器无关、键序固定。"""

    payload = {
        "revision": schema.revision,
        "tables": [
            {
                "name": table.name,
                "columns": [
                    {"name": column.name, "type": column.type, "nullable": column.nullable}
                    for column in table.columns
                ],
                "primary_key": list(table.primary_key),
                "uniques": [list(unique) for unique in table.uniques],
                "foreign_keys": [
                    {
                        "columns": list(foreign_key.columns),
                        "references": {
                            "table": foreign_key.references_table,
                            "columns": list(foreign_key.references_columns),
                        },
                    }
                    for foreign_key in table.foreign_keys
                ],
                "checks": list(table.checks),
                "indexes": [
                    {"name": index.name, "columns": list(index.columns)} for index in table.indexes
                ],
            }
            for table in schema.tables
        ],
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def fingerprint(schema: LogicalSchema) -> str:
    """逻辑 schema 的 SHA-256 十六进制指纹,写入 `schema_meta.schema_fingerprint`。"""

    return hashlib.sha256(canonical_json(schema).encode("utf-8")).hexdigest()
