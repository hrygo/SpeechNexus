"""InnoDB parity for the bounded sweep: the same selection and the same release SQL.

PostgreSQL is the product database; InnoDB only has to prove the coordination path is
portable (ADR 0006/0016). This suite therefore reuses the production statement and
the production release SQL through the shared test plumbing
(``tests.support.lease_reconciliation_db`` / ``MySQLLeaseTransactions``) and checks
that the four projections are returned once, that the second pass reads nothing, and
that a lease with a submit intent is left alone.
"""

from __future__ import annotations

import re
from typing import Any

import pytest
from tests.integration.test_coordination_foundation import Foundation
from tests.integration.test_coordination_foundation import foundation as foundation
from tests.integration.test_lease_reconciliation import (
    ATTEMPT,
    INSTANCE,
    LEASE,
    OPERATION,
    POOL,
    admit_outcome,
    batch_slot_sum,
    projections,
    seed_leaked_lease,
)
from tests.integration.test_production_jobs import database as database
from tests.support.job_database import MySQLJobTransactions
from tests.support.lease_reconciliation_db import MySQLLeaseReconciliation

from speechnexus_backend.speech.application.lease_reconciliation import (
    LeaseReconciliation,
    LeaseReconciliationReport,
)
from speechnexus_backend.speech.infrastructure.public_job_schema import PUBLIC_JOB_DDL

pytestmark = [
    pytest.mark.integration,
    pytest.mark.parametrize("database", ["mysql"], indirect=True),
]
#: MySQL only accepts a cross-column CHECK as a named table constraint (error 3813),
#: so the InnoDB fixture moves these three instead of weakening them -- the same
#: treatment the abort-intent DDL already gets in ``test_coordination_foundation``.
_CROSS_COLUMN_CHECKS = (
    "CHECK ((state = 'succeeded') = (result_json IS NOT NULL))",
    "CHECK ((state = 'failed') = (error_code IS NOT NULL))",
    "CHECK (updated_at_us >= created_at_us)",
)


def ensure_speech_jobs(db: Any) -> None:
    """The InnoDB fixture creates the coordination schema only; add the public Job row.

    Migration 0016 owns this table on PostgreSQL. On InnoDB the shared fixture applies
    a fixed DDL list that predates it, so the sweep's InnoDB case creates the same
    declaration here (same text, MySQL spelling) instead of hand-writing a table.
    """
    if db.query(
        "SELECT table_name AS name FROM information_schema.tables "
        "WHERE table_schema = %s AND table_name = 'speech_jobs'",
        (db.schema,),
    ):
        return
    for ddl in PUBLIC_JOB_DDL:
        ddl = ddl.replace('COLLATE "C"', "COLLATE utf8mb4_0900_bin").replace("TEXT", "LONGTEXT")
        for check in _CROSS_COLUMN_CHECKS:
            ddl = ddl.replace(check + ",", "")
        # Dropping the last inline item also dropped the separator before UNIQUE.
        ddl = re.sub(r"(?<!,)[ \t]*\n[ \t]*UNIQUE \(", ",\n        UNIQUE (", ddl)
        db.query(ddl)
    for index, check in enumerate(_CROSS_COLUMN_CHECKS):
        db.query(f"ALTER TABLE speech_jobs ADD CONSTRAINT speech_jobs_check_{index} {check}")


class Reconciler:
    def __init__(self, foundation: Foundation) -> None:
        self.db = foundation.database
        self.driver = MySQLJobTransactions(self.db.url, self.db.schema)

    def sweep(self, limit: int = 8) -> LeaseReconciliationReport:
        return LeaseReconciliation(MySQLLeaseReconciliation(self.driver)).run_once(limit=limit)


@pytest.fixture
def reconciler(foundation: Foundation) -> Reconciler:
    assert foundation.database.dialect == "mysql"
    ensure_speech_jobs(foundation.database)
    return Reconciler(foundation)


def test_the_leaked_batch_slot_is_returned_once_on_innodb(reconciler: Reconciler) -> None:
    db = reconciler.db
    seed_leaked_lease(db)
    assert admit_outcome(db) == "capacity_exhausted"

    report = reconciler.sweep()
    assert len(report.sweeps) == 1, report.sweeps
    sweep = report.sweeps[0]
    assert (sweep.candidate.lease_id, sweep.decision) == (LEASE, "released")
    assert sweep.returned_slots == 1

    after = projections(db)
    assert after["lease"][0]["lease_state"] == "released"
    assert after["lease"][0]["cleanup_state"] == "complete"
    assert after["instance"][0]["held_slots"] == 0
    assert after["host"][0]["held_unified_physical_memory_bytes"] == 1
    assert after["quota"][0]["held_attempts"] == 1
    assert after["attempt"][0]["quota_state"] == "released"
    assert batch_slot_sum(db) == 0
    assert admit_outcome(db) == "admitted"

    before_second = projections(db)
    assert reconciler.sweep().sweeps == ()
    assert projections(db) == before_second


def test_a_lease_with_a_submit_intent_is_never_swept_on_innodb(reconciler: Reconciler) -> None:
    db = reconciler.db
    seed_leaked_lease(db, submit_intent=True)
    before = projections(db)
    report = reconciler.sweep()
    assert report.sweeps == ()
    assert projections(db) == before
    # The batch instance is still the full one #1248 leaves behind.
    assert admit_outcome(db) == "capacity_exhausted"


def test_the_identities_the_sweep_reports_are_the_rows_it_read(reconciler: Reconciler) -> None:
    db = reconciler.db
    seed_leaked_lease(db)
    sweep = reconciler.sweep().sweeps[0]
    candidate = sweep.candidate
    assert (candidate.operation_id, candidate.attempt_id) == (OPERATION, ATTEMPT)
    assert candidate.stage_id == "stage-main"
    assert candidate.reserved_slots == 1
    assert candidate.attempt_quota_state == "held"
    assert projections(db)["lease"][0]["lease_id"] == candidate.lease_id
    assert batch_slot_sum(db) == 0
    assert (
        db.query("SELECT pool_id FROM resource_pools WHERE pool_id = %s", (POOL,))[0]["pool_id"]
        == POOL
    )
    assert projections(db)["instance"][0]["instance_id"] == INSTANCE
