"""删除执行模块的读语句:每条都是字面量,列序对着 schema 常量核对."""

from __future__ import annotations

from speechnexus_backend.speech.infrastructure import deletion_executions_postgres as module

_LEDGER_COLUMNS = tuple(column.strip() for column in module._LEDGER_ROW_COLUMNS.split(","))
_PLAN_COLUMNS = tuple(column.strip() for column in module._PLAN_ROW_COLUMNS.split(","))

_LEDGER_SELECTS = {
    "_SELECT_LEDGER_BY_KEY",
    "_SELECT_LEDGER_BY_KEY_LOCKED",
    "_SELECT_LEDGER_BY_ID",
    "_SELECT_LEDGER_BY_PK",
    "_SELECT_LEDGER_BY_ID_LOCKED",
    "_SELECT_LEDGER_FOR_PLAN",
    "_SELECT_QUEUED_LEDGER",
    "_SELECT_LEDGER_CLAIM_STATE",
}


def _columns(statement: str) -> tuple[str, ...]:
    body = statement.split("SELECT ", 1)[1].split(" FROM ", 1)[0]
    return tuple(column.strip() for column in body.split(",") if column.strip())


def test_every_ledger_select_projects_the_whole_row_in_schema_order() -> None:
    for name in sorted(_LEDGER_SELECTS):
        statement = getattr(module, name)
        projected = _columns(statement)
        if name == "_SELECT_LEDGER_CLAIM_STATE":
            # 这一条额外投影一个派生列,列前缀必须与行形状一致。
            # 最后一个投影项是 claim_live 表达式,它以 `(` 开头而不是列名。
            projected = tuple(column for column in projected if not column.startswith("("))
        assert projected == _LEDGER_COLUMNS, name


def test_the_plan_select_projects_every_plan_column() -> None:
    assert _columns(module._SELECT_PLAN_BY_ID) == _PLAN_COLUMNS


def test_no_read_statement_is_built_by_interpolation() -> None:
    for name in sorted(_LEDGER_SELECTS | {"_SELECT_PLAN_BY_ID"}):
        statement = getattr(module, name)
        assert statement.startswith("SELECT "), name
        assert "{" not in statement and "%s" not in statement, name
        assert statement.count("speech_deletion_") == 1, name


def test_every_write_statement_is_literal_and_uses_the_canonical_marker() -> None:
    for name in (
        "_INSERT_LEDGER",
        "_CLAIM_PURGE",
        "_RELEASE_PURGE",
        "_SETTLE_LEDGER",
        "_DELETE_JOB_INTENT",
        "_TOMBSTONE_ARTIFACT",
    ):
        statement = getattr(module, name)
        assert "{" not in statement, name
        assert "%s" not in statement, name
        assert "RETURNING" not in statement.upper(), name
