"""The readiness report and a create answer one refusal from the same stored rows.

The failure this module exists to catch is not a wrong message: it is a report that
says ``admitting`` while every create is refused. ``#857`` proved that defect cannot
be caught by hand-built facts -- its equivalence test compared two models that were
missing the whole selection layer, and the real database answered differently -- so
every row below drives the production adapter and the real coordination rows, then
compares both surfaces on the same instant.

Each row mutates one deployment fact, asks for a create and reads the report:

* the create answer must be the row's declared public code (or a created Job);
* ``report.cause`` must be that same code's cause, and
  ``report.status == "admitting"`` exactly when the create succeeded.

The report runs ``decide_create_admission`` -- the predicate the create transaction
runs -- so a row cannot pass by agreeing with a second copy of the rules.

Boundaries: PostgreSQL with the shipped schema and roles, one synthetic binding, one
instance and one workspace. It is not a real engine, gateway or GPU: readiness
receipts are rewritten into the state a renewal would produce rather than elapsed by
a clock. The workspace refusals are answered by the service before admission is
consulted, so they are pinned here by name on both surfaces. MySQL/InnoDB and
concurrency are covered by their own suites.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
from tests.integration.test_coordination_foundation import foundation as foundation
from tests.integration.test_production_admission import AdmissionDatabase
from tests.integration.test_production_admission import admission_database as admission_database
from tests.integration.test_production_jobs import database as database
from tests.integration.test_production_runtime_intents import (
    runtime_database as runtime_database,
)
from tests.integration.test_public_job_admission_http_pg import (
    HOST_NODE,
    INSTANCE,
    WORKSPACE,
)
from tests.integration.test_public_job_atomic_admission_pg import _job, _seed, _store

REPORT_CAUSE_BY_CREATE_CODE: dict[str, str] = {
    "engine_not_ready": "engine_receipt_expired",
    "engine_state_mismatch": "engine_state_mismatch",
    "engine_resource_disabled": "engine_resource_disabled",
    "job_capacity_exhausted": "capacity_exhausted",
    "workspace_not_found": "workspace_not_found",
    "workspace_disabled": "workspace_disabled",
    "workspace_not_configured": "workspace_not_configured",
    "job_unavailable": "storage_unavailable",
}


def _query(db: Any, statement: str, parameters: tuple[object, ...] = ()) -> None:
    db.query(statement, parameters)


ROWS: tuple[tuple[str, Callable[[Any, int], None] | None, str | None], ...] = (
    # (id, mutation, expected create public code; ``None`` means "a Job is created")
    ("healthy", None, None),
    (
        "node-quarantined",
        lambda db, now: _query(
            db,
            "UPDATE host_nodes SET node_state = 'quarantined' WHERE host_node_id = %s",
            (HOST_NODE,),
        ),
        "engine_resource_disabled",
    ),
    (
        "quota-full",
        lambda db, now: _query(
            db,
            "UPDATE workspace_quotas SET held_attempts = max_attempts, "
            "quarantined_attempts = 0 WHERE workspace_id = %s",
            (WORKSPACE,),
        ),
        "job_capacity_exhausted",
    ),
    (
        "slots-full",
        lambda db, now: _query(
            db,
            "UPDATE engine_instances SET held_slots = max_slots "
            "WHERE instance_id = %s AND instance_generation = %s",
            INSTANCE,
        ),
        "job_capacity_exhausted",
    ),
    (
        "pool-draining",
        lambda db, now: _query(db, "UPDATE resource_pools SET pool_state = 'draining'"),
        "engine_resource_disabled",
    ),
    (
        "undecodable-intent",
        lambda db, now: _query(
            db,
            "UPDATE runtime_instance_intents SET request_document = %s",
            ("{}",),
        ),
        "engine_state_mismatch",
    ),
    (
        "no-ready-instance",
        lambda db, now: _query(
            db,
            "UPDATE engine_instances SET instance_state = 'draining' "
            "WHERE instance_id = %s AND instance_generation = %s",
            INSTANCE,
        ),
        "job_capacity_exhausted",
    ),
    # Receipt expiry needs the renewal-shaped rewrite the product itself produces,
    # not a clock wait: admission also requires verified_at_us <= ready_at_us <= now.
    ("receipt-expired", None, "engine_not_ready"),
)


def _expire_receipt(admission: AdmissionDatabase) -> None:
    from tests.integration.test_public_job_admission_http_pg import _rewrite_receipt

    db = admission.runtime.foundation.database
    now_us = admission.runtime.now()
    _rewrite_receipt(db, verified_at_us=now_us - 1000, expires_at_us=now_us - 1)


@pytest.mark.parametrize(
    ("row_id", "mutation", "create_code"),
    ROWS,
    ids=[row[0] for row in ROWS],
)
def test_the_report_and_a_create_answer_the_same_refusal(
    admission_database: AdmissionDatabase,
    row_id: str,
    mutation: Callable[[Any, int], None] | None,
    create_code: str | None,
) -> None:
    _seed(admission_database)
    db = admission_database.runtime.foundation.database
    if row_id == "receipt-expired":
        _expire_receipt(admission_database)
    elif mutation is not None:
        mutation(db, admission_database.runtime.now())
    store = _store(admission_database)
    created = store.create(_job())
    report = store.admission_readiness(WORKSPACE)
    # `status` and `cause` are one answer split in two.
    assert (report.status == "admitting") is (report.cause is None)
    if create_code is None:
        assert created.outcome == "created", (row_id, created.error_code)
        assert report.status == "admitting", (row_id, report.cause)
        assert report.detail is None
    else:
        assert created.error_code == create_code, (row_id, created.error_code)
        assert report.status == "degraded", (row_id, report.cause)
        assert (
            report.cause is not None
            and report.cause.value == REPORT_CAUSE_BY_CREATE_CODE[create_code]
        ), (row_id, report.cause)
        assert report.detail


def test_a_report_never_reserves(admission_database: AdmissionDatabase) -> None:
    _seed(admission_database)
    db = admission_database.runtime.foundation.database
    store = _store(admission_database)
    before = db.query(
        "SELECT held_attempts, version FROM workspace_quotas WHERE workspace_id = %s",
        (WORKSPACE,),
    )
    store.admission_readiness(WORKSPACE)
    store.admission_readiness(WORKSPACE)
    assert (
        db.query(
            "SELECT held_attempts, version FROM workspace_quotas WHERE workspace_id = %s",
            (WORKSPACE,),
        )
        == before
    )


def test_the_report_lists_the_ready_instances_with_their_receipt_window(
    admission_database: AdmissionDatabase,
) -> None:
    # ``instances[]`` is the operator's lever: it must name the rows admission reads,
    # with the same receipt vocabulary the console report prints. A report that
    # omitted the expired instance would leave a degraded answer without a cause.
    _seed(admission_database)
    _expire_receipt(admission_database)
    report = _store(admission_database).admission_readiness(WORKSPACE)
    row = next(item for item in report.instances if item.instance_id == INSTANCE[0])
    assert row.instance_generation == INSTANCE[1]
    assert row.readiness_state == "expired"
    assert row.remaining_seconds is not None and row.remaining_seconds < 0
    assert row.max_slots >= row.held_slots + row.quarantined_slots
    assert report.quota is not None and report.quota.max_attempts == 2


def test_an_unknown_workspace_is_reported_by_its_own_name(
    admission_database: AdmissionDatabase,
) -> None:
    # The create for this workspace answers `workspace_not_found` in the service,
    # before admission is consulted; the report must name the same class instead of
    # silently reporting the default workspace.
    _seed(admission_database)
    report = _store(admission_database).admission_readiness("workspace-absent")
    assert report.status == "degraded"
    assert report.cause is not None and report.cause.value == "workspace_not_found"
    assert report.workspace_id == "workspace-absent"
    assert report.instances == ()
