"""Real gateway HTTP and ledger; the engine adapter returns unverified stop."""

import asyncio

import httpx
import pytest
from pydantic import SecretStr
from tests.integration.test_coordination_foundation import Foundation
from tests.integration.test_coordination_foundation import foundation as foundation
from tests.integration.test_gateway_submit_http import (
    LEASE,
    TOKEN,
    LiveGateway,
    abort_body,
    contract_command,
    post,
)
from tests.integration.test_gateway_submit_http import gateway as gateway
from tests.integration.test_gateway_submit_ledger import identity_for
from tests.integration.test_production_jobs import database as database

from speechnexus_backend.speech.domain.binding_disable_intent import DisableExecutionTarget
from speechnexus_backend.speech.infrastructure.gateway_abort_intent_schema import (
    gateway_abort_grants,
)
from speechnexus_backend.speech.infrastructure.gateway_execution_client import (
    GatewayExecutionClient,
)
from speechnexus_gateway.domain.abort_intent import AbortExecutionRequest
from speechnexus_gateway.domain.canonical import command_digest
from speechnexus_gateway.infrastructure.abort_authority import verify_abort_authority

pytestmark = [
    pytest.mark.integration,
    pytest.mark.parametrize("database", ["postgresql"], indirect=True),
]


def test_abort_client_reaches_gateway_and_preserves_unknown_capacity(
    foundation: Foundation, gateway: LiveGateway
) -> None:
    for statement in gateway_abort_grants(foundation.database.schema, foundation.gateway_role):
        foundation.database.query(statement)
    assert verify_abort_authority(gateway.ledger._pool, foundation.database.schema)
    gateway.app.state.abort_execution._durable_abort_enabled = True
    identity = identity_for(LEASE, submit_intent_id="intent-http")
    command = contract_command()
    assert gateway.submit(identity, command).status_code == 200
    target = DisableExecutionTarget(
        submit_intent_id=identity.submit_intent_id,
        operation_id=identity.operation_id,
        attempt_id=identity.attempt_id,
        lease_id=identity.lease_id,
        stage_id=identity.stage_id,
        workspace_id=identity.workspace_id,
        pool_id=identity.pool_id,
        gateway_id=identity.gateway_id,
        host_node_id=identity.host_node_id,
        instance_id=identity.instance_id,
        instance_generation=identity.instance_generation,
        binding_revision_id=identity.binding_revision_id,
        policy_revision_id=identity.policy_revision_id,
        voice_revision_id=identity.voice_revision_id,
        fencing_token=identity.fencing_token,
        command_digest=command_digest(command),
        wait_timeout_ms=1000,
    )

    async def call() -> None:
        async with GatewayExecutionClient(
            base_url="http://gateway",
            credential=SecretStr(TOKEN),
            timeout_seconds=3,
            max_retries=0,
            backoff_seconds=0,
            _transport=httpx.ASGITransport(app=gateway.app),
        ) as client:
            result = await client.abort_idempotent(target)
            assert result["execution_state"] == "unknown_quarantined"
            assert result["cleanup_state"] == "quarantined"
            recovered = await client.query_disable_target(target)
            assert recovered["execution_state"] == "unknown_quarantined"
            replayed = await client.abort_idempotent(target)
            assert replayed["execution_state"] == "unknown_quarantined"

    asyncio.run(call())
    assert gateway.abort.calls == [("intent-http", "resource_force_disabled", 1000)]
    assert (
        foundation.database.query(
            "SELECT count(*) AS n FROM gateway_abort_intents WHERE submit_intent_id=%s",
            ("intent-http",),
        )[0]["n"]
        == 1
    )
    lease = foundation.database.query(
        "SELECT lease_state, cleanup_state FROM admission_leases WHERE lease_id = %s", (LEASE,)
    )[0]
    assert lease["lease_state"] != "released"
    foundation.database.query(
        f'GRANT UPDATE (reason) ON gateway_abort_intents TO "{foundation.gateway_role}"'
    )
    assert not verify_abort_authority(gateway.ledger._pool, foundation.database.schema)


@pytest.mark.parametrize("crash_after", ["receipt", "claim"])
def test_gateway_recovers_original_abort_after_commit_boundary(
    foundation: Foundation, gateway: LiveGateway, crash_after: str
) -> None:
    for statement in gateway_abort_grants(foundation.database.schema, foundation.gateway_role):
        foundation.database.query(statement)
    assert verify_abort_authority(gateway.ledger._pool, foundation.database.schema)
    gateway.app.state.abort_execution._durable_abort_enabled = True
    identity = identity_for(LEASE, submit_intent_id="intent-http")
    assert gateway.submit(identity, contract_command()).status_code == 200
    request = AbortExecutionRequest(
        "intent-http", "operation-test", "attempt-alt", 1, "resource_force_disabled", 1000
    )
    gateway.ledger.receive_abort(request)
    if crash_after == "claim":
        assert gateway.ledger.claim_abort(request) is not None
    path = "/internal/v1/executions/intent-http/abort-intents"
    payload = abort_body(reason="resource_force_disabled")
    response = post(gateway.app, path, payload)
    assert response.status_code == 200
    assert response.json()["execution_state"] == "unknown_quarantined"
    expected_calls = 1 if crash_after == "receipt" else 0
    assert len(gateway.abort.calls) == expected_calls
    assert post(gateway.app, path, payload).status_code == 200
    assert len(gateway.abort.calls) == expected_calls
    conflict = post(gateway.app, path, {**payload, "wait_timeout_ms": 2000})
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "control_state_conflict"
    wrong_fence = post(gateway.app, path, {**payload, "fencing_token": 2})
    assert wrong_fence.status_code == 409
    assert len(gateway.abort.calls) == expected_calls


def test_gateway_without_durable_authority_fails_closed(gateway: LiveGateway) -> None:
    identity = identity_for(LEASE, submit_intent_id="intent-http")
    assert gateway.submit(identity, contract_command()).status_code == 200
    response = post(
        gateway.app,
        "/internal/v1/executions/intent-http/abort-intents",
        abort_body(reason="resource_force_disabled"),
    )
    assert response.status_code == 503
    assert gateway.abort.calls == []
