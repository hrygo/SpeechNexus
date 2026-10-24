"""Named full results for the submit-intent lifecycle on real PostgreSQL.

The unit suite exercises the same boundary against an in-memory store. Here the boundary
runs on the restricted gateway login, so these tests also show what a reported outcome means
for the durable fact: one row, one dispatch, one version per real transition, and an
identical repeat that changes nothing.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from tests.fake.execution import ScriptedEngine
from tests.integration.test_coordination_foundation import Foundation
from tests.integration.test_coordination_foundation import foundation as foundation
from tests.integration.test_gateway_stop_evidence import counters, evidence_for, new_pool
from tests.integration.test_gateway_submit_http import contract_command, submit_body
from tests.integration.test_gateway_submit_ledger import (
    identity_for,
    seed_second_attempt,
    service_for,
)
from tests.integration.test_production_jobs import database as database
from tests.support.contracts import validator
from tests.unit.gateway.test_execution_fact_boundary import stop_request, unknown_request

from speechnexus_gateway.domain.submission import (
    SubmissionIdentity,
    derived_output_stream_id,
)
from speechnexus_gateway.infrastructure.postgres_pool import GatewayPool
from speechnexus_gateway.infrastructure.submit_postgres import PostgresSubmissionLedger
from speechnexus_gateway.interfaces.submit_transactions import ExecutionFactBoundary

pytestmark = [
    pytest.mark.integration,
    # The submit ledger is a PostgreSQL adapter; InnoDB keeps the shared schema oracle.
    pytest.mark.parametrize("database", ["postgresql"], indirect=True),
]

LEASE = "lease-alt-stage-main"
SECOND_LEASE = "lease-alt-stage-alt"
FactBoundary = tuple[ExecutionFactBoundary, PostgresSubmissionLedger, ScriptedEngine]


def create_submit_request(
    identity: SubmissionIdentity, command: Any, **changes: Any
) -> dict[str, Any]:
    """One frozen ``create_submit`` command naming the stream its attempt emits."""
    return {
        "execution_request": submit_body(identity, command, **changes),
        "output_stream_id": derived_output_stream_id(identity.operation_id, identity.attempt_id),
    }


def assert_frozen_result(operation: str, request: dict[str, Any], result: dict[str, Any]) -> None:
    validator("internal/coordination-transactions.schema.json", operation).validate(request)
    validator("internal/coordination-transactions.schema.json", "TransactionResult").validate(
        result
    )


@pytest.fixture
def facts(foundation: Foundation) -> Iterator[FactBoundary]:
    seed_second_attempt(foundation)
    pool: GatewayPool = new_pool(foundation)
    ledger = PostgresSubmissionLedger(pool)
    service, engine = service_for(ledger)
    try:
        yield ExecutionFactBoundary(submit=service, ledger=ledger), ledger, engine
    finally:
        pool.close()


def test_create_submit_reports_committed_then_replayed_with_one_dispatch(
    foundation: Foundation, facts: FactBoundary
) -> None:
    boundary, _, engine = facts
    identity = identity_for(LEASE, submit_intent_id="intent-a")
    request = create_submit_request(identity, contract_command())

    first = boundary.create_submit(request)
    replay = boundary.create_submit(request)

    assert_frozen_result("create_submit", request, first)
    assert first["outcome"] == "committed"
    assert first["scope_id"] == "intent-a"
    assert first["version"] == "2"
    execution = first["value"]["execution"]
    assert execution["execution_state"] == "executing"
    assert execution["cleanup_state"] == "pending"
    assert execution["dispatched_at"] is not None
    assert replay["outcome"] == "replayed"
    assert replay["value"] == first["value"]
    assert engine.dispatch_count == 1

    rows = foundation.database.query(
        "SELECT submit_intent_id, execution_state, version FROM submit_intents WHERE lease_id = %s",
        (LEASE,),
    )
    assert rows == [{"submit_intent_id": "intent-a", "execution_state": "executing", "version": 2}]


def test_a_conflicting_or_mismatched_command_is_reported_without_a_second_dispatch(
    foundation: Foundation, facts: FactBoundary
) -> None:
    boundary, _, engine = facts
    identity = identity_for(LEASE, submit_intent_id="intent-a")
    assert (
        boundary.create_submit(create_submit_request(identity, contract_command()))["outcome"]
        == "committed"
    )

    conflict = boundary.create_submit(create_submit_request(identity, contract_command(1)))
    mismatched = boundary.create_submit(
        {
            "execution_request": submit_body(
                identity, contract_command(), command_digest="sha256:" + "b" * 64
            ),
            "output_stream_id": derived_output_stream_id("operation-test", "attempt-alt"),
        }
    )

    assert conflict["outcome"] == "rejected"
    assert conflict["error_code"] == "submit_intent_conflict"
    assert mismatched["outcome"] == "rejected"
    assert mismatched["error_code"] == "invalid_execution_request"
    assert engine.dispatch_count == 1
    assert foundation.database.query(
        "SELECT submit_intent_id FROM submit_intents WHERE lease_id = %s", (LEASE,)
    ) == [{"submit_intent_id": "intent-a"}]


def test_record_stop_reports_committed_then_replayed_and_leaves_budgets_alone(
    foundation: Foundation, facts: FactBoundary
) -> None:
    boundary, _, engine = facts
    identity = identity_for(LEASE, submit_intent_id="intent-a")
    assert (
        boundary.create_submit(create_submit_request(identity, contract_command()))["outcome"]
        == "committed"
    )
    before = counters(foundation)
    request = stop_request(evidence_for(foundation, submit_intent_id="intent-a"))

    first = boundary.record_stop(request)
    replay = boundary.record_stop(request)

    assert_frozen_result("record_stop", request, first)
    assert first["outcome"] == "committed"
    assert first["version"] == "3"
    execution = first["value"]["execution"]
    assert execution["execution_state"] == "aborted"
    assert execution["cleanup_state"] == "complete"
    assert execution["stop_evidence_kind"] == "engine_abort_ack"
    assert replay["outcome"] == "replayed"
    assert replay["version"] == first["version"]
    assert replay["value"] == first["value"]
    assert engine.dispatch_count == 1
    # Releasing capacity stays with the Scheduler: this boundary records stop facts only.
    assert counters(foundation) == before
    assert (
        foundation.database.query(
            "SELECT count(*) AS n FROM execution_stop_evidence WHERE submit_intent_id = 'intent-a'"
        )[0]["n"]
        == 1
    )


def test_mark_execution_unknown_reports_committed_then_replayed_and_keeps_capacity(
    foundation: Foundation, facts: FactBoundary
) -> None:
    boundary, _, engine = facts
    identity = identity_for(
        SECOND_LEASE, submit_intent_id="intent-b", stage_id="stage-alt", fencing_token=2
    )
    assert (
        boundary.create_submit(create_submit_request(identity, contract_command()))["outcome"]
        == "committed"
    )
    before = counters(foundation)
    now_us = foundation.database.query(
        "SELECT CAST(EXTRACT(EPOCH FROM clock_timestamp()) * 1000000 AS BIGINT) AS now_us"
    )[0]["now_us"]
    request = unknown_request("intent-b", observed_at_us=now_us)

    first = boundary.mark_execution_unknown(request)
    replay = boundary.mark_execution_unknown(request)

    assert_frozen_result("mark_execution_unknown", request, first)
    assert first["outcome"] == "committed"
    assert first["version"] == "3"
    execution = first["value"]["execution"]
    assert execution["execution_state"] == "unknown_quarantined"
    assert execution["cleanup_state"] == "quarantined"
    assert execution["error_code"] == "engine_transport_unknown"
    assert replay["outcome"] == "replayed"
    assert replay["value"] == first["value"]
    assert engine.dispatch_count == 1
    assert counters(foundation) == before


def test_a_terminal_or_missing_execution_is_reported_as_rejected(
    foundation: Foundation, facts: FactBoundary
) -> None:
    boundary, _, engine = facts
    identity = identity_for(LEASE, submit_intent_id="intent-a")
    assert (
        boundary.create_submit(create_submit_request(identity, contract_command()))["outcome"]
        == "committed"
    )
    stopped = stop_request(evidence_for(foundation, submit_intent_id="intent-a"))
    assert boundary.record_stop(stopped)["outcome"] == "committed"
    now_us = foundation.database.query(
        "SELECT CAST(EXTRACT(EPOCH FROM clock_timestamp()) * 1000000 AS BIGINT) AS now_us"
    )[0]["now_us"]

    terminal = boundary.mark_execution_unknown(unknown_request("intent-a", observed_at_us=now_us))
    missing = boundary.mark_execution_unknown(
        unknown_request("intent-absent", observed_at_us=now_us)
    )

    assert terminal["outcome"] == "rejected"
    assert terminal["error_code"] == "execution_cleanup_pending"
    assert terminal["recovery"] == "reconcile"
    assert missing["outcome"] == "rejected"
    assert missing["error_code"] == "invalid_execution_request"
    assert engine.dispatch_count == 1
    stored = foundation.database.query(
        "SELECT execution_state, cleanup_state FROM submit_intents "
        "WHERE submit_intent_id = 'intent-a'"
    )
    assert stored == [{"execution_state": "aborted", "cleanup_state": "complete"}]
