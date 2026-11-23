"""删除计划插入:语句是字面量,列与占位符对着 schema 常量核对.

原来这里用 `", ".join(...)` 拼列名与 `%s` 占位符 —— 受限方言不允许把列清单拼进语句,
也不允许 `%s`。改成字面量之后,「列顺序」这件事就有了两个事实源:语句文本与
`DELETION_PLAN_INSERT_COLUMNS`。这个用例把它们钉在一起,拼错一个列名就红。
"""

from __future__ import annotations

import re

from speechnexus_backend.speech.infrastructure import deletion_plans_postgres
from speechnexus_backend.speech.infrastructure.deletion_plan_schema import (
    DELETION_PLAN_DDL,
    DELETION_PLAN_INSERT_COLUMNS,
)

_STATEMENT = deletion_plans_postgres._INSERT_PLAN


def _statement_columns() -> list[str]:
    body = _STATEMENT.split("(", 1)[1].split(")", 1)[0]
    return [column.strip() for column in body.split(",")]


def test_the_insert_names_every_column_once_in_schema_order() -> None:
    assert _statement_columns() == list(DELETION_PLAN_INSERT_COLUMNS)


def test_the_insert_binds_one_marker_per_column() -> None:
    values = _STATEMENT.split("VALUES", 1)[1]
    assert values.count("?") == len(DELETION_PLAN_INSERT_COLUMNS)
    assert "%s" not in _STATEMENT


def test_the_insert_has_no_vendor_upsert_clause() -> None:
    assert "ON CONFLICT" not in _STATEMENT.upper()


def test_every_inserted_column_exists_in_the_ddl() -> None:
    ddl = next(chunk for chunk in DELETION_PLAN_DDL if "speech_deletion_plans" in chunk)
    body = ddl.split("(", 1)[1]
    declared = set(re.findall(r"^\s{4,}([a-z_]+)\s", body, re.M))
    unknown = sorted(set(DELETION_PLAN_INSERT_COLUMNS) - declared)
    assert unknown == [], f"DDL does not declare {unknown}"
