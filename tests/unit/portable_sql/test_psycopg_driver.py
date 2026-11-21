"""驱动适配:qmark 翻译、字面量百分号转义与迁移期兼容."""

from __future__ import annotations

from speechnexus_portable_sql.psycopg_driver import qmark_to_pyformat


def test_qmark_placeholders_are_translated() -> None:
    assert qmark_to_pyformat("SELECT a FROM t WHERE b = ? AND c = ?", ("x", "y")) == (
        "SELECT a FROM t WHERE b = %s AND c = %s"
    )


def test_literal_percent_is_escaped_before_placeholders_are_inserted() -> None:
    assert qmark_to_pyformat("SELECT 1 FROM t WHERE name LIKE 'op%' AND id = ?", (1,)) == (
        "SELECT 1 FROM t WHERE name LIKE 'op%%' AND id = %s"
    )


def test_legacy_pyformat_statements_are_left_alone() -> None:
    sql = "SELECT 1 FROM t WHERE id = %s"
    assert qmark_to_pyformat(sql, (1,)) == sql
    named = "SELECT 1 FROM t WHERE id = %(id)s"
    assert qmark_to_pyformat(named, (1,)) == named


def test_statements_without_parameters_are_not_rewritten() -> None:
    sql = "SELECT 1 FROM t WHERE name LIKE 'op%'"
    assert qmark_to_pyformat(sql, None) == sql
