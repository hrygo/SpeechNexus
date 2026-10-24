"""Served submit/abort routes on the real PostgreSQL ledger; no engine model is started.

The unit suite covers the same two routes against an in-memory ledger. Here the app is
wired to the restricted gateway login and the real process ingress, so the tests cover
what a deployed gateway actually persists: one intent row, at most one dispatch, an
honest quarantine when nothing was verified and durable stop evidence when it was.
"""

from __future__ import annotations

import asyncio
import copy
import json
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from psycopg.conninfo import make_conninfo
from pydantic import SecretStr
from speechnexus_contracts.execution import parse
from tests.fake.execution import ScriptedAbort, ScriptedEngine
from tests.fake.readiness import StaticProbe
from tests.integration.test_coordination_foundation import Foundation
from tests.integration.test_coordination_foundation import foundation as foundation
from tests.integration.test_gateway_stop_evidence import counters, evidence_for, new_pool
from tests.integration.test_gateway_submit_ledger import identity_for, seed_second_attempt
from tests.integration.test_production_jobs import database as database

from speechnexus_gateway.app import create_app
from speechnexus_gateway.application.abort_execution import ExecutionAbortService
from speechnexus_gateway.application.barrier_control import GatewayBarrierService
from speechnexus_gateway.application.ingress import GatewayIngress
from speechnexus_gateway.application.instance_drain import InstanceDrainService
from speechnexus_gateway.application.query_execution import ExecutionQueryService
from speechnexus_gateway.application.readiness import ReadinessService
from speechnexus_gateway.application.submit_execution import SubmitExecutionService
from speechnexus_gateway.domain.canonical import JsonValue, command_digest
from speechnexus_gateway.domain.ports import AbortOutcome
from speechnexus_gateway.domain.submission import (
    SubmissionIdentity,
    derived_output_stream_id,
)
from speechnexus_gateway.infrastructure.postgres_pool import GatewayPool
from speechnexus_gateway.infrastructure.process_ownership import ProcessIngressOwnership
from speechnexus_gateway.infrastructure.submit_postgres import PostgresSubmissionLedger
from speechnexus_gateway.settings import Settings

pytestmark = [
    pytest.mark.integration,
    # The submit/abort ledger adapter is PostgreSQL; InnoDB keeps the shared schema oracle.
    pytest.mark.parametrize("database", ["postgresql"], indirect=True),
]

ROOT = Path(__file__).resolve().parents[2]
TOKEN = "synthetic-http-gateway-component-token"
HEADERS = {"Authorization": f"Bearer {TOKEN}", "X-Request-ID": "synthetic-submit-http"}
SUBMIT_PATH = "/internal/v1/executions/submit"
ABORT_PATH = "/internal/v1/executions/{submit_intent_id}/abort"
LEASE = "lease-alt-stage-main"


def contract_command(index: int = 0) -> JsonValue:
    """One frozen command example: the served route validates the real command schema."""
    schema = json.loads((ROOT / "contracts/internal/execution-command.schema.json").read_text())
    return dict(copy.deepcopy(schema["examples"][index]))


def submit_body(
    identity: SubmissionIdentity,
    command: JsonValue,
    **changes: Any,
) -> dict[str, Any]:
    return {
        "submit_intent_id": identity.submit_intent_id,
        "lease_id": identity.lease_id,
        "operation_id": identity.operation_id,
        "attempt_id": identity.attempt_id,
        "stage_id": identity.stage_id,
        "workspace_id": identity.workspace_id,
        "pool_id": identity.pool_id,
        "host_node_id": identity.host_node_id,
        "gateway_id": identity.gateway_id,
        "instance_id": identity.instance_id,
        "instance_generation": identity.instance_generation,
        "binding_revision_id": identity.binding_revision_id,
        "policy_revision_id": identity.policy_revision_id,
        "voice_revision_id": identity.voice_revision_id,
        "fencing_token": identity.fencing_token,
        "command_digest": command_digest(command),
        "command": command,
        "deadline_at": identity.deadline_at.isoformat(),
        "max_output_audio_ms": identity.max_output_audio_ms,
        "sample_rate_hz": identity.sample_rate_hz,
    } | changes


def abort_body(**changes: Any) -> dict[str, Any]:
    return {
        "operation_id": "operation-test",
        "attempt_id": "attempt-alt",
        "fencing_token": 1,
        "reason": "client_cancelled",
        "wait_timeout_ms": 1000,
    } | changes


def post(
    app: FastAPI,
    path: str,
    body: dict[str, Any],
    *,
    headers: dict[str, str] = HEADERS,
) -> httpx.Response:
    async def call() -> httpx.Response:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://gateway") as client:
            return await client.post(path, json=body, headers=headers)

    return asyncio.run(call())


@dataclass
class LiveGateway:
    """One app on the real ledger plus the doubles the routes dispatch through."""

    app: FastAPI
    ledger: PostgresSubmissionLedger
    engine: ScriptedEngine
    abort: ScriptedAbort

    def submit(self, identity: SubmissionIdentity, command: JsonValue, **changes: Any) -> Any:
        return post(self.app, SUBMIT_PATH, submit_body(identity, command, **changes))

    def abort_intent(self, submit_intent_id: str, **changes: Any) -> Any:
        return post(
            self.app,
            ABORT_PATH.format(submit_intent_id=submit_intent_id),
            abort_body(**changes),
        )


@pytest.fixture
def gateway(foundation: Foundation, tmp_path: Path) -> Iterator[LiveGateway]:
    seed_second_attempt(foundation)
    pool: GatewayPool = new_pool(foundation)
    owner = ProcessIngressOwnership(tmp_path, node_id="host-test", gateway_id="gateway-test")
    ingress = GatewayIngress(owner, timeout_seconds=0.1)
    ledger = PostgresSubmissionLedger(pool)
    engine = ScriptedEngine()
    abort = ScriptedAbort()
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
        instance_drain=InstanceDrainService(ingress=ingress, ledger=ledger),
        submit_execution=SubmitExecutionService(
            ingress=ingress, ledger=ledger, dispatch=engine, observation=engine
        ),
        abort_execution=ExecutionAbortService(ingress=ingress, ledger=ledger, abort=abort),
        readiness=ReadinessService([StaticProbe("control_postgresql")], timeout_seconds=1),
    )
    try:
        yield LiveGateway(app=app, ledger=ledger, engine=engine, abort=abort)
    finally:
        owner.close()
        pool.close()


def test_submit_persists_one_durable_intent_and_never_dispatches_twice(
    foundation: Foundation, gateway: LiveGateway
) -> None:
    identity = identity_for(LEASE, submit_intent_id="intent-http")
    command = contract_command()

    first = gateway.submit(identity, command)
    replay = gateway.submit(identity, command)

    assert first.status_code == replay.status_code == 200
    parse("SubmitExecutionResponse", first.json())
    assert first.json()["execution_state"] == "executing"
    assert first.json()["cleanup_state"] == "pending"
    assert first.json()["idempotent_replay"] is False
    assert first.json()["dispatched_at"] is not None
    assert replay.json()["idempotent_replay"] is True
    assert replay.json()["recorded_at"] == first.json()["recorded_at"]
    assert gateway.engine.dispatch_count == 1

    rows = foundation.database.query(
        "SELECT submit_intent_id, request_digest, output_stream_id, sample_rate_hz, "
        "hard_output_ceiling_samples, execution_state, cleanup_state, dispatched_at_us, version "
        "FROM submit_intents WHERE submit_intent_id = 'intent-http'"
    )
    assert len(rows) == 1
    derived = derived_output_stream_id("operation-test", "attempt-alt")
    assert rows[0]["output_stream_id"] == derived
    assert rows[0]["sample_rate_hz"] == 24000
    assert rows[0]["hard_output_ceiling_samples"] == 48000
    assert rows[0]["execution_state"] == "executing"
    assert rows[0]["cleanup_state"] == "pending"
    assert rows[0]["dispatched_at_us"] is not None
    assert rows[0]["version"] == 2
    heads = foundation.database.query(
        "SELECT output_stream_id, sample_rate_hz FROM output_stream_heads "
        "WHERE operation_id = 'operation-test' AND attempt_id = 'attempt-alt'"
    )
    assert heads == [{"output_stream_id": derived, "sample_rate_hz": 24000}]


def test_a_conflicting_command_and_a_mismatched_digest_never_reach_the_engine(
    foundation: Foundation, gateway: LiveGateway
) -> None:
    identity = identity_for(LEASE, submit_intent_id="intent-http")
    command = contract_command()
    other = contract_command(1)
    assert gateway.submit(identity, command).status_code == 200

    conflict = gateway.submit(identity, other)
    mismatched = gateway.submit(
        identity_for(LEASE, submit_intent_id="intent-http-two"),
        command,
        command_digest=command_digest(other),
    )

    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "submit_intent_conflict"
    assert mismatched.status_code == 422
    assert mismatched.json()["error"]["code"] == "invalid_execution_request"
    assert gateway.engine.dispatch_count == 1
    stored = foundation.database.query(
        "SELECT submit_intent_id FROM submit_intents WHERE lease_id = %s", (LEASE,)
    )
    assert stored == [{"submit_intent_id": "intent-http"}]


def test_an_unverified_abort_quarantines_the_execution_and_keeps_its_capacity(
    foundation: Foundation, gateway: LiveGateway
) -> None:
    identity = identity_for(LEASE, submit_intent_id="intent-http")
    assert gateway.submit(identity, contract_command()).status_code == 200
    before = counters(foundation)

    # The adapter's default answer is honest: this abort verified nothing about the engine.
    response = gateway.abort_intent("intent-http")

    assert response.status_code == 200
    parse("ExecutionStatusResponse", response.json())
    assert response.json()["execution_state"] == "unknown_quarantined"
    assert response.json()["error_code"] == "cancellation_unconfirmed"
    assert response.json()["cleanup_state"] == "quarantined"
    assert gateway.abort.calls == [("intent-http", "client_cancelled", 1000)]
    assert counters(foundation) == before
    stored = foundation.database.query(
        "SELECT execution_state, cleanup_state, terminated_at_us FROM submit_intents "
        "WHERE submit_intent_id = 'intent-http'"
    )
    assert stored == [
        {
            "execution_state": "unknown_quarantined",
            "cleanup_state": "quarantined",
            "terminated_at_us": None,
        }
    ]
    assert (
        foundation.database.query(
            "SELECT evidence_id FROM execution_stop_evidence WHERE submit_intent_id = 'intent-http'"
        )
        == []
    )


def test_verified_abort_evidence_closes_the_execution_without_releasing_budgets(
    foundation: Foundation, gateway: LiveGateway
) -> None:
    identity = identity_for(LEASE, submit_intent_id="intent-http")
    assert gateway.submit(identity, contract_command()).status_code == 200
    before = counters(foundation)
    evidence = evidence_for(foundation, submit_intent_id="intent-http")
    gateway.abort.script_outcome(AbortOutcome(observed_at=datetime.now(UTC), evidence=evidence))

    first = gateway.abort_intent("intent-http")
    replay = gateway.abort_intent("intent-http")

    assert first.status_code == replay.status_code == 200
    parse("ExecutionStatusResponse", first.json())
    assert first.json()["execution_state"] == "aborted"
    assert first.json()["cleanup_state"] == "complete"
    assert first.json()["stop_evidence_kind"] == evidence.evidence_kind.value
    assert first.json()["updated_at"] is not None
    assert replay.json() == first.json()
    # A closed execution is reported from its recorded facts, so the engine is asked once.
    assert len(gateway.abort.calls) == 1
    saved = foundation.database.query(
        "SELECT s.evidence_id, s.observed_at_us, s.execution_stopped, s.resources_released, "
        "i.execution_state, i.cleanup_state, i.terminated_at_us FROM execution_stop_evidence s "
        "JOIN submit_intents i USING (submit_intent_id) WHERE s.submit_intent_id = 'intent-http'"
    )
    assert saved == [
        {
            "evidence_id": evidence.evidence_id,
            "observed_at_us": evidence.observed_at_us,
            "execution_stopped": 1,
            "resources_released": 1,
            "execution_state": "aborted",
            "cleanup_state": "complete",
            "terminated_at_us": evidence.observed_at_us,
        }
    ]
    # Releasing capacity stays with the Scheduler: the gateway recorded the stop facts only.
    assert counters(foundation) == before


def test_submit_refuses_without_the_component_credential(
    foundation: Foundation, gateway: LiveGateway
) -> None:
    response = post(
        gateway.app,
        SUBMIT_PATH,
        submit_body(identity_for(LEASE, submit_intent_id="intent-http"), contract_command()),
        headers={},
    )

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "component_credential_invalid"
    assert gateway.engine.dispatch_count == 0
    assert (
        foundation.database.query(
            "SELECT submit_intent_id FROM submit_intents WHERE submit_intent_id = 'intent-http'"
        )
        == []
    )
