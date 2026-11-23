"""删除账本插入:语句是字面量,列与表名对着 schema 常量核对."""

from __future__ import annotations

import re

from speechnexus_backend.speech.infrastructure import deletion_executions_postgres
from speechnexus_backend.speech.infrastructure.deletion_ledger_schema import (
    DELETION_LEDGER,
    DELETION_LEDGER_DDL,
    DELETION_LEDGER_INSERT_COLUMNS,
    DELETION_LEDGER_TARGETS_DDL,
)

_STATEMENT = deletion_executions_postgres._INSERT_LEDGER


def test_the_insert_names_every_column_once_in_schema_order() -> None:
    body = _STATEMENT.split("(", 1)[1].split(")", 1)[0]
    assert [column.strip() for column in body.split(",")] == list(DELETION_LEDGER_INSERT_COLUMNS)


def test_the_insert_targets_the_ledger_table_and_binds_one_marker_per_column() -> None:
    assert f"INSERT INTO {DELETION_LEDGER} " in _STATEMENT
    assert _STATEMENT.count("?") == len(DELETION_LEDGER_INSERT_COLUMNS)
    assert "%s" not in _STATEMENT


def test_every_inserted_column_exists_in_the_ddl() -> None:
    """列可以来自 CREATE TABLE,也可以来自后续 `ADD COLUMN` —— 两者都在同一份 DDL 元组里。"""

    ddl = "\n".join((*DELETION_LEDGER_DDL, *DELETION_LEDGER_TARGETS_DDL))
    declared = set(re.findall(r"^\s{4,}([a-z_]+)\s", ddl.split("(", 1)[1], re.M))
    declared |= set(re.findall(r"ADD COLUMN IF NOT EXISTS ([a-z_]+)", ddl))
    assert sorted(set(DELETION_LEDGER_INSERT_COLUMNS) - declared) == []
