"""隔离释放账本:读与写都是字面量,插入列与占位符一一对应."""

from __future__ import annotations

from speechnexus_backend.speech.infrastructure import quarantine_release_sql as module

_READ_COLUMNS = (
    "lease_id",
    "evidence_kind",
    "evidence_digest",
    "evidence_observed_at_us",
    "reason",
    "budget_released_at_us",
    "released_submit_intent_ids",
    "idempotency_key_digest",
    "request_digest",
)

_INSERT_COLUMNS = (
    "lease_id",
    "operation_id",
    "attempt_id",
    "stage_id",
    "host_node_id",
    "instance_id",
    "instance_generation",
    "fencing_token",
    "idempotency_key_digest",
    "request_digest",
    "evidence_kind",
    "evidence_digest",
    "evidence_observed_at_us",
    "reason",
    "budget_released_at_us",
    "released_submit_intent_ids",
)


def _projection(statement: str) -> tuple[str, ...]:
    body = statement.split("SELECT ", 1)[1].split(" FROM ", 1)[0]
    return tuple(column.strip() for column in body.split(","))


def _insert_columns(statement: str) -> tuple[str, ...]:
    body = statement.split("(", 1)[1].split(")", 1)[0]
    return tuple(column.strip() for column in body.split(","))


def test_the_ledger_read_projects_its_own_columns_and_the_insert_writes_all_of_them() -> None:
    assert _projection(module._READ_LEDGER) == _READ_COLUMNS
    assert _insert_columns(module._INSERT_LEDGER) == _INSERT_COLUMNS
    # 读是写的子集:账本行必须能按插入时的列名读回来。
    assert set(_READ_COLUMNS) <= set(_INSERT_COLUMNS)
    assert module._READ_LEDGER.endswith("WHERE lease_id = ?")


def test_the_insert_binds_one_marker_per_column() -> None:
    statement = module._INSERT_LEDGER
    assert statement.count("?") == len(_INSERT_COLUMNS)
    assert "%s" not in statement and "{" not in statement
    assert "ON CONFLICT" not in statement.upper()


def test_the_read_never_builds_a_statement_at_call_time() -> None:
    statement = module._READ_LEDGER
    assert statement.startswith("SELECT lease_id, evidence_kind")
    assert "{" not in statement and "%s" not in statement
