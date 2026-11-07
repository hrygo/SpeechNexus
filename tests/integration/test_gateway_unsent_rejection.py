"""A structural refusal before the engine call must persist as a released failure.

The row is written with a dedicated controlled update: the ordinary intent rewrite
refuses to clear an already-dispatched association and refuses to enter
``not_required`` from ``executing``. Runs on PostgreSQL and InnoDB.
"""

import pytest
from tests.integration.test_coordination_foundation import Foundation
from tests.integration.test_coordination_foundation import foundation as foundation
from tests.integration.test_gateway_stop_evidence import (
    start_execution,
)
from tests.integration.test_gateway_stop_evidence import (
    stop_ledger as stop_ledger,
)
from tests.integration.test_production_jobs import database as database

from speechnexus_gateway.domain.file_execution import FileExecutionLedger
from speechnexus_gateway.domain.recovery import RejectExecutionBeforeEngine
from speechnexus_gateway.domain.submission import (
    CleanupState,
    ExecutionErrorCode,
    ExecutionState,
)

pytestmark = pytest.mark.integration


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
