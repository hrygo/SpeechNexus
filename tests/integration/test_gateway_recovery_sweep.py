"""Real PostgreSQL recovery sweep: adopt engine facts, close only on verified release.

The module parametrises the database fixture to PostgreSQL alone, the way the other
PostgreSQL-only gateway adapters do, so the complete integration collection contains no
skip this file needs an exemption for. The termination producer is scripted here; no
engine, container or GPU is contacted.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime

import pytest
from psycopg.conninfo import make_conninfo
from tests.fake.execution import ScriptedEngine, fake_ingress
from tests.integration.test_coordination_foundation import Foundation
from tests.integration.test_coordination_foundation import foundation as foundation
from tests.integration.test_gateway_submit_ledger import (
    identity_for,
    request_for,
    seed_second_attempt,
)
from tests.integration.test_production_jobs import database as database

from speechnexus_gateway.application.recover_executions import ExecutionRecoveryService
from speechnexus_gateway.application.submit_execution import SubmitExecutionService
from speechnexus_gateway.domain.ports import DispatchOutcome
from speechnexus_gateway.domain.recovery import (
    InstanceTerminationEvidence,
    whole_instance_stop_evidence,
)
from speechnexus_gateway.domain.submission import (
    DispatchResult,
    ExecutionState,
    RecordStop,
    StopEvidenceConflictError,
    StopEvidenceKind,
)
from speechnexus_gateway.infrastructure.postgres_pool import GatewayPool, gateway_pool
from speechnexus_gateway.infrastructure.submit_postgres import PostgresSubmissionLedger

pytestmark = [
    pytest.mark.integration,
    # The submit ledger is a PostgreSQL adapter; InnoDB keeps the shared schema oracle.
    pytest.mark.parametrize("database", ["postgresql"], indirect=True),
]

DIGEST = "sha256:" + "b" * 64
LEASE = "lease-alt-stage-main"
PASSWORD = "synthetic_foundation_test"


class LossyEngine(ScriptedEngine):
    """Reports the lost response at dispatch time, the way a real adapter would."""

    def dispatch(self, intent: object) -> DispatchOutcome:
        self.dispatch_count += 1
        return DispatchOutcome(DispatchResult.RESPONSE_LOST, datetime.now(UTC))


class ScriptedTerminations:
    """Answers with the whole-instance facts a test scripts; never invents one."""

    def __init__(self) -> None:
        self.evidence: dict[str, InstanceTerminationEvidence] = {}

    def termination_evidence(self, intent: object) -> InstanceTerminationEvidence | None:
        return self.evidence.get(intent.identity.submit_intent_id)  # type: ignore[attr-defined]


def termination_evidence_for(
    submit_intent_id: str, **overrides: object
) -> InstanceTerminationEvidence:
    fields: dict[str, object] = {
        "instance_id": "instance-test",
        "instance_generation": 1,
        "evidence_id": f"stop-sweep-{submit_intent_id}",
        "evidence_kind": StopEvidenceKind.INSTANCE_TERMINATED_BY_CONTROLLER,
        "evidence_digest": DIGEST,
        "observed_at": datetime.now(UTC),
        "process_exited": True,
        "resources_released": True,
        "reason": None,
    }
    fields.update(overrides)
    return InstanceTerminationEvidence(**fields)  # type: ignore[arg-type]


@pytest.fixture
def sweep_ledger(foundation: Foundation) -> Iterator[PostgresSubmissionLedger]:
    pool: GatewayPool = gateway_pool(
        make_conninfo(foundation.database.url, user=foundation.gateway_role, password=PASSWORD),
        schema=foundation.database.schema,
        max_size=2,
    )
    pool.open()
    try:
        yield PostgresSubmissionLedger(pool)
    finally:
        pool.close()


def uncertain_execution(
    ledger: PostgresSubmissionLedger,
    *,
    submit_intent_id: str = "intent-a",
    lease_id: str = LEASE,
    stage_id: str = "stage-main",
    fencing_token: int = 1,
) -> tuple[SubmitExecutionService, str]:
    """One durable intent whose engine response was lost, so it stays quarantined."""
    engine = LossyEngine()
    service = SubmitExecutionService(
        ingress=fake_ingress(node_id="host-test", gateway_id="gateway-test"),
        ledger=ledger,
        dispatch=engine,
        observation=engine,
    )
    identity = identity_for(
        lease_id,
        submit_intent_id=submit_intent_id,
        stage_id=stage_id,
        fencing_token=fencing_token,
    )
    intent = service.submit(request_for(identity)).intent
    assert intent.execution_state is ExecutionState.UNKNOWN_QUARANTINED
    return service, identity.submit_intent_id


def sweep_for(
    ledger: PostgresSubmissionLedger,
    submissions: SubmitExecutionService,
    terminations: ScriptedTerminations,
) -> ExecutionRecoveryService:
    return ExecutionRecoveryService(
        ingress=fake_ingress(node_id="host-test", gateway_id="gateway-test"),
        ledger=ledger,
        submissions=submissions,
        terminations=terminations,
    )


def stored_rows(
    foundation: Foundation, submit_intent_id: str = "intent-a"
) -> list[dict[str, object]]:
    return foundation.database.query(
        "SELECT s.execution_state, s.cleanup_state, s.version, s.terminated_at_us, "
        "s.engine_request_id, e.evidence_id, e.evidence_kind, l.lease_state, "
        "l.cleanup_state AS lease_cleanup_state, l.budget_released_at_us "
        "FROM submit_intents AS s JOIN admission_leases AS l ON l.lease_id = s.lease_id "
        "LEFT JOIN execution_stop_evidence AS e ON e.submit_intent_id = s.submit_intent_id "
        "WHERE s.submit_intent_id = %s",
        (submit_intent_id,),
    )


def test_verified_termination_closes_the_intent_and_leaves_every_budget_alone(
    foundation: Foundation, sweep_ledger: PostgresSubmissionLedger
) -> None:
    seed_second_attempt(foundation)
    submissions, submit_intent_id = uncertain_execution(sweep_ledger)
    before = stored_rows(foundation)
    terminations = ScriptedTerminations()
    terminations.evidence[submit_intent_id] = termination_evidence_for(submit_intent_id)

    report = sweep_for(sweep_ledger, submissions, terminations).sweep()

    after = stored_rows(foundation)
    before_version = before[0]["version"]
    assert isinstance(before_version, int)
    assert report.examined == 1 and report.closed == 1
    assert after[0]["execution_state"] == "aborted"
    assert after[0]["cleanup_state"] == "complete"
    assert after[0]["terminated_at_us"] is not None
    assert after[0]["version"] == before_version + 1
    assert after[0]["evidence_kind"] == StopEvidenceKind.INSTANCE_TERMINATED_BY_CONTROLLER.value
    # No budget, lease or quota column changes: the Scheduler still owns release.
    assert after[0]["lease_state"] == before[0]["lease_state"]
    assert after[0]["lease_cleanup_state"] == before[0]["lease_cleanup_state"]
    assert after[0]["budget_released_at_us"] == before[0]["budget_released_at_us"]


def test_a_second_sweep_neither_rewrites_nor_reopens_the_closed_fact(
    foundation: Foundation, sweep_ledger: PostgresSubmissionLedger
) -> None:
    seed_second_attempt(foundation)
    submissions, submit_intent_id = uncertain_execution(sweep_ledger)
    terminations = ScriptedTerminations()
    terminations.evidence[submit_intent_id] = termination_evidence_for(submit_intent_id)
    service = sweep_for(sweep_ledger, submissions, terminations)

    assert service.sweep().closed == 1
    settled = stored_rows(foundation)

    assert service.sweep().examined == 0
    assert stored_rows(foundation) == settled


@pytest.mark.parametrize("batch_size", (1, 2), ids=("separate-batches", "same-batch"))
def test_one_instance_termination_closes_every_covered_execution(
    foundation: Foundation, sweep_ledger: PostgresSubmissionLedger, batch_size: int
) -> None:
    seed_second_attempt(foundation)
    submissions, first_id = uncertain_execution(sweep_ledger)
    _, second_id = uncertain_execution(
        sweep_ledger,
        submit_intent_id="intent-b",
        lease_id="lease-alt-stage-alt",
        stage_id="stage-alt",
        fencing_token=2,
    )
    # One physical termination is deliberately reused for both executions.
    source = termination_evidence_for("shared-instance-termination")
    terminations = ScriptedTerminations()
    terminations.evidence = dict.fromkeys((first_id, second_id), source)
    budget_tables = (
        "host_nodes",
        "resource_pools",
        "engine_instances",
        "admission_attempts",
        "admission_leases",
    )
    before = {table: foundation.database.query(f"SELECT * FROM {table}") for table in budget_tables}

    assert sweep_for(sweep_ledger, submissions, terminations).sweep(limit=batch_size).closed == (
        batch_size
    )
    if batch_size == 1:
        # Reconstruct the service between batches: identities cannot depend on its memory.
        assert sweep_for(sweep_ledger, submissions, terminations).sweep(limit=1).closed == 1

    rows = [stored_rows(foundation, intent_id)[0] for intent_id in (first_id, second_id)]
    assert all(row["execution_state"] == "aborted" for row in rows)
    assert all(row["cleanup_state"] == "complete" for row in rows)
    assert len({row["evidence_id"] for row in rows}) == 2
    evidence = foundation.database.query(
        "SELECT evidence_digest, observed_at_us FROM execution_stop_evidence"
    )
    assert len(evidence) == 2
    assert all(row["evidence_digest"] == source.evidence_digest for row in evidence)
    assert evidence[0]["observed_at_us"] == evidence[1]["observed_at_us"]
    first = sweep_ledger.get(first_id)
    assert first is not None
    replay = whole_instance_stop_evidence(first, source)
    assert replay is not None
    sweep_ledger.record_stop(RecordStop(replay, ExecutionState.ABORTED))
    changed = whole_instance_stop_evidence(
        first, replace(source, evidence_digest="sha256:" + "c" * 64)
    )
    assert changed is not None
    with pytest.raises(StopEvidenceConflictError):
        sweep_ledger.record_stop(RecordStop(changed, ExecutionState.ABORTED))
    assert sweep_for(sweep_ledger, submissions, terminations).sweep().examined == 0
    assert [stored_rows(foundation, intent_id)[0] for intent_id in (first_id, second_id)] == rows
    assert {
        table: foundation.database.query(f"SELECT * FROM {table}") for table in budget_tables
    } == before


@pytest.mark.parametrize(
    "overrides",
    (
        {"process_exited": False},
        {"resources_released": False},
        {"evidence_kind": StopEvidenceKind.UNCONFIRMED_TIMEOUT},
        {"instance_generation": 2},
        {"instance_id": "instance-other"},
    ),
    ids=("process-running", "resources-held", "unconfirmed", "other-generation", "other-instance"),
)
def test_incomplete_or_foreign_evidence_leaves_the_row_exactly_as_it_was(
    foundation: Foundation, sweep_ledger: PostgresSubmissionLedger, overrides: dict[str, object]
) -> None:
    """Each rejection gets its own lease: one durable intent per lease is the point."""
    seed_second_attempt(foundation)
    submissions, submit_intent_id = uncertain_execution(sweep_ledger)
    before = stored_rows(foundation, submit_intent_id)
    terminations = ScriptedTerminations()
    terminations.evidence[submit_intent_id] = termination_evidence_for(
        submit_intent_id, **overrides
    )

    report = sweep_for(sweep_ledger, submissions, terminations).sweep()

    assert report.still_unknown == 1 and report.closed == 0
    assert stored_rows(foundation, submit_intent_id) == before


def test_a_sweep_never_reads_another_gateways_uncertain_work(
    foundation: Foundation, sweep_ledger: PostgresSubmissionLedger
) -> None:
    seed_second_attempt(foundation)
    submissions, _ = uncertain_execution(sweep_ledger)

    report = ExecutionRecoveryService(
        ingress=fake_ingress(node_id="host-test", gateway_id="gateway-other"),
        ledger=sweep_ledger,
        submissions=submissions,
        terminations=ScriptedTerminations(),
    ).sweep()

    assert report.examined == 0
