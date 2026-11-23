"""MySQL 验证路径的驱动也要展开 `ListMarker`,与 psycopg 驱动共用一条规则."""

from __future__ import annotations

import pytest

from speechnexus_portable_sql.contract import ContractError, ListMarker
from speechnexus_portable_sql.pymysql_driver import _PortableCursor


class _Recorder:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []

    def execute(self, sql: str, params: object = None) -> None:
        self.calls.append((sql, params))


def _cursor() -> tuple[_PortableCursor, _Recorder]:
    recorder = _Recorder()
    return _PortableCursor(recorder), recorder


def test_a_list_marker_expands_to_one_placeholder_per_identity() -> None:
    cursor, recorder = _cursor()
    cursor.execute(
        "UPDATE gateway_abort_intents SET next_check_at_us=? WHERE submit_intent_id IN (?)",
        (7, ListMarker(("intent-a", "intent-b"))),
    )
    sql, params = recorder.calls[0]
    assert sql == (
        "UPDATE gateway_abort_intents SET next_check_at_us=%s WHERE submit_intent_id IN (%s, %s)"
    )
    assert params == (7, "intent-a", "intent-b")


def test_the_same_marker_object_may_serve_two_statements() -> None:
    cursor, recorder = _cursor()
    marker = ListMarker(("intent-a", "intent-b"))
    cursor.execute("SELECT 1 FROM t WHERE id IN (?)", (marker,))
    cursor.execute("SELECT 2 FROM t WHERE id IN (?)", (marker,))
    assert recorder.calls[1][1] == ("intent-a", "intent-b")


def test_a_list_marker_in_a_legacy_mixed_statement_is_refused() -> None:
    cursor, _ = _cursor()
    with pytest.raises(ContractError):
        cursor.execute("SELECT 1 FROM t WHERE id=%s AND other IN (?)", (1, ListMarker(("a",))))


def test_plain_parameters_still_translate_without_expansion() -> None:
    cursor, recorder = _cursor()
    cursor.execute("SELECT 1 FROM t WHERE id=?", ("a",))
    assert recorder.calls[0] == ("SELECT 1 FROM t WHERE id=%s", ("a",))
