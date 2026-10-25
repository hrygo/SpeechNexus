"""Real PG/InnoDB eligibility reads for committed file execution, without inference."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

import pytest
from tests.integration.test_coordination_foundation import Foundation
from tests.integration.test_coordination_foundation import foundation as foundation
from tests.integration.test_gateway_command_redaction import wait_for_cleanup_lock
from tests.integration.test_gateway_execution_results import registration, start
from tests.integration.test_gateway_frozen_inputs import snapshot
from tests.integration.test_gateway_recovery import Ledger
from tests.integration.test_gateway_recovery import recovery_ledger as recovery_ledger
from tests.integration.test_gateway_stop_evidence import stop_ledger as stop_ledger
from tests.integration.test_production_jobs import database as database

from speechnexus_gateway.domain.observation import RecordExecutionObservation
from speechnexus_gateway.domain.recovery import MarkExecutionUnknown
from speechnexus_gateway.domain.submission import (
    ExecutionErrorCode,
    ExecutionRejectedError,
    ExecutionState,
    LeaseNotActiveError,
    StaleSubmissionVersionError,
)

pytestmark = pytest.mark.integration


def test_eligible_committed_file_start_is_a_read_and_does_not_claim_again(
    foundation: Foundation, recovery_ledger: Ledger
) -> None:
    _, accepted = start(foundation, recovery_ledger)
    before = snapshot(foundation)
    for _ in range(2):
        assert recovery_ledger.check_file_start(accepted) == accepted
    assert snapshot(foundation) == before


@pytest.mark.parametrize(
    "change",
    [
        "lease",
        "cleanup",
        "lease-expired",
        "deadline-shortened",
        "instance-fenced",
        "loading",
        "draining",
        "generation",
        "barrier",
        "version",
        "cancelled",
        "result",
        "engine-id",
        "digest",
    ],
)
def test_start_rejects_changed_authority_without_modifying_execution(
    foundation: Foundation, recovery_ledger: Ledger, change: str
) -> None:
    _, accepted = start(foundation, recovery_ledger)
    identity = accepted.identity
    db = foundation.database
    if change == "lease":
        db.query(
            "UPDATE admission_leases SET lease_state = 'quarantined' "
            "WHERE lease_id = 'lease-alt-stage-main'"
        )
    elif change == "cleanup":
        db.query(
            "UPDATE admission_leases SET cleanup_state = 'complete' "
            "WHERE lease_id = 'lease-alt-stage-main'"
        )
    elif change in {"lease-expired", "deadline-shortened"}:
        db.query(
            "UPDATE admission_leases SET deadline_at_us = %s "
            "WHERE lease_id = 'lease-alt-stage-main'",
            (
                1
                if change == "lease-expired"
                else int(datetime.now(UTC).timestamp() * 1e6) + 1_000_000,
            ),
        )
    elif change == "instance-fenced":
        db.query("UPDATE engine_instances SET last_fencing_token = 2")
    elif change in {"loading", "draining"}:
        db.query("UPDATE engine_instances SET instance_state = %s", (change,))
    elif change == "generation":
        next_generation = identity.instance_generation + 1
        db.query(
            "INSERT INTO engine_instances (instance_id, instance_generation, host_node_id, "
            "pool_id, gateway_id, binding_revision_id, manifest_id, approved_template_id, "
            "management_mode, loading_policy, config_digest, instance_state, max_slots, "
            "held_slots) SELECT instance_id, %s, host_node_id, pool_id, gateway_id, "
            "binding_revision_id, manifest_id, approved_template_id, management_mode, "
            "loading_policy, config_digest, 'ready', max_slots, 0 FROM engine_instances "
            "WHERE instance_id = %s AND instance_generation = %s",
            (next_generation, identity.instance_id, identity.instance_generation),
        )
        db.query(
            "UPDATE runtime_instance_heads SET current_generation = %s, generation_high_water = %s "
            "WHERE instance_id = %s",
            (next_generation, next_generation, identity.instance_id),
        )
    elif change == "barrier":
        db.query(
            "INSERT INTO gateway_fencing_barriers "
            "(gateway_id, pool_id, min_fencing_token, sealed_evidence_digest) "
            "VALUES (%s, %s, %s, %s)",
            (
                identity.gateway_id,
                identity.pool_id,
                identity.fencing_token + 1,
                "sha256:" + "d" * 64,
            ),
        )
    elif change in {"version", "cancelled"}:
        db.query(
            "UPDATE submit_intents SET version = version + 1, execution_state = %s "
            "WHERE submit_intent_id = 'intent-a'",
            ("stopping" if change == "cancelled" else "executing",),
        )
    elif change == "result":
        accepted = recovery_ledger.record_execution_observation(registration(accepted))
    elif change == "engine-id":
        accepted = replace(accepted, engine_request_id="another-engine-id")
    else:
        accepted = replace(accepted, request_digest="sha256:" + "f" * 64)
    before = snapshot(foundation)
    with pytest.raises((ExecutionRejectedError, StaleSubmissionVersionError)):
        recovery_ledger.check_file_start(accepted)
    assert snapshot(foundation) == before


@pytest.mark.parametrize("changed", ["lease", "expiry"])
def test_start_rechecks_after_an_observed_database_lock_wait(
    foundation: Foundation, recovery_ledger: Ledger, changed: str
) -> None:
    _, accepted = start(foundation, recovery_ledger)
    with foundation.database.blocker() as blocker:
        with blocker.cursor() as cursor:
            cursor.execute(
                (
                    "UPDATE admission_leases SET lease_state = 'quarantined' "
                    if changed == "lease"
                    else "UPDATE admission_leases SET deadline_at_us = 1 "
                )
                + "WHERE lease_id = 'lease-alt-stage-main'"
            )
        with ThreadPoolExecutor(max_workers=1) as threads:
            pending = threads.submit(recovery_ledger.check_file_start, accepted)
            try:
                wait_for_cleanup_lock(foundation)  # Real PG/InnoDB wait, never a sleep substitute.
                blocker.commit()
            finally:
                blocker.rollback()
            with pytest.raises(LeaseNotActiveError):
                pending.result(timeout=5)
    assert recovery_ledger.get("intent-a") == accepted


def test_startup_inventory_is_scoped_bounded_sorted_and_excludes_quarantine(
    foundation: Foundation, recovery_ledger: Ledger
) -> None:
    request, accepted = start(foundation, recovery_ledger)
    other_request = replace(
        request,
        identity=replace(
            request.identity,
            submit_intent_id="intent-b",
            lease_id="lease-alt-stage-alt",
            stage_id="stage-alt",
            fencing_token=2,
        ),
    )
    other = replace(
        accepted,
        identity=other_request.identity,
        request_digest=other_request.request_digest,
        updated_at=accepted.recorded_at,
        version=1,
        execution_state=ExecutionState.SUBMITTING,
        engine_request_id=None,
        dispatched_at=None,
    )
    recovery_ledger.add(other, request_document=other_request.request_document)
    other.mark_dispatched("synthetic-b", at=datetime.now(UTC))
    other = recovery_ledger.record_execution_observation(
        RecordExecutionObservation.from_intent(other, expected_version=1)
    )
    other = recovery_ledger.record_execution_observation(
        RecordExecutionObservation.from_intent(
            replace(other, execution_state=ExecutionState.STOPPING), expected_version=other.version
        )
    )
    scope: dict[str, Any] = {"gateway_id": "gateway-test", "host_node_id": "host-test"}
    before = snapshot(foundation)
    values = recovery_ledger.list_unfinished(**scope, limit=500)
    assert [(x.identity.submit_intent_id, x.execution_state) for x in values] == [
        ("intent-a", ExecutionState.EXECUTING),
        ("intent-b", ExecutionState.STOPPING),
        ("submit-test", ExecutionState.SUBMITTING),
    ]
    assert recovery_ledger.list_unfinished(**scope, limit=1) == values[:1]
    assert recovery_ledger.list_unfinished(**scope, limit=2) == values[:2]
    for field in ("gateway_id", "host_node_id"):
        assert recovery_ledger.list_unfinished(**(scope | {field: "other"}), limit=500) == []
    for limit in (0, 501, True):
        with pytest.raises(ValueError):
            recovery_ledger.list_unfinished(**scope, limit=limit)
    assert snapshot(foundation) == before
    recovery_ledger.mark_execution_unknown(
        MarkExecutionUnknown.for_intent(
            accepted,
            reason_code=ExecutionErrorCode.ENGINE_TRANSPORT_UNKNOWN,
            at=datetime.now(UTC),
        )
    )
    assert recovery_ledger.list_unfinished(**scope, limit=500) == values[1:]
