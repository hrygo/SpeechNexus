"""A structural refusal before the engine call must persist as a released failure.

The row is written with a dedicated controlled update: the ordinary intent rewrite
refuses to clear an already-dispatched association and refuses to enter
``not_required`` from ``executing``. Runs on PostgreSQL and InnoDB.

The guard matrix below executes that statement directly, with a valid control and
with one frozen fact changed at a time. It deliberately bypasses ``apply()`` so a
deleted SQL guard cannot stay hidden behind the domain preconditions; the normal
caller path is covered by the first test.
"""

from datetime import UTC, datetime
from typing import Any

import pytest
from tests.integration.test_coordination_foundation import Foundation
from tests.integration.test_coordination_foundation import foundation as foundation
from tests.integration.test_gateway_stop_evidence import start_execution
from tests.integration.test_gateway_stop_evidence import stop_ledger as stop_ledger
from tests.integration.test_production_jobs import database as database

from speechnexus_gateway.domain.file_execution import FileExecutionLedger
from speechnexus_gateway.domain.recovery import RejectExecutionBeforeEngine
from speechnexus_gateway.domain.submission import (
    CleanupState,
    ExecutionErrorCode,
    ExecutionState,
)
from speechnexus_gateway.infrastructure.submit_sql import (
    REJECT_UNSENT_INTENT,
    to_unix_micros,
)

pytestmark = pytest.mark.integration
DIGEST = "sha256:" + "a" * 64


def _row(foundation: Foundation, submit_intent_id: str = "intent-a") -> dict[str, Any]:
    rows = foundation.database.query(
        "SELECT execution_state, cleanup_state, engine_request_id, dispatched_at_us, "
        "error_code, generated_pcm_samples, staging_output_object_key, terminated_at_us, "
        "version FROM submit_intents WHERE submit_intent_id = %s",
        (submit_intent_id,),
    )
    assert len(rows) == 1
    return rows[0]


def _run_rejection_statement(
    foundation: Foundation,
    submit_intent_id: str,
    version: int,
) -> int:
    with foundation.connection("gateway") as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                REJECT_UNSENT_INTENT,
                (
                    ExecutionErrorCode.INVALID_EXECUTION_REQUEST.value,
                    to_unix_micros(datetime.now(UTC)),
                    submit_intent_id,
                    version,
                ),
            )
            rowcount = cursor.rowcount
        connection.commit()
    return int(rowcount)


def test_an_unsent_rejection_closes_the_row_without_capacity(
    foundation: Foundation, stop_ledger: FileExecutionLedger
) -> None:
    start_execution(foundation, stop_ledger)
    before = stop_ledger.get("intent-a")
    assert before is not None and before.execution_state is ExecutionState.EXECUTING
    assert before.engine_request_id is not None and before.dispatched_at is not None

    rejected = stop_ledger.reject_execution_before_engine(RejectExecutionBeforeEngine("intent-a"))

    assert rejected.execution_state is ExecutionState.FAILED
    assert rejected.cleanup_state is CleanupState.NOT_REQUIRED
    assert rejected.error_code is ExecutionErrorCode.INVALID_EXECUTION_REQUEST
    assert rejected.engine_request_id is None
    assert rejected.dispatched_at is None
    assert rejected.staging_result is None
    assert not rejected.execution_state.holds_capacity

    rows = foundation.database.query(
        "SELECT execution_state, cleanup_state, engine_request_id, dispatched_at_us, "
        "error_code FROM submit_intents WHERE submit_intent_id = %s",
        ("intent-a",),
    )
    assert len(rows) == 1
    assert rows[0]["execution_state"] == "failed"
    assert rows[0]["cleanup_state"] == "not_required"
    assert rows[0]["engine_request_id"] is None
    assert rows[0]["dispatched_at_us"] is None
    assert rows[0]["error_code"] == "invalid_execution_request"

    evidence = foundation.database.query(
        "SELECT count(*) AS n FROM execution_stop_evidence WHERE submit_intent_id = %s",
        ("intent-a",),
    )
    assert evidence[0]["n"] == 0


#: One frozen fact changed at a time. ``staging_result_document`` is omitted on
#: purpose: its CHECK requires ``staging_output_object_key`` to be non-null, so
#: the key case already covers that row shape and the document-only shape cannot
#: exist. ``version_mismatch`` and ``unknown_id`` do not mutate a frozen state.
GUARDS = (
    "control",
    "succeeded",
    "stopping",
    "submitting",
    "cleanup_complete",
    "pcm",
    "staging_key",
    "terminated",
    "stop_evidence",
    "version_mismatch",
    "unknown_id",
)


def _mutate(foundation: Foundation, guard: str, now_us: int) -> None:
    # Minimal DB-valid row shapes: this matrix exercises the SQL statement in
    # isolation, so a state need not satisfy SubmissionIntent.validate().
    if guard == "succeeded":
        foundation.database.query(
            "UPDATE submit_intents SET execution_state = 'succeeded', "
            "error_code = NULL WHERE submit_intent_id = 'intent-a'"
        )
    elif guard == "stopping":
        foundation.database.query(
            "UPDATE submit_intents SET execution_state = 'stopping' "
            "WHERE submit_intent_id = 'intent-a'"
        )
    elif guard == "submitting":
        foundation.database.query(
            "UPDATE submit_intents SET execution_state = 'submitting', "
            "cleanup_state = 'pending', engine_request_id = NULL, dispatched_at_us = NULL "
            "WHERE submit_intent_id = 'intent-a'"
        )
    elif guard == "cleanup_complete":
        foundation.database.query(
            "UPDATE submit_intents SET cleanup_state = 'complete' "
            "WHERE submit_intent_id = 'intent-a'"
        )
    elif guard == "pcm":
        foundation.database.query(
            "UPDATE submit_intents SET generated_pcm_samples = 1 "
            "WHERE submit_intent_id = 'intent-a'"
        )
    elif guard == "staging_key":
        foundation.database.query(
            "UPDATE submit_intents SET staging_output_object_key = 'synthetic/staging/result' "
            "WHERE submit_intent_id = 'intent-a'"
        )
    elif guard == "terminated":
        foundation.database.query(
            "UPDATE submit_intents SET terminated_at_us = %s WHERE submit_intent_id = 'intent-a'",
            (now_us,),
        )
    elif guard == "stop_evidence":
        foundation.database.query(
            "INSERT INTO execution_stop_evidence (evidence_id, submit_intent_id, evidence_kind, "
            "evidence_digest, observed_at_us, execution_stopped, resources_released) "
            "VALUES ('evidence-guard', 'intent-a', 'engine_terminal_response', %s, %s, 1, 1)",
            (DIGEST, now_us),
        )
    elif guard == "version_mismatch":
        foundation.database.query(
            "UPDATE submit_intents SET version = version + 1 WHERE submit_intent_id = 'intent-a'"
        )


@pytest.mark.parametrize("guard", GUARDS)
def test_the_guarded_update_refuses_every_frozen_fact(
    foundation: Foundation, stop_ledger: FileExecutionLedger, guard: str
) -> None:
    start_execution(foundation, stop_ledger)
    before = _row(foundation)
    assert before["execution_state"] == "executing"
    assert before["cleanup_state"] == "pending"
    version = int(before["version"])
    now_us = to_unix_micros(datetime.now(UTC))

    if guard != "unknown_id":
        _mutate(foundation, guard, now_us)
    mutated = _row(foundation)
    if guard in {"control", "unknown_id", "stop_evidence"}:
        assert mutated == before
    else:
        assert mutated != before

    submit_intent_id = "missing-intent" if guard == "unknown_id" else "intent-a"
    expected_version = version - 1 if guard == "version_mismatch" else version
    rowcount = _run_rejection_statement(foundation, submit_intent_id, expected_version)

    if guard == "control":
        assert rowcount == 1
        after = _row(foundation)
        assert after["execution_state"] == "failed"
        assert after["cleanup_state"] == "not_required"
        assert after["engine_request_id"] is None
        assert after["dispatched_at_us"] is None
        return

    assert rowcount == 0
    assert _row(foundation) == mutated
