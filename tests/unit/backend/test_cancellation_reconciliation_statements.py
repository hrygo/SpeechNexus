"""取消对账语句:表名与列都写死,CAS 条件不能被改写掉."""

from __future__ import annotations

from speechnexus_backend.speech.infrastructure import (
    job_cancellation_reconciliation_postgres as module,
)

_TABLE = "speech_job_cancellation_reconciliations"
_STATEMENTS = (
    "_CANDIDATES",
    "_READ_CLAIM",
    "_INSERT_CLAIM",
    "_TAKE_CLAIM",
    "_RECORD_SETTLED",
    "_RECORD_RETRY",
    "_RECORD_EXHAUSTED",
    "_READ",
)


def test_every_statement_names_its_tables_literally() -> None:
    for name in _STATEMENTS:
        statement = getattr(module, name)
        assert "{" not in statement, name
        assert "%s" not in statement, name
    for name in _STATEMENTS:
        if name != "_CANDIDATES":
            assert _TABLE in getattr(module, name), name
    candidates = module._CANDIDATES
    for table in (
        "speech_job_cancellation_fences",
        _TABLE,
        "speech_job_cancellation_not_submitted",
    ):
        assert table in candidates


def test_the_settlement_writes_keep_their_cas_predicate() -> None:
    for name in ("_RECORD_SETTLED", "_RECORD_RETRY", "_RECORD_EXHAUSTED", "_TAKE_CLAIM"):
        statement = getattr(module, name)
        assert statement.strip().startswith("UPDATE "), name
    assert module._RECORD_SETTLED.endswith("WHERE job_id = ? AND settled_at_us IS NULL")
    assert "WHERE job_id = ? AND attempts = ? AND settled_at_us IS NULL" in module._TAKE_CLAIM
    assert "next_check_at_us <= ?" in module._TAKE_CLAIM


def test_the_candidate_scan_keeps_its_keyset_and_retry_predicates() -> None:
    statement = module._CANDIDATES
    assert "AND (f.accepted_at_us > ? OR (f.accepted_at_us = ? AND f.job_id > ?))" in statement
    assert "ORDER BY f.accepted_at_us, f.job_id" in statement
    assert "r.last_outcome <> 'exhausted'" in statement
