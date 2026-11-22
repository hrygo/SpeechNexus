"""`ListMarker`:一个 `?` 绑一列值,展开只发生在拥有标记语法的这一层。"""

from __future__ import annotations

import pytest

from speechnexus_portable_sql.contract import (
    ContractError,
    ListMarker,
    check_parameters,
    expand_lists,
)


def test_one_list_expands_in_place_and_keeps_parameter_order() -> None:
    sql, params = expand_lists(
        "SELECT id FROM t WHERE id IN (?) AND state = ? ORDER BY id", (ListMarker([1, 2, 3]), "ok")
    )
    assert sql == "SELECT id FROM t WHERE id IN (?, ?, ?) AND state = ? ORDER BY id"
    assert params == (1, 2, 3, "ok")


def test_two_lists_expand_in_their_own_positions() -> None:
    sql, params = expand_lists(
        "SELECT a FROM t WHERE a IN (?) AND b IN (?)", (ListMarker(["x"]), ListMarker(["y", "z"]))
    )
    assert sql == "SELECT a FROM t WHERE a IN (?) AND b IN (?, ?)"
    assert params == ("x", "y", "z")


def test_a_question_mark_inside_a_literal_is_not_a_placeholder() -> None:
    # 词法扫描决定位置:字面量里的 `?` 不参与展开,否则参数顺序会整体错位。
    sql, params = expand_lists(
        "SELECT a FROM t WHERE note = 'why?' AND a IN (?)", (ListMarker([7, 8]),)
    )
    assert sql == "SELECT a FROM t WHERE note = 'why?' AND a IN (?, ?)"
    assert params == (7, 8)


def test_an_empty_list_is_refused_rather_than_guessed() -> None:
    # 展开成 `IN ()` 不合语法,当成「匹配不到任何行」会让 `NOT IN` 静默返回空。
    with pytest.raises(ContractError, match="at least one value"):
        ListMarker([])


def test_a_list_of_values_still_passes_parameter_validation() -> None:
    # 展开可能晚于校验:标记本身要通过参数校验,里面的每个值也要。
    marked = ListMarker([1, 2])
    assert check_parameters((marked,)) == (marked,)
    with pytest.raises(ContractError, match="not bool"):
        check_parameters((ListMarker([1, True]),))


def test_expansion_without_a_list_returns_the_statement_untouched() -> None:
    sql, params = expand_lists("SELECT a FROM t WHERE a = ?", (5,))
    assert sql == "SELECT a FROM t WHERE a = ?"
    assert params == (5,)


def test_a_statement_without_parameters_keeps_none_so_percent_stays_literal() -> None:
    # psycopg 只在 params 为 None 时跳过 `%` 解析。驱动若把 None 换成空序列,
    # DDL 里的字面量 `%` 就会被当成占位符 —— 这是真实库上发现的回归。
    from speechnexus_portable_sql.psycopg_driver import _translated

    ddl = "CREATE TABLE t (note TEXT CHECK (note <> '100%'))"
    assert _translated(ddl, None) == ddl


def test_named_parameters_keep_their_mapping() -> None:
    # 具名标记配映射:转成元组会把键当参数值 —— 真实库上表现为「具名占位符需要映射」
    # 与「占位符数与参数数不符」两种报错。
    sql, params = expand_lists("SELECT a FROM t WHERE a = %(value)s", {"value": 3})
    assert sql == "SELECT a FROM t WHERE a = %(value)s"
    assert params == {"value": 3}


def test_a_list_inside_named_parameters_is_refused() -> None:
    with pytest.raises(ContractError, match="positional markers"):
        expand_lists("SELECT a FROM t WHERE a = %(value)s", {"value": ListMarker([1, 2])})
