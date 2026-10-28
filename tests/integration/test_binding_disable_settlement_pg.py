"""Disable settlement with real restricted PG roles and original lease identities."""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any, cast

import psycopg
import pytest
from psycopg.conninfo import make_conninfo
from tests.integration.test_coordination_foundation import foundation as foundation
from tests.integration.test_production_admission import AdmissionDatabase
from tests.integration.test_production_admission import admission_database as admission_database
from tests.integration.test_production_jobs import database as database
from tests.integration.test_production_runtime_intents import runtime_database as runtime_database
from tests.integration.test_transcription_admission_pg import _adapter, _audio, _plan, _recipe
from tests.integration.test_transcription_disable_barrier_pg import _seed

from speechnexus_backend.binding_disable_worker import (
    _verify_pool_pair,
    _verify_principal,
    _verify_settlement_principal,
)
from speechnexus_backend.speech.application.binding_disable_coordinator import (
    BindingDisableCoordinator,
)
from speechnexus_backend.speech.application.leases import LeaseService
from speechnexus_backend.speech.domain.binding_lifecycle import BindingRetirementCommand
from speechnexus_backend.speech.domain.leases import QuarantineLease
from speechnexus_backend.speech.infrastructure.binding_disable_coordinator_postgres import (
    PostgresDisableCoordinatorStore,
)
from speechnexus_backend.speech.infrastructure.binding_disable_targets_schema import (
    disable_coordinator_grants,
    disable_settlement_grants,
)
from speechnexus_backend.speech.infrastructure.binding_lifecycle_postgres import (
    PostgresBindingLifecycle,
)
from speechnexus_backend.speech.infrastructure.leases_postgres import PostgresLeaseTransactions
from speechnexus_backend.speech.infrastructure.postgres_pool import SpeechPool, speech_pool

pytestmark = [
    pytest.mark.integration,
    pytest.mark.parametrize("database", ["postgresql"], indirect=True),
]


@pytest.mark.asyncio
async def test_disable_settles_unsubmitted_and_retains_active_with_restricted_roles(
    admission_database: AdmissionDatabase,
) -> None:
    admission = admission_database
    db = admission.runtime.foundation.database
    _seed(admission)
    plan = _plan("request-to-settle")
    original = _adapter(admission, _recipe(admission)).admit(
        plan, submit_intent_id="submit-to-settle", audio=_audio(plan)
    )
    assert db.pool is not None
    lifecycle = PostgresBindingLifecycle(db.pool)
    lifecycle.retire(
        BindingRetirementCommand(
            binding_id="asr-quality",
            revision="1",
            request_id="disable-settlement",
            actor_type="anonymous",
            credential_label=None,
            key_digest=None,
            intent_digest="sha256:" + "d" * 64,
            mode="disable",
            reason="synthetic",
        )
    )
    scanner_role, settlement_role = db.schema + "_scan", db.schema + "_settle"
    for role in (scanner_role, settlement_role):
        db.query(f"CREATE ROLE \"{role}\" LOGIN PASSWORD 'synthetic_settlement_test'")
    scanner = speech_pool(
        make_conninfo(db.url, user=scanner_role, password="synthetic_settlement_test"),
        schema=db.schema,
    )
    settlement = speech_pool(
        make_conninfo(db.url, user=settlement_role, password="synthetic_settlement_test"),
        schema=db.schema,
    )

    class Gateway:
        async def abort_idempotent(self, target: Any) -> object:
            return object()

        async def query_disable_target(self, target: Any) -> object:
            return object()

    try:
        for statement in disable_coordinator_grants(db.schema, scanner_role):
            db.query(statement)
        for statement in disable_settlement_grants(db.schema, settlement_role):
            db.query(statement)
        scanner.open()
        settlement.open()
        _verify_principal(scanner, db.schema)
        _verify_settlement_principal(settlement, db.schema)
        _verify_pool_pair(scanner, settlement)
        store = PostgresDisableCoordinatorStore(scanner, timeout_seconds=3)

        class LostCommitReply:
            calls = 0

            @contextmanager
            def connection(self, *, timeout: float) -> Any:
                self.calls += 1
                call = self.calls
                with settlement.connection(timeout=timeout) as connection:
                    yield connection
                if call == 2:  # The actual release committed before the reply was lost.
                    raise psycopg.OperationalError("synthetic lost commit reply")

        lossy = LostCommitReply()
        coordinator = BindingDisableCoordinator(
            store,
            Gateway(),
            gateway_id="gateway-test",
            node_id="host-test",
            batch_size=1,
            wait_timeout_ms=1000,
            settlement=LeaseService(PostgresLeaseTransactions(cast(SpeechPool, lossy))),
        )
        first = await coordinator.run_pass()
        assert first.settled == 1
        assert lossy.calls >= 3  # Unknown release was followed by an original-ID query.
        assert first.pending == 0
        assert db.query(
            "SELECT lease_state FROM admission_leases WHERE lease_id=%s", (original.lease_id,)
        ) == [{"lease_state": "released"}]
        assert db.query(
            "SELECT lease_state FROM admission_leases WHERE lease_id='lease-test-stage-main'"
        ) == [{"lease_state": "reserved"}]
        second = await coordinator.run_pass()
        assert second.settled == second.pending == 0  # Empty root page rotates to the start.
        third = await coordinator.run_pass()
        assert third.settled == 0 and third.pending == 1  # Active submit is not a stop fact.
        db.query(
            "UPDATE submit_intents SET execution_state='unknown_quarantined', "
            "cleanup_state='quarantined' WHERE lease_id='lease-test-stage-main'"
        )
        assert admission.runtime.pool is not None
        quarantined = PostgresLeaseTransactions(admission.runtime.pool).quarantine_lease(
            QuarantineLease(
                "lease-test-stage-main", "engine_transport_unknown", admission.runtime.now()
            )
        )
        assert quarantined.outcome == "committed"
        await coordinator.run_pass()  # Rotate the root cursor.
        await coordinator.run_pass()  # Rotate the lease cursor.
        await coordinator.run_pass()  # Rotate the root cursor again.
        quarantined_pass = await coordinator.run_pass()
        assert quarantined_pass.settled == 0 and quarantined_pass.pending == 1
        assert db.query(
            "SELECT lease_state FROM admission_leases WHERE lease_id='lease-test-stage-main'"
        ) == [{"lease_state": "quarantined"}]
        with (
            pytest.raises(psycopg.errors.InsufficientPrivilege),
            scanner.connection() as connection,
        ):
            connection.execute("UPDATE admission_leases SET lease_state='released'")
        with (
            pytest.raises(psycopg.errors.InsufficientPrivilege),
            settlement.connection() as connection,
        ):
            connection.execute("SELECT request_document FROM submit_intents")
        db.query(f'GRANT SELECT (request_document) ON submit_intents TO "{settlement_role}"')
        with pytest.raises(ValueError, match="privileges"):
            _verify_settlement_principal(settlement, db.schema)
    finally:
        scanner.close()
        settlement.close()
        for role in (scanner_role, settlement_role):
            db.query(f'DROP OWNED BY "{role}"')
            db.query(f'DROP ROLE "{role}"')
