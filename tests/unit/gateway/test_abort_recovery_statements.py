"""abort 回收:三条语句都是字面量,列表参数走 ListMarker 而不是按长度拼接."""

from __future__ import annotations

from speechnexus_gateway.infrastructure import abort_recovery_sql


def test_the_due_read_orders_by_the_next_check_then_identity() -> None:
    statement = abort_recovery_sql._DUE
    assert statement.startswith("SELECT a.submit_intent_id FROM gateway_abort_intents a ")
    assert "ORDER BY COALESCE(NULLIF(a.next_check_at_us,0),a.received_at_us), " in statement
    assert statement.endswith("a.submit_intent_id LIMIT ? FOR UPDATE")
    # 五个过滤/取值参数 + 一个页大小
    assert statement.count("?") == 6


def test_the_list_statements_bind_one_marker_not_a_length_dependent_list() -> None:
    for statement in (abort_recovery_sql._MARK_DUE, abort_recovery_sql._READ_RECEIPTS):
        assert statement.endswith("WHERE submit_intent_id IN (?)")
        assert statement.count("?") == (2 if statement.startswith("UPDATE") else 1)
    assert "submit_intent_id, operation_id, attempt_id, fencing_token, reason, " in (
        abort_recovery_sql._READ_RECEIPTS
    )


def test_no_statement_is_built_at_call_time() -> None:
    for statement in (
        abort_recovery_sql._DUE,
        abort_recovery_sql._MARK_DUE,
        abort_recovery_sql._READ_RECEIPTS,
    ):
        assert "{" not in statement and "%s" not in statement
