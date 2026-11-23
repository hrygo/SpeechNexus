"""三条语句的字面量化:abort 回执投影、asset 扫描/标记、binding 草稿插入."""

from __future__ import annotations

from speechnexus_backend.speech.infrastructure import (
    assets_postgres,
    binding_drafts_sql,
)
from speechnexus_gateway.infrastructure import abort_intent_sql


def _projection(statement: str) -> tuple[str, ...]:
    body = statement.split("SELECT ", 1)[1].split(" FROM ", 1)[0]
    return tuple(column.strip() for column in body.split(","))


def test_the_abort_receipt_projects_the_whole_row_in_order() -> None:
    statement = abort_intent_sql._RECEIPT_SELECT
    assert _projection(statement) == tuple(
        column.strip() for column in abort_intent_sql._RECEIPT_COLUMNS.split(",")
    )
    assert statement.endswith("WHERE submit_intent_id=? FOR UPDATE")


def test_the_two_asset_scans_order_by_their_own_poll_hint() -> None:
    pending = assets_postgres._PENDING_CANDIDATES
    expired = assets_postgres._EXPIRED_CANDIDATES
    assert "ORDER BY reconcile_polled_at_us," in pending
    assert "ORDER BY expiry_polled_at_us," in expired
    assert "expiry_polled_at_us" not in pending
    assert "reconcile_polled_at_us" not in expired
    assert assets_postgres._MARK_RECONCILED.startswith(
        "UPDATE speech_assets SET reconcile_polled_at_us = ?"
    )
    assert assets_postgres._MARK_EXPIRED.startswith(
        "UPDATE speech_assets SET expiry_polled_at_us = ?"
    )


def test_the_binding_draft_inserts_are_literal_and_never_upsert() -> None:
    plain = binding_drafts_sql._INSERT_DRAFT
    defaults = binding_drafts_sql._INSERT_DRAFT_WITH_DEFAULTS
    for statement in (plain, defaults):
        assert "{" not in statement and "%s" not in statement
        assert "ON CONFLICT" not in statement.upper()
        columns = statement.split("(", 1)[1].split(")", 1)[0]
        values = statement.split("VALUES (", 1)[1].split(")", 1)[0]
        assert statement.count("?") == len(columns.split(",")) == len(values.split(","))
    assert defaults.split("(", 1)[1].split(")", 1)[0].endswith("defaults_declared")
