"""Enforced ingress ordering and durable barriers on PG and InnoDB, no inference model."""

from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from threading import Event
from typing import Any, cast

import psycopg
import pytest
from tests.fake.execution import ScriptedEngine
from tests.integration.test_coordination_foundation import Foundation
from tests.integration.test_coordination_foundation import foundation as foundation
from tests.integration.test_gateway_stop_evidence import counters, evidence_for, new_pool
from tests.integration.test_gateway_stop_evidence import stop_ledger as stop_ledger
from tests.integration.test_gateway_submit_ledger import (
    identity_for,
    request_for,
    seed_second_attempt,
)
from tests.integration.test_production_jobs import database as database
from tests.integration.test_production_owners import OwnerDatabase
from tests.integration.test_production_owners import owner_database as owner_database
from tests.support.contracts import validator

from speechnexus_backend.speech.domain.owners import ActivateOwner, OwnerAuthority
from speechnexus_gateway.application.ingress import GatewayIngress
from speechnexus_gateway.application.submit_execution import SubmitExecutionService
from speechnexus_gateway.domain.fencing import BarrierRequest, IngressUnavailableError
from speechnexus_gateway.domain.ports import BarrierLedger, DispatchOutcome, SubmissionLedger
from speechnexus_gateway.domain.submission import (
    DispatchResult,
    GatewayBarrierFencedError,
    InvalidExecutionRequestError,
    StaleFencingTokenError,
    SubmissionOutcomeUnknownError,
    SubmissionStoreError,
)
from speechnexus_gateway.infrastructure.process_ownership import ProcessIngressOwnership
from speechnexus_gateway.infrastructure.submit_postgres import PostgresSubmissionLedger

pytestmark = pytest.mark.integration
REQUEST = BarrierRequest("tts-realtime", "scheduler-new", 2, False)


@pytest.fixture
def ingress(tmp_path: Path) -> Iterator[GatewayIngress]:
    owner = ProcessIngressOwnership(tmp_path, node_id="host-test", gateway_id="gateway-test")
    try:
        yield GatewayIngress(owner, timeout_seconds=0.1)
    finally:
        owner.close()


def takeover(foundation: Foundation, *, token: int = 2, expires: int = 2**53 - 1) -> None:
    foundation.database.query(
        "UPDATE resource_pools SET owner_id = 'scheduler-new', fencing_token = %s, "
        "owner_expires_at_us = %s, owner_state = 'reconciling_takeover' "
        "WHERE pool_id = 'tts-realtime'",
        (token, expires),
    )


def service_for(
    ledger: SubmissionLedger, ingress: GatewayIngress, engine: ScriptedEngine | None = None
) -> SubmitExecutionService:
    engine = engine or ScriptedEngine()
    return SubmitExecutionService(
        ledger=ledger, ingress=ingress, dispatch=engine, observation=engine
    )


def test_barrier_persists_the_actual_minimum_and_fresh_inventory_without_budget_release(
    foundation: Foundation,
    stop_ledger: SubmissionLedger,
    ingress: GatewayIngress,
) -> None:
    seed_second_attempt(foundation)
    service = service_for(stop_ledger, ingress)
    service.submit(request_for(identity_for("lease-alt-stage-main", submit_intent_id="intent-a")))
    before = counters(foundation)
    takeover(foundation)
    snapshot = ingress.seal(REQUEST, cast(BarrierLedger, stop_ledger))
    # The foundation's existing submitting intent must also be enumerated.
    assert snapshot.submitting_intent_ids == ("submit-test",)
    assert snapshot.executing_intent_ids == ("intent-a",)
    assert snapshot.quarantined_intent_ids == ()
    assert counters(foundation) == before
    validator("internal/coordination-transactions.schema.json", "seal_barrier").validate(
        {
            "authority": {
                "pool_id": REQUEST.pool_id,
                "owner_id": REQUEST.new_owner_scheduler_id,
                "fencing_token": REQUEST.min_accepted_fencing_token,
            },
            "barrier": snapshot.response(),
            "evidence_digest": snapshot.evidence_digest,
        }
    )
    saved = foundation.database.query("SELECT * FROM gateway_fencing_barriers")
    assert saved[0]["min_fencing_token"] == 2
    assert saved[0]["sealed_evidence_digest"] == snapshot.evidence_digest
    assert saved[0]["version"] == 1
    # A newer observation refreshes inventory but preserves the durable barrier fact.
    service.record_stop(evidence_for(foundation))
    replay = ingress.seal(REQUEST, cast(BarrierLedger, stop_ledger))
    assert replay.version == snapshot.version and replay.evidence_digest == snapshot.evidence_digest
    assert replay.executing_intent_ids == ()
    assert foundation.database.query("SELECT * FROM gateway_fencing_barriers") == saved


def test_old_tokens_stay_blocked_after_replacing_the_process_guard(
    foundation: Foundation,
    stop_ledger: SubmissionLedger,
    tmp_path: Path,
) -> None:
    seed_second_attempt(foundation)
    takeover(foundation)
    owner = ProcessIngressOwnership(tmp_path, node_id="host-test", gateway_id="gateway-test")
    try:
        GatewayIngress(owner).seal(REQUEST, cast(BarrierLedger, stop_ledger))
    finally:
        owner.close()
    replacement = ProcessIngressOwnership(tmp_path, node_id="host-test", gateway_id="gateway-test")
    engine = ScriptedEngine()
    try:
        service = service_for(stop_ledger, GatewayIngress(replacement), engine)
        with pytest.raises(GatewayBarrierFencedError):
            service.submit(
                request_for(identity_for("lease-alt-stage-main", submit_intent_id="intent-stale"))
            )
        accepted = service.submit(
            request_for(
                identity_for(
                    "lease-alt-stage-alt",
                    submit_intent_id="intent-new",
                    stage_id="stage-alt",
                    fencing_token=2,
                )
            )
        )
        assert not accepted.idempotent_replay
        assert engine.dispatch_count == 1
    finally:
        replacement.close()


def test_barrier_waits_for_the_persisted_but_paused_dispatch_before_acknowledging(
    foundation: Foundation,
    stop_ledger: SubmissionLedger,
    ingress: GatewayIngress,
) -> None:
    seed_second_attempt(foundation)
    entered, resume = Event(), Event()

    def paused(_: object) -> None:
        entered.set()
        assert resume.wait(5)

    engine = ScriptedEngine(before_dispatch=paused)
    service = service_for(stop_ledger, ingress, engine)
    with ThreadPoolExecutor(max_workers=1) as workers:
        submitted = workers.submit(
            service.submit,
            request_for(identity_for("lease-alt-stage-main", submit_intent_id="intent-a")),
        )
        try:
            assert entered.wait(2)
            takeover(foundation)
            with pytest.raises(IngressUnavailableError):
                ingress.seal(REQUEST, cast(BarrierLedger, stop_ledger))
            assert (
                foundation.database.query("SELECT count(*) AS n FROM gateway_fencing_barriers")[0][
                    "n"
                ]
                == 0
            )
            if foundation.database.dialect == "postgresql":
                rows = foundation.database.query(
                    "SELECT count(*) AS n FROM pg_stat_activity "
                    "WHERE usename = %s AND state = 'idle in transaction'",
                    (foundation.gateway_role,),
                )
                assert rows[0]["n"] == 0  # External engine I/O holds no DB transaction.
        finally:
            resume.set()
        submitted.result(5)
    sealed = ingress.seal(REQUEST, cast(BarrierLedger, stop_ledger))
    assert sealed.executing_intent_ids == ("intent-a",)
    assert engine.dispatch_count == 1


@pytest.mark.parametrize(
    "bad",
    [
        {"new_owner_scheduler_id": "another-owner"},
        {"min_accepted_fencing_token": 3},
    ],
)
def test_a_foreign_owner_cannot_publish_a_barrier(
    foundation: Foundation,
    stop_ledger: SubmissionLedger,
    ingress: GatewayIngress,
    bad: dict[str, object],
) -> None:
    takeover(foundation)
    with pytest.raises(StaleFencingTokenError):
        ingress.seal(replace(REQUEST, **bad), cast(BarrierLedger, stop_ledger))  # type: ignore[arg-type]
    assert (
        foundation.database.query("SELECT count(*) AS n FROM gateway_fencing_barriers")[0]["n"] == 0
    )


def test_an_expired_owner_cannot_publish_a_barrier(
    foundation: Foundation,
    stop_ledger: SubmissionLedger,
    ingress: GatewayIngress,
) -> None:
    takeover(foundation, expires=1)
    with pytest.raises(StaleFencingTokenError):
        ingress.seal(REQUEST, cast(BarrierLedger, stop_ledger))
    assert (
        foundation.database.query("SELECT count(*) AS n FROM gateway_fencing_barriers")[0]["n"] == 0
    )


def test_barrier_minimum_and_version_are_monotonic(
    foundation: Foundation,
    stop_ledger: SubmissionLedger,
    ingress: GatewayIngress,
) -> None:
    takeover(foundation)
    first = ingress.seal(REQUEST, cast(BarrierLedger, stop_ledger))
    takeover(foundation, token=3)
    second = ingress.seal(
        replace(REQUEST, min_accepted_fencing_token=3), cast(BarrierLedger, stop_ledger)
    )
    assert second.version == first.version + 1
    assert second.min_fencing_token == 3
    with pytest.raises(StaleFencingTokenError):
        ingress.seal(REQUEST, cast(BarrierLedger, stop_ledger))
    assert (
        foundation.database.query("SELECT min_fencing_token FROM gateway_fencing_barriers")[0][
            "min_fencing_token"
        ]
        == 3
    )


def test_a_different_physical_node_cannot_claim_the_engine_ingress(
    foundation: Foundation,
    stop_ledger: SubmissionLedger,
    tmp_path: Path,
) -> None:
    takeover(foundation)
    owner = ProcessIngressOwnership(tmp_path, node_id="another-node", gateway_id="gateway-test")
    try:
        with pytest.raises(InvalidExecutionRequestError):
            GatewayIngress(owner).seal(REQUEST, cast(BarrierLedger, stop_ledger))
    finally:
        owner.close()
    assert (
        foundation.database.query("SELECT count(*) AS n FROM gateway_fencing_barriers")[0]["n"] == 0
    )


def test_persistence_failure_returns_no_sealed_fact_and_rolls_back(
    foundation: Foundation,
    stop_ledger: SubmissionLedger,
    ingress: GatewayIngress,
) -> None:
    takeover(foundation)
    foundation.database.query(
        "ALTER TABLE gateway_fencing_barriers ADD CONSTRAINT injected_barrier_failure "
        "CHECK (min_fencing_token < 2)"
    )
    with pytest.raises(SubmissionStoreError):
        ingress.seal(REQUEST, cast(BarrierLedger, stop_ledger))
    assert (
        foundation.database.query("SELECT count(*) AS n FROM gateway_fencing_barriers")[0]["n"] == 0
    )


def test_scheduler_activation_consumes_the_enforced_barrier_and_waits_for_stop(
    foundation: Foundation,
    stop_ledger: SubmissionLedger,
    ingress: GatewayIngress,
    owner_database: OwnerDatabase,
) -> None:
    seed_second_attempt(foundation)
    service = service_for(stop_ledger, ingress)
    service.submit(request_for(identity_for("lease-alt-stage-main", submit_intent_id="intent-a")))
    # The foundation's unrelated synthetic intent never dispatched; close only that fixture.
    foundation.database.query(
        "UPDATE submit_intents SET execution_state = 'failed', cleanup_state = 'not_required', "
        "error_code = 'engine_transport_unknown' WHERE submit_intent_id = 'submit-test'"
    )
    takeover(foundation)
    sealed = ingress.seal(REQUEST, cast(BarrierLedger, stop_ledger))
    assert sealed.executing_intent_ids == ("intent-a",)
    request = ActivateOwner(OwnerAuthority("tts-realtime", "scheduler-new", 2), expected_version=1)
    before = counters(foundation)
    blocked = owner_database.port.activate_owner(request)
    assert blocked.outcome == "rejected"
    assert counters(foundation) == before
    service.record_stop(evidence_for(foundation))
    active = owner_database.port.activate_owner(request)
    assert active.outcome == "committed"
    assert active.fact is not None and active.fact.owner_state == "active"
    assert counters(foundation) == before  # Activation does not release held budget.


def test_barrier_inventory_keeps_unknown_and_stopping_executions(
    foundation: Foundation,
    stop_ledger: SubmissionLedger,
    ingress: GatewayIngress,
) -> None:
    seed_second_attempt(foundation)
    engine = ScriptedEngine()
    engine.script_outcome(DispatchOutcome(DispatchResult.RESPONSE_LOST, datetime.now(UTC)))
    service = service_for(stop_ledger, ingress, engine)
    service.submit(request_for(identity_for("lease-alt-stage-main", submit_intent_id="intent-a")))
    stopping = service.submit(
        request_for(
            identity_for(
                "lease-alt-stage-alt",
                submit_intent_id="intent-b",
                stage_id="stage-alt",
                fencing_token=2,
            )
        )
    ).intent
    version = stopping.version
    stopping.mark_stopping(at=datetime.now(UTC))
    stop_ledger.save(stopping, expected_version=version)
    before = counters(foundation)
    takeover(foundation)
    sealed = ingress.seal(REQUEST, cast(BarrierLedger, stop_ledger))
    assert sealed.submitting_intent_ids == ("submit-test",)
    assert sealed.executing_intent_ids == ("intent-b",)
    assert sealed.quarantined_intent_ids == ("intent-a",)
    assert counters(foundation) == before


@pytest.mark.parametrize("database", ["postgresql"], indirect=True)
def test_lost_barrier_commit_ack_keeps_floor_and_replays_without_a_second_version(
    foundation: Foundation,
    ingress: GatewayIngress,
) -> None:
    pool = new_pool(foundation)
    try:
        takeover(foundation)
        real = pool.connection
        calls = 0

        @contextmanager
        def lose_ack(*args: Any, **kwargs: Any) -> Iterator[Any]:
            nonlocal calls
            calls += 1
            with real(*args, **kwargs) as connection:
                yield connection
            raise psycopg.OperationalError("synthetic lost commit acknowledgment")

        ledger = PostgresSubmissionLedger(pool)
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(pool, "connection", lose_ack)
            with pytest.raises(SubmissionOutcomeUnknownError):
                ingress.seal(REQUEST, ledger)
        assert calls == 1
        with (
            pytest.raises(GatewayBarrierFencedError),
            ingress.dispatch(identity_for("lease-test", submit_intent_id="intent-stale")),
        ):
            pytest.fail("an ambiguous commit reopened the old ingress")
        saved = foundation.database.query("SELECT * FROM gateway_fencing_barriers")
        assert saved[0]["version"] == 1
        recovered = ingress.seal(REQUEST, ledger)
        assert recovered.version == 1
        assert recovered.evidence_digest == saved[0]["sealed_evidence_digest"]
        assert foundation.database.query("SELECT * FROM gateway_fencing_barriers") == saved
    finally:
        pool.close()
