"""翻译层必须只改占位符:字面量、注释与 JSONB 运算符都不能被误伤."""

from __future__ import annotations

import pytest

from speechnexus_portable_sql.markers import (
    AmbiguousMarkerError,
    placeholder_positions,
    qmark_to_pyformat,
)


def test_question_mark_inside_a_string_literal_is_left_alone() -> None:
    assert qmark_to_pyformat("SELECT 'what?' AS q, ? AS n", (1,)) == (
        "SELECT 'what?' AS q, %s AS n"
    )


def test_question_mark_inside_a_comment_is_left_alone() -> None:
    sql = "SELECT 1 -- is this a placeholder?\nFROM t WHERE id = ?"
    assert qmark_to_pyformat(sql, (1,)) == (
        "SELECT 1 -- is this a placeholder?\nFROM t WHERE id = %s"
    )


def test_doubled_quotes_do_not_end_the_literal() -> None:
    assert qmark_to_pyformat("SELECT 'it''s?' AS q, ? AS n", (1,)) == (
        "SELECT 'it''s?' AS q, %s AS n"
    )


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT payload ? 'key' FROM t",
        "SELECT payload ?| array['a'] FROM t",
        "SELECT payload ?& array['a'] FROM t",
    ],
)
def test_operator_forms_are_rejected_instead_of_rewritten(sql: str) -> None:
    with pytest.raises(AmbiguousMarkerError, match="not a placeholder"):
        placeholder_positions(sql)


def test_placeholders_after_operators_are_still_found() -> None:
    assert placeholder_positions("SELECT ? AS a, '?' AS b, ? AS c") == [
        len("SELECT "),
        len("SELECT ? AS a, '?' AS b, "),
    ]
