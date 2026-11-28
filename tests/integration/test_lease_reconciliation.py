"""Production PostgreSQL: the bounded sweep on the real release identity.

The reconciler is what turns #1248 from "an operator edits ``held_slots`` by hand"
into "the next coordinator pass converges". This suite drives the production adapter
under the **declared release role** -- the same
``quarantine_release_grants``/``verify_quarantine_release_role`` pair the CronJob
starts with -- so the evidence covers the identity as well as the SQL:

* a batch instance whose only slot is held by a ``reserved`` lease of a Job that is
  already terminal and was never submitted answers ``capacity_exhausted``; one pass
  returns the four projections, the lease and the attempt to their released value;
* a second pass reads nothing, and two concurrent passes roll the counters back
  exactly once -- judged on the projection numbers, not on an exit code;
* every condition of the selection is load-bearing: a submit intent, a live
  deadline, or a Job that is still running keeps the lease (and its capacity) in
  place. Deleting the ``speech_jobs.state`` condition turns the third case red;
  deleting ``NOT EXISTS (submit_intents)`` turns the first one red.

The lease shape is created directly: the public Job writer of an isolated schema is
exercised by its own suites, and this one needs the *leaked* state a cancelled or
expired Job can leave behind, which no writer produces on purpose.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any

import pytest
from psycopg.conninfo import make_conninfo
from tests.integration.test_coordination_foundation import Foundation
from tests.integration.test_coordination_foundation import foundation as foundation
from tests.integration.test_production_jobs import database as database

from speechnexus_backend.quarantine_release_runtime import verify_quarantine_release_role
from speechnexus_backend.speech.application.lease_reconciliation import (
    LeaseReconciliation,
    LeaseReconciliationReport,
)
from speechnexus_backend.speech.infrastructure.lease_reconciliation_postgres import (
    PostgresLeaseReconciliation,
)
from speechnexus_backend.speech.infrastructure.leases_postgres import (
    PostgresLeaseTransactions,
)
from speechnexus_backend.speech.infrastructure.postgres_pool import SpeechPool, speech_pool
from speechnexus_backend.speech.infrastructure.quarantine_release_schema import (
    quarantine_release_grants,
)

pytestmark = [
    pytest.mark.integration,
    # The declared release role and its startup gate exist on PostgreSQL only; InnoDB
    # parity for the same statement and release SQL is
    # ``test_lease_reconciliation_innodb``.
    pytest.mark.parametrize("database", ["postgresql"], indirect=True),
]
LEASE = "lease-batch-stage-main"
OPERATION = "operation-batch"
ATTEMPT = "attempt-batch"
INSTANCE = "instance-batch"
POOL = "asr-batch"
DIGEST = "sha256:" + "a" * 64
PASSWORD = "synthetic_reconciler_test"
TERMINAL_ERROR = "execution_deadline_expired"


@dataclass
class Reconciler:
    db: Any = field(repr=False)
    pool: SpeechPool = field(repr=False)
    role: str

    def sweep(self, limit: int = 8) -> LeaseReconciliationReport:
        port = PostgresLeaseReconciliation(self.pool, PostgresLeaseTransactions(self.pool))
        return LeaseReconciliation(port).run_once(limit=limit)

    def query(self, statement: str, parameters: tuple[object, ...] = ()) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = self.db.query(statement, parameters)
        return rows


@pytest.fixture
def reconciler(foundation: Foundation) -> Iterator[Reconciler]:
    """Open the production release identity on the migrated schema, then the adapter."""
    db = foundation.database
    role = db.schema + "_release"
    db.query(f"CREATE ROLE \"{role}\" LOGIN PASSWORD '{PASSWORD}'")
    pool: SpeechPool | None = None
    try:
        for grant in quarantine_release_grants(db.schema, role):
            db.query(grant)
        pool = speech_pool(make_conninfo(db.url, user=role, password=PASSWORD), schema=db.schema)
        pool.open()
        # The startup gate the CronJob runs: the declared surface and the live
        # catalog must agree, column by column, before one lease is read.
        verify_quarantine_release_role(pool, db.schema)
        yield Reconciler(db, pool, role)
    finally:
        if pool is not None:
            pool.close()
        db.query(f'DROP OWNED BY "{role}"')
        db.query(f'DROP ROLE "{role}"')


def seed_leaked_lease(
    db: Any,
    *,
    job_state: str = "failed",
    deadline_at_us: int = 1,
    submit_intent: bool = False,
) -> None:
    """The #1248 shape: a batch slot held by a terminal, never-submitted Job."""
    db.query(
        "INSERT INTO resource_pools (pool_id, service_class, pool_state) "
        "VALUES (%s, 'batch', 'active')",
        (POOL,),
    )
    # The realtime lease from the seed already holds one unified byte; the leaked
    # batch lease is the second, so the release has to give exactly one back.
    db.query("UPDATE workspace_quotas SET held_attempts = 2 WHERE workspace_id = 'workspace-test'")
    db.query(
        "UPDATE host_nodes SET held_unified_physical_memory_bytes = 2 "
        "WHERE host_node_id = 'host-test'"
    )
    db.query(
        "INSERT INTO engine_instances (instance_id, instance_generation, host_node_id, pool_id, "
        "gateway_id, binding_revision_id, manifest_id, approved_template_id, management_mode, "
        "loading_policy, config_digest, instance_state, max_slots, held_slots) VALUES "
        "('instance-batch', 1, 'host-test', 'asr-batch', 'gateway-test', 'binding-batch', "
        "'manifest-test', 'template-test', 'managed', 'on_demand', %s, 'ready', 1, 1)",
        (DIGEST,),
    )
    db.query(
        "INSERT INTO runtime_instance_heads "
        "(instance_id, current_generation, generation_high_water) "
        "VALUES ('instance-batch', 1, 1)"
    )
    db.query(
        "INSERT INTO admission_attempts "
        "(operation_id, attempt_id, workspace_id, binding_revision_id, "
        "manifest_id, recipe_digest, policy_revision_id, service_class, request_digest, "
        "required_stage_ids) VALUES ('operation-batch', 'attempt-batch', 'workspace-test', "
        "'binding-batch', 'manifest-test', %s, 'policy-test', 'batch', %s, %s)",
        (DIGEST, DIGEST, '["stage-main"]'),
    )
    db.query(
        "INSERT INTO admission_leases (lease_id, operation_id, attempt_id, stage_id, "
        "host_node_id, instance_id, instance_generation, pool_id, gateway_id, fencing_token, "
        "reserved_slots, deadline_at_us, reserved_unified_physical_memory_bytes) VALUES "
        "('lease-batch-stage-main', 'operation-batch', 'attempt-batch', 'stage-main', "
        "'host-test', 'instance-batch', 1, 'asr-batch', 'gateway-test', 1, 1, %s, 1)",
        (deadline_at_us,),
    )
    db.query(
        "INSERT INTO speech_jobs (job_id, workspace_id, kind, model_id, request_digest, "
        "state, error_code, created_at_us, updated_at_us) VALUES "
        "('operation-batch', 'workspace-test', 'asr', 'model-test', %s, %s, %s, 1, 1)",
        (DIGEST, job_state, TERMINAL_ERROR if job_state == "failed" else None),
    )
    if submit_intent:
        db.query(
            "INSERT INTO submit_intents (submit_intent_id, lease_id, operation_id, attempt_id, "
            "command_digest, request_digest, request_document, deadline_at_us, recorded_at_us, "
            "updated_at_us, output_stream_id, sample_rate_hz, hard_output_ceiling_samples) "
            "VALUES ('submit-batch', 'lease-batch-stage-main', 'operation-batch', 'attempt-batch', "
            "%s, %s, %s, 1, 1, 1, 'asr-main', 16000, 48000)",
            (DIGEST, DIGEST, '{"synthetic_execution_body":true}'),
        )


def projections(db: Any) -> dict[str, Any]:
    """The four held counters plus the lease and attempt rows, as one comparable fact."""
    return {
        "quota": db.query(
            "SELECT workspace_id, held_attempts, quarantined_attempts, version "
            "FROM workspace_quotas WHERE workspace_id = 'workspace-test'"
        ),
        "host": db.query(
            "SELECT host_node_id, held_unified_physical_memory_bytes, version "
            "FROM host_nodes WHERE host_node_id = 'host-test'"
        ),
        "instance": db.query(
            "SELECT instance_id, held_slots, quarantined_slots, max_slots, version "
            "FROM engine_instances WHERE instance_id = 'instance-batch'"
        ),
        "attempt": db.query(
            "SELECT operation_id, attempt_id, quota_state, version "
            "FROM admission_attempts WHERE attempt_id = 'attempt-batch'"
        ),
        "lease": db.query(
            "SELECT lease_id, lease_state, cleanup_state, budget_released_at_us, version "
            "FROM admission_leases WHERE lease_id = 'lease-batch-stage-main'"
        ),
    }


def batch_slot_sum(db: Any) -> int:
    """The instance's unreleased non-realtime leases, exactly as admission sums them."""
    rows = db.query(
        "SELECT COALESCE(SUM(l.reserved_slots), 0) AS slots FROM admission_leases l "
        "JOIN admission_attempts a ON a.operation_id = l.operation_id "
        "AND a.attempt_id = l.attempt_id WHERE l.instance_id = %s "
        "AND l.instance_generation = %s AND l.lease_state <> 'released' "
        "AND a.service_class <> 'realtime'",
        (INSTANCE, 1),
    )
    return int(rows[0]["slots"])


def admit_outcome(db: Any) -> str:
    """What the same numbers would make of one more batch slot on this instance."""
    state = projections(db)["instance"][0]
    held = int(state["held_slots"]) + int(state["quarantined_slots"])
    batch = batch_slot_sum(db)
    if held + 1 > int(state["max_slots"]) or batch + 1 > int(state["max_slots"]):
        return "capacity_exhausted"
    return "admitted"


def test_the_leaked_batch_slot_is_returned_exactly_once(reconciler: Reconciler) -> None:
    db = reconciler.db
    seed_leaked_lease(db)
    # The reported field symptom, reproduced without any manual counter edit.
    assert admit_outcome(db) == "capacity_exhausted"

    report = reconciler.sweep()
    assert len(report.sweeps) == 1
    sweep = report.sweeps[0]
    assert (sweep.candidate.lease_id, sweep.decision) == (LEASE, "released"), sweep
    assert sweep.returned_slots == 1

    after = projections(db)
    assert after["lease"] == [
        {
            "lease_id": LEASE,
            "lease_state": "released",
            "cleanup_state": "complete",
            "budget_released_at_us": after["lease"][0]["budget_released_at_us"],
            "version": 2,
        }
    ]
    assert after["lease"][0]["budget_released_at_us"] is not None
    assert after["instance"][0]["held_slots"] == 0
    assert after["host"][0]["held_unified_physical_memory_bytes"] == 1
    assert after["quota"][0]["held_attempts"] == 1
    assert after["attempt"][0]["quota_state"] == "released"
    assert batch_slot_sum(db) == 0
    # The next Job on this instance is admitted instead of refused (#1248's criterion).
    assert admit_outcome(db) == "admitted"

    # A second pass reads nothing: the released lease is the durable record, and no
    # counter is rolled back twice.
    before_second = projections(db)
    second = reconciler.sweep()
    assert second.sweeps == ()
    assert projections(db) == before_second


def test_a_cancelled_never_dispatched_job_is_swept_too(reconciler: Reconciler) -> None:
    # #1235's shape: the cancellation was accepted before any dispatch, so the Job is
    # terminal with no error code and no execution.
    db = reconciler.db
    seed_leaked_lease(db, job_state="cancelled")
    assert admit_outcome(db) == "capacity_exhausted"
    report = reconciler.sweep()
    assert [sweep.decision for sweep in report.sweeps] == ["released"]
    assert admit_outcome(db) == "admitted"


def test_a_lease_with_a_submit_intent_is_never_swept(reconciler: Reconciler) -> None:
    # The execution reached the gateway, so no automatic path may return its capacity:
    # the stop is unproven and only a verified release may settle it.
    db = reconciler.db
    seed_leaked_lease(db, submit_intent=True)
    before = projections(db)
    report = reconciler.sweep()
    assert report.sweeps == ()
    assert report.returned_slots == 0
    assert projections(db) == before


def test_a_live_deadline_is_never_swept(reconciler: Reconciler) -> None:
    db = reconciler.db
    now_us = int(
        db.query(
            "SELECT CAST(EXTRACT(EPOCH FROM clock_timestamp()) * 1000000 AS BIGINT) AS now_us"
        )[0]["now_us"]
    )
    seed_leaked_lease(db, deadline_at_us=now_us + 60_000_000)
    before = projections(db)
    assert reconciler.sweep().sweeps == ()
    assert projections(db) == before


@pytest.mark.parametrize("job_state", ["pending", "running", "cancelling"])
def test_a_job_that_can_still_be_dispatched_is_never_swept(
    reconciler: Reconciler, job_state: str
) -> None:
    # The dispatcher refuses a terminal Job before planning, so a non-terminal one can
    # still create a submit intent; its lease is live capacity, not a leak.
    db = reconciler.db
    seed_leaked_lease(db, job_state=job_state)
    before = projections(db)
    assert reconciler.sweep().sweeps == ()
    assert projections(db) == before


def test_two_concurrent_sweeps_return_capacity_once(reconciler: Reconciler) -> None:
    db = reconciler.db
    seed_leaked_lease(db)
    barrier = threading.Barrier(2)

    def pass_once() -> LeaseReconciliationReport:
        barrier.wait(timeout=5)
        return reconciler.sweep()

    with ThreadPoolExecutor(max_workers=2) as executor:
        reports = [
            future.result(timeout=30) for future in [executor.submit(pass_once) for _ in range(2)]
        ]

    decisions = [sweep.decision for report in reports for sweep in report.sweeps]
    released = decisions.count("released")
    # One of the two may lose the row version race; what must never happen is a
    # second rollback, so the counters -- not the outcome mix -- decide the case.
    assert released <= 1
    assert set(decisions) <= {"released", "replayed", "refused"}
    assert [decision for decision in decisions if decision == "released"] == ["released"]
    after = projections(db)
    assert after["lease"][0]["lease_state"] == "released"
    assert after["instance"][0]["held_slots"] == 0
    assert after["host"][0]["held_unified_physical_memory_bytes"] == 1
    assert after["quota"][0]["held_attempts"] == 1
    assert admit_outcome(db) == "admitted"
