"""A disable abort uses its fixed identity and never retries an unknown POST."""

import json

import httpx
import pytest

from speechnexus_backend.speech.domain.binding_disable_intent import DisableExecutionTarget
from speechnexus_backend.speech.infrastructure.gateway_execution_client import (
    GatewayRejectedError,
    GatewayResponseError,
    GatewayTimeoutError,
)
from tests.unit.backend.test_gateway_execution_client import client, status_response


def _target() -> DisableExecutionTarget:
    return DisableExecutionTarget(
        submit_intent_id="intent-1",
        operation_id="operation-1",
        attempt_id="attempt-1",
        lease_id="lease-1",
        stage_id="stage-1",
        workspace_id="workspace-1",
        pool_id="asr-batch",
        gateway_id="gateway-1",
        host_node_id="node-1",
        instance_id="instance-1",
        instance_generation=1,
        binding_revision_id="binding-1",
        policy_revision_id="policy-1",
        voice_revision_id=None,
        fencing_token=7,
        command_digest="sha256:" + "a" * 64,
        wait_timeout_ms=1000,
    )


async def test_abort_uses_fixed_identity_and_keeps_unconfirmed_status() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=status_response())

    async with client(handler) as gateway:
        result = await gateway.abort(_target())
    assert result["execution_state"] == "executing"
    assert result["cleanup_state"] == "pending"
    assert len(requests) == 1
    assert requests[0].url.path == "/internal/v1/executions/intent-1/abort"
    assert json.loads(requests[0].content) == _target().abort_payload()


async def test_abort_rejects_another_execution_identity() -> None:
    response = status_response()
    response["fencing_token"] = 8
    async with client(lambda _: httpx.Response(200, json=response)) as gateway:
        with pytest.raises(GatewayResponseError) as error:
            await gateway.abort(_target())
    assert error.value.request_may_have_run


async def test_abort_timeout_is_unknown_and_not_retried() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ReadTimeout("synthetic timeout", request=request)

    async with client(handler, max_retries=4) as gateway:
        with pytest.raises(GatewayTimeoutError) as error:
            await gateway.abort(_target())
    assert calls == 1
    assert error.value.request_may_have_run


async def test_missing_durable_abort_endpoint_never_falls_back_to_legacy() -> None:
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        return httpx.Response(404, json={"detail": "Not Found"})

    async with client(handler) as gateway:
        with pytest.raises(GatewayRejectedError):
            await gateway.abort_idempotent(_target())
    assert paths == ["/internal/v1/executions/intent-1/abort-intents"]
