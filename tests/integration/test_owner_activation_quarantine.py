"""Real owner activation waits for A quarantine accounting; no stop inference."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest
from tests.integration.test_production_leases import LeaseDatabase
from tests.integration.test_production_leases import database as database
from tests.integration.test_production_leases import foundation as foundation
from tests.integration.test_production_leases import lease_database as lease_database
from tests.integration.test_production_owners import OwnerDatabase
from tests.integration.test_production_owners import owner_database as owner_database

from speechnexus_backend.speech.domain.leases import LeaseRejectedError
from speechnexus_backend.speech.domain.owners import (
    AcquireOwner,
    ActivateOwner,
    OwnerAuthority,
    RenewOwner,
)
from speechnexus_backend.speech.infrastructure import leases_sql
from speechnexus_backend.speech.infrastructure.coordination_grants import DIMENSIONS

pytestmark = pytest.mark.integration


def _pending_accounting(owner: OwnerDatabase, lease: LeaseDatabase, threshold_ppm: int = 0) -> None:
    assert owner.foundation.database is lease.foundation.database
    db = lease.foundation.database
    assert owner.acquire().outcome == "committed"
    owner.seed_barrier()
    # Synthetic persisted B facts. Production A only reads these gateway-owned facts.
    db.query(
        "UPDATE submit_intents SET execution_state='unknown_quarantined', "
        "cleanup_state='quarantined'"
    )
    db.query("UPDATE host_nodes SET quarantine_threshold_ppm=%s", (threshold_ppm,))


def _totals(snapshot: dict[str, Any]) -> tuple[object, ...]:
    return (
        tuple(
            (
                row["host_node_id"],
                tuple(row["held_" + d] + row["quarantined_" + d] for d in DIMENSIONS),
            )
            for row in snapshot["host_nodes"]
        ),
        tuple(
            (row["instance_id"], row["held_slots"] + row["quarantined_slots"])
            for row in snapshot["engine_instances"]
        ),
        tuple(
            (row["workspace_id"], row["held_attempts"] + row["quarantined_attempts"])
            for row in snapshot["workspace_quotas"]
        ),
    )


@pytest.mark.parametrize("threshold_ppm", [0, 1000000])
def test_gateway_unknown_requires_committed_quarantine_accounting_before_activation(
    owner_database: OwnerDatabase, lease_database: LeaseDatabase, threshold_ppm: int
) -> None:
    owner, lease = owner_database, lease_database
    db = lease.foundation.database
    _pending_accounting(owner, lease, threshold_ppm)
    command = ActivateOwner(owner.authority(), 2)
    before = lease.snapshot()

    rejected = owner.port.activate_owner(command)

    assert rejected.error_code == "reconciliation_required"
    assert lease.snapshot() == before
    assert (
        db.query("SELECT owner_state FROM resource_pools")[0]["owner_state"]
        == "reconciling_takeover"
    )
    assert lease.port.quarantine_lease(lease.request()).outcome == "committed"
    accounted = lease.snapshot()
    assert _totals(accounted) == _totals(before)
    assert accounted["submit_intents"] == before["submit_intents"]
    assert accounted["host_nodes"][0]["node_state"] == (
        "quarantined" if threshold_ppm == 0 else "active"
    )
    assert accounted["admission_leases"][0]["lease_state"] == "quarantined"
    assert accounted["admission_leases"][0]["budget_released_at_us"] is None
    result = owner.port.activate_owner(command)
    assert result.outcome == "committed" and result.fact is not None
    assert result.fact.owner_state == "active" and result.fact.version == 3
    after = lease.snapshot()
    assert all(after[t] == accounted[t] for t in accounted if t != "resource_pools")
    assert _totals(after) == _totals(before)


@pytest.mark.parametrize("fail_quarantine", [False, True])
def test_activation_waits_for_quarantine_transaction_and_rejects_its_rollback(
    owner_database: OwnerDatabase,
    lease_database: LeaseDatabase,
    fail_quarantine: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner, lease = owner_database, lease_database
    db = lease.foundation.database
    _pending_accounting(owner, lease)
    request = lease.request()
    command = ActivateOwner(owner.authority(), 2)
    before = lease.snapshot()
    update = leases_sql._update

    def fail_after_host(cursor: Any, query: str, parameters: tuple[object, ...]) -> None:
        update(cursor, query, parameters)
        if fail_quarantine and query.startswith("UPDATE host_nodes"):
            raise LeaseRejectedError("reconciliation_required")

    monkeypatch.setattr(leases_sql, "_update", fail_after_host)
    blocker = db.blocker()
    try:
        with blocker.cursor() as cursor:
            cursor.execute("SELECT host_node_id FROM host_nodes FOR UPDATE")
            cursor.fetchall()
        with ThreadPoolExecutor(max_workers=2) as workers:
            quarantining = workers.submit(lease.port.quarantine_lease, request)
            try:
                lease.wait_for_lock()  # Quarantine holds pool and waits for this actual host lock.
                activating = workers.submit(owner.port.activate_owner, command)
                owner.wait_for_lock(minimum=2)  # Activation waits behind the same pool row.
                assert not quarantining.done() and not activating.done()
            finally:
                blocker.rollback()
            isolated = quarantining.result(timeout=5)
            activated = activating.result(timeout=5)
    finally:
        blocker.rollback()
        blocker.close()
    if fail_quarantine:
        assert isolated.error_code == "reconciliation_required"
        assert activated.error_code == "reconciliation_required"
        assert lease.snapshot() == before
    else:
        assert isolated.outcome == activated.outcome == "committed"
        after = lease.snapshot()
        assert _totals(after) == _totals(before)
        assert after["submit_intents"] == before["submit_intents"]
        assert after["host_nodes"][0]["node_state"] == "quarantined"
        assert after["admission_leases"][0]["budget_released_at_us"] is None
        assert after["resource_pools"][0]["owner_state"] == "active"


def test_new_owner_cannot_activate_before_accounting_and_old_owner_stays_fenced(
    owner_database: OwnerDatabase, lease_database: LeaseDatabase
) -> None:
    owner, lease = owner_database, lease_database
    db = lease.foundation.database
    _pending_accounting(owner, lease)
    old = owner.authority()
    db.query("UPDATE resource_pools SET owner_expires_at_us=0")
    next_owner = owner.port.acquire_owner(AcquireOwner("tts-realtime", "scheduler-new", 2, 60000))
    assert next_owner.fact is not None and next_owner.fact.fencing_token == 2
    db.query("UPDATE gateway_fencing_barriers SET min_fencing_token=2")
    new = OwnerAuthority("tts-realtime", "scheduler-new", 2)
    before = lease.snapshot()
    assert owner.port.activate_owner(ActivateOwner(old, 3)).error_code == "stale_fencing_token"
    assert owner.port.renew_owner(RenewOwner(old, 3, 60000)).error_code == "stale_fencing_token"
    assert owner.port.activate_owner(ActivateOwner(new, 3)).error_code == "reconciliation_required"
    assert lease.snapshot() == before
    assert lease.port.quarantine_lease(lease.request()).outcome == "committed"
    accounted = lease.snapshot()
    active = owner.port.activate_owner(ActivateOwner(new, 3))
    assert active.fact is not None and active.fact.owner_state == "active"
    assert active.fact.fencing_token == 2 and active.fact.version == 4
    assert owner.port.activate_owner(ActivateOwner(old, 4)).error_code == "stale_fencing_token"
    after = lease.snapshot()
    assert all(after[t] == accounted[t] for t in accounted if t != "resource_pools")
    assert _totals(after) == _totals(before)
