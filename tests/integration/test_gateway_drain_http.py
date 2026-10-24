"""Served instance drain on the real PostgreSQL ledger; no engine model is started.

The unit suite covers the drain rules against an in-memory ledger. Here the app runs on the
restricted gateway login and the real process ingress, so the tests cover what a deployed
gateway actually reads and writes: the recorded generation identity, the in-flight
inventory of that generation, and the guarantee that a drain releases no capacity.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from psycopg.conninfo import make_conninfo
from pydantic import SecretStr
from speechnexus_contracts.execution import parse
from tests.fake.execution import ScriptedAbort, ScriptedEngine
from tests.fake.readiness import StaticProbe
from tests.integration.test_coordination_foundation import Foundation
from tests.integration.test_coordination_foundation import foundation as foundation
from tests.integration.test_gateway_barrier import REQUEST, takeover
from tests.integration.test_gateway_stop_evidence import counters, evidence_for, new_pool
from tests.integration.test_gateway_submit_http import (
    TOKEN,
    LiveGateway,
    contract_command,
    post,
)
from tests.integration.test_gateway_submit_ledger import DIGEST, identity_for, seed_second_attempt
from tests.integration.test_production_jobs import database as database

from speechnexus_gateway.app import create_app
from speechnexus_gateway.application.abort_execution import ExecutionAbortService
from speechnexus_gateway.application.barrier_control import GatewayBarrierService
from speechnexus_gateway.application.ingress import GatewayIngress
from speechnexus_gateway.application.instance_drain import InstanceDrainService
from speechnexus_gateway.application.query_execution import ExecutionQueryService
from speechnexus_gateway.application.readiness import ReadinessService
from speechnexus_gateway.application.submit_execution import SubmitExecutionService
from speechnexus_gateway.domain.ports import AbortOutcome
from speechnexus_gateway.infrastructure.postgres_pool import GatewayPool
from speechnexus_gateway.infrastructure.process_ownership import ProcessIngressOwnership
from speechnexus_gateway.infrastructure.submit_postgres import PostgresSubmissionLedger
from speechnexus_gateway.settings import Settings

pytestmark = [
    pytest.mark.integration,
    # The drain reads the PostgreSQL ledger adapter; InnoDB keeps the shared schema oracle.
    pytest.mark.parametrize("database", ["postgresql"], indirect=True),
]

INSTANCE = "instance-test"
GENERATION = 1
LEASE = "lease-alt-stage-main"
DRAIN_PATH = f"/internal/v1/instances/{INSTANCE}/drain"
BARRIER_PATH = "/internal/v1/gateways/gateway-test/fencing-barrier"


def drain_body(**changes: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "host_node_id": "host-test",
        "pool_id": "tts-realtime",
        "instance_generation": GENERATION,
        "fencing_token": 1,
        "max_drain_wait_ms": 1000,
    }
    return body | changes


def barrier_body(**changes: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "pool_id": REQUEST.pool_id,
        "new_owner_scheduler_id": REQUEST.new_owner_scheduler_id,
        "min_accepted_fencing_token": REQUEST.min_accepted_fencing_token,
        "abort_in_flight": False,
    }
    return body | changes


def drain(gateway: LiveGateway, **changes: Any) -> Any:
    return post(gateway.app, DRAIN_PATH, drain_body(**changes))


def intent_rows(foundation: Foundation) -> list[dict[str, Any]]:
    return foundation.database.query(
        "SELECT submit_intent_id, execution_state, cleanup_state, version FROM submit_intents "
        "ORDER BY submit_intent_id"
    )


def stop_evidence_rows(foundation: Foundation) -> list[dict[str, Any]]:
    return foundation.database.query(
        "SELECT evidence_id, submit_intent_id FROM execution_stop_evidence ORDER BY evidence_id"
    )


def seed_idle_generation(foundation: Foundation) -> None:
    """A second recorded instance generation with no admitted work at all."""
    foundation.database.query(
        "INSERT INTO engine_instances (instance_id, instance_generation, host_node_id, pool_id, "
        "gateway_id, binding_revision_id, manifest_id, approved_template_id, management_mode, "
        "loading_policy, config_digest, instance_state, max_slots, held_slots) VALUES "
        "('instance-test', 2, 'host-test', 'tts-realtime', 'gateway-test', 'binding-test', "
        "'manifest-test', 'template-test', 'managed', 'on_demand', %s, 'ready', 2, 0)",
        (DIGEST,),
    )


def started_verified_stop(foundation: Foundation, submit_intent_id: str) -> AbortOutcome:
    evidence = evidence_for(
        foundation,
        evidence_id=f"evidence-{submit_intent_id}",
        submit_intent_id=submit_intent_id,
    )
    return AbortOutcome(observed_at=datetime.now(UTC), evidence=evidence)


@contextmanager
def open_gateway(
    foundation: Foundation, tmp_path: Path, *, abort_adapter: bool
) -> Iterator[LiveGateway]:
    seed_second_attempt(foundation)
    pool: GatewayPool = new_pool(foundation)
    owner = ProcessIngressOwnership(tmp_path, node_id="host-test", gateway_id="gateway-test")
    ingress = GatewayIngress(owner, timeout_seconds=0.1)
    ledger = PostgresSubmissionLedger(pool)
    engine = ScriptedEngine()
    abort = ScriptedAbort()
    abort_service = ExecutionAbortService(ingress=ingress, ledger=ledger, abort=abort)
    app = create_app(
        settings=Settings(
            control_database_url=SecretStr(
                make_conninfo(
                    foundation.database.url,
                    user=foundation.gateway_role,
                    password="synthetic_foundation_test",
                )
            ),
            control_schema=foundation.database.schema,
            node_id="host-test",
            gateway_id="gateway-test",
            component_token=SecretStr(TOKEN),
            ingress_lock_directory=tmp_path,
        ),
        barrier_control=GatewayBarrierService(ingress=ingress, ledger=ledger),
        execution_query=ExecutionQueryService(ingress=ingress, ledger=ledger),
        instance_drain=InstanceDrainService(
            ingress=ingress, ledger=ledger, abort=abort_service if abort_adapter else None
        ),
        submit_execution=SubmitExecutionService(
            ingress=ingress, ledger=ledger, dispatch=engine, observation=engine
        ),
        abort_execution=abort_service,
        readiness=ReadinessService([StaticProbe("control_postgresql")], timeout_seconds=1),
    )
    try:
        yield LiveGateway(app=app, ledger=ledger, engine=engine, abort=abort)
    finally:
        owner.close()
        pool.close()


@pytest.fixture
def gateway(foundation: Foundation, tmp_path: Path) -> Iterator[LiveGateway]:
    """The production shape today: no engine abort adapter, so a drain only reports."""
    with open_gateway(foundation, tmp_path, abort_adapter=False) as live:
        yield live


@pytest.fixture
def stopping_gateway(foundation: Foundation, tmp_path: Path) -> Iterator[LiveGateway]:
    """A drain allowed to attempt bounded stops through the abort service."""
    with open_gateway(foundation, tmp_path, abort_adapter=True) as live:
        yield live


def test_a_drain_reports_the_recorded_work_and_writes_nothing(
    foundation: Foundation, gateway: LiveGateway
) -> None:
    identity = identity_for(LEASE, submit_intent_id="intent-http")
    assert gateway.submit(identity, contract_command()).status_code == 200
    before = counters(foundation)
    rows_before = intent_rows(foundation)

    response = drain(gateway)

    assert response.status_code == 200
    body = parse("DrainInstanceResponse", response.json())
    assert body["instance_id"] == INSTANCE
    assert body["instance_generation"] == GENERATION
    # `submit-test` is the foundation's own recorded but never dispatched intent, so an
    # honest inventory must report it next to the intent this test submitted.
    assert body["submitting_intent_count"] == 1
    assert body["active_intent_count"] == 1
    assert body["quarantined_intent_count"] == 0
    assert body["drain_state"] == "draining"
    assert body["safe_to_stop_instance"] is False
    assert intent_rows(foundation) == rows_before
    assert stop_evidence_rows(foundation) == []
    assert counters(foundation) == before


def test_another_generation_node_or_pool_is_a_conflict(
    foundation: Foundation, gateway: LiveGateway
) -> None:
    rows_before = intent_rows(foundation)

    other_generation = drain(gateway, instance_generation=9)
    other_node = drain(gateway, host_node_id="host-other")
    other_pool = drain(gateway, pool_id="asr-batch")

    for response in (other_generation, other_node, other_pool):
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "instance_generation_mismatch"
    assert intent_rows(foundation) == rows_before


def test_a_recorded_generation_with_no_admitted_work_is_drained(
    foundation: Foundation, gateway: LiveGateway
) -> None:
    seed_idle_generation(foundation)
    rows_before = intent_rows(foundation)

    response = drain(gateway, instance_generation=2)

    assert response.status_code == 200, response.text
    body = parse("DrainInstanceResponse", response.json())
    assert body["instance_generation"] == 2
    assert body["drain_state"] == "drained"
    assert body["safe_to_stop_instance"] is True
    assert body["active_intent_count"] == 0
    assert body["submitting_intent_count"] == 0
    assert body["quarantined_intent_count"] == 0
    assert intent_rows(foundation) == rows_before
    assert stop_evidence_rows(foundation) == []


def test_a_sealed_barrier_makes_an_older_drain_token_stale(
    foundation: Foundation, gateway: LiveGateway
) -> None:
    takeover(foundation)
    sealed = post(gateway.app, BARRIER_PATH, barrier_body())
    assert sealed.status_code == 200

    stale = drain(gateway, fencing_token=1)
    current = drain(gateway, fencing_token=REQUEST.min_accepted_fencing_token)

    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "stale_fencing_token"
    assert current.status_code == 200
    assert current.json()["drain_state"] == "draining"


def test_a_verified_stop_closes_the_dispatched_intent_without_releasing_capacity(
    foundation: Foundation, stopping_gateway: LiveGateway
) -> None:
    gateway = stopping_gateway
    identity = identity_for(LEASE, submit_intent_id="intent-http")
    assert gateway.submit(identity, contract_command()).status_code == 200
    before = counters(foundation)
    gateway.abort.script_outcome(started_verified_stop(foundation, "intent-http"))

    response = drain(gateway)

    assert response.status_code == 200, response.text
    body = parse("DrainInstanceResponse", response.json())
    assert body["active_intent_count"] == 0
    assert body["quarantined_intent_count"] == 0
    # The foundation's own recorded intent was never dispatched, so no engine abort can
    # stop it: it keeps holding capacity and the drain stays unfinished.
    assert body["submitting_intent_count"] == 1
    assert body["drain_state"] == "draining"
    assert body["safe_to_stop_instance"] is False
    assert stop_evidence_rows(foundation) == [
        {"evidence_id": "evidence-intent-http", "submit_intent_id": "intent-http"}
    ]
    rows = {row["submit_intent_id"]: row for row in intent_rows(foundation)}
    assert rows["intent-http"]["execution_state"] == "aborted"
    assert rows["intent-http"]["cleanup_state"] == "complete"
    assert rows["submit-test"]["execution_state"] == "submitting"
    # The Scheduler still owns budget release: a verified stop publishes no capacity.
    assert counters(foundation) == before


def test_an_unverified_stop_quarantines_the_generation_and_keeps_holding_capacity(
    foundation: Foundation, stopping_gateway: LiveGateway
) -> None:
    gateway = stopping_gateway
    identity = identity_for(LEASE, submit_intent_id="intent-http")
    assert gateway.submit(identity, contract_command()).status_code == 200
    before = counters(foundation)

    # The scripted adapter verifies nothing about the engine by default.
    response = drain(gateway)

    assert response.status_code == 200
    body = parse("DrainInstanceResponse", response.json())
    assert body["drain_state"] == "quarantined"
    assert body["safe_to_stop_instance"] is False
    assert body["quarantined_intent_count"] == 1
    assert body["active_intent_count"] == 0
    assert body["submitting_intent_count"] == 1
    assert stop_evidence_rows(foundation) == []
    states = {row["submit_intent_id"]: row["execution_state"] for row in intent_rows(foundation)}
    assert states == {
        "intent-http": "unknown_quarantined",
        "submit-test": "submitting",
    }
    assert counters(foundation) == before
