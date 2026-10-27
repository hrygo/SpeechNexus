"""Transport-only gateway client: exact bodies, five failure classes and safe reads."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
from pydantic import SecretStr, ValidationError
from speechnexus_contracts.execution import parse
from speechnexus_contracts.execution_models import (
    ArtifactStorageReference,
    SubmitExecutionRequest,
)

from speechnexus_backend.settings import Settings
from speechnexus_backend.speech.infrastructure.gateway_execution_client import (
    GatewayConnectionError,
    GatewayExecutionClient,
    GatewayRejectedError,
    GatewayResponseError,
    GatewayResultObjectError,
    GatewayServerError,
    GatewayTimeoutError,
    MinioExecutionResultObjects,
)
from tests.unit.backend.test_health import _settings

ROOT = Path(__file__).resolve().parents[3]
TOKEN = "synthetic-gateway-component-token"
DIGEST = "sha256:" + "a" * 64


def contract_command() -> dict[str, Any]:
    schema = json.loads((ROOT / "contracts/internal/execution-command.schema.json").read_text())
    return cast(dict[str, Any], copy.deepcopy(schema["examples"][0]))


def submit_request() -> SubmitExecutionRequest:
    request = cast(
        SubmitExecutionRequest,
        {
            "submit_intent_id": "intent-1",
            "lease_id": "lease-1",
            "operation_id": "operation-1",
            "attempt_id": "attempt-1",
            "stage_id": "stage-1",
            "workspace_id": "workspace-1",
            "pool_id": "asr-batch",
            "host_node_id": "node-1",
            "gateway_id": "gateway-1",
            "instance_id": "instance-1",
            "instance_generation": 1,
            "binding_revision_id": "binding-1",
            "policy_revision_id": "policy-1",
            "voice_revision_id": None,
            "fencing_token": 7,
            "command_digest": DIGEST,
            "command": contract_command(),
            "deadline_at": "2026-10-27T15:00:00+00:00",
            "max_output_audio_ms": None,
            "sample_rate_hz": None,
        },
    )
    parse("SubmitExecutionRequest", request)
    return request


def submit_response() -> dict[str, Any]:
    return {
        "submit_intent_id": "intent-1",
        "lease_id": "lease-1",
        "operation_id": "operation-1",
        "attempt_id": "attempt-1",
        "stage_id": "stage-1",
        "workspace_id": "workspace-1",
        "pool_id": "asr-batch",
        "host_node_id": "node-1",
        "gateway_id": "gateway-1",
        "instance_id": "instance-1",
        "instance_generation": 1,
        "binding_revision_id": "binding-1",
        "policy_revision_id": "policy-1",
        "voice_revision_id": None,
        "fencing_token": 7,
        "command_digest": DIGEST,
        "deadline_at": "2026-10-27T15:00:00+00:00",
        "dispatched_at": "2026-10-27T14:59:00+00:00",
        "sample_rate_hz": None,
        "execution_state": "executing",
        "cleanup_state": "pending",
        "idempotent_replay": False,
        "recorded_at": "2026-10-27T14:59:00+00:00",
    }


def status_response() -> dict[str, Any]:
    response = submit_response()
    del response["idempotent_replay"]
    del response["recorded_at"]
    response.update(
        stop_evidence_kind="none",
        updated_at="2026-10-27T14:59:01+00:00",
    )
    return response


def error_envelope(code: str, *, retryable: bool, recovery: str | None = None) -> dict[str, Any]:
    error: dict[str, Any] = {
        "code": code,
        "message": "synthetic gateway refusal",
        "retryable": retryable,
    }
    if recovery is not None:
        error["recovery"] = recovery
    return {"error": error}


def client(
    handler: Any,
    *,
    timeout_seconds: float = 1.0,
    max_retries: int = 2,
) -> GatewayExecutionClient:
    return GatewayExecutionClient(
        base_url="http://gateway.test",
        credential=SecretStr(TOKEN),
        timeout_seconds=timeout_seconds,
        max_retries=max_retries,
        backoff_seconds=0.0,
        _transport=httpx.MockTransport(handler),
    )


async def test_submit_posts_the_exact_contract_body_and_parses_the_response() -> None:
    request = submit_request()
    response = submit_response()
    calls: list[httpx.Request] = []

    def handler(sent: httpx.Request) -> httpx.Response:
        calls.append(sent)
        return httpx.Response(200, json=response)

    async with client(handler) as gateway:
        result = await gateway.submit(request)

    assert result == response
    assert len(calls) == 1
    sent = calls[0]
    assert sent.method == "POST"
    assert str(sent.url) == "http://gateway.test/internal/v1/executions/submit"
    assert sent.headers["authorization"] == "Bearer " + TOKEN
    assert sent.headers["content-type"] == "application/json"
    assert sent.headers["accept-encoding"] == "identity"
    assert sent.headers["x-request-id"]
    expected = json.dumps(
        request, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    ).encode()
    assert sent.content == expected
    assert json.loads(sent.content) == request


async def test_connection_failure_is_classified_and_submit_is_not_retried() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ConnectError("synthetic connect failure", request=request)

    async with client(handler, max_retries=4) as gateway:
        with pytest.raises(GatewayConnectionError) as caught:
            await gateway.submit(submit_request())

    error = caught.value
    assert error.kind == "connection"
    assert error.request_may_have_run is False
    assert calls == 1
    assert TOKEN not in str(error) and TOKEN not in repr(error)


async def test_a_4xx_answer_is_a_semantic_rejection_without_retry() -> None:
    calls = 0
    envelope = error_envelope(
        "submit_intent_conflict", retryable=False, recovery="query_current_authority"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(409, json=envelope)

    async with client(handler, max_retries=4) as gateway:
        with pytest.raises(GatewayRejectedError) as caught:
            await gateway.submit(submit_request())

    error = caught.value
    assert error.kind == "rejected"
    assert error.status_code == 409
    assert error.gateway_code == "submit_intent_conflict"
    assert error.recovery == "query_current_authority"
    assert error.retryable is False
    assert error.request_may_have_run is True
    assert calls == 1
    assert TOKEN not in str(error)


async def test_a_5xx_answer_is_a_server_failure_and_submit_is_not_retried() -> None:
    calls = 0
    envelope = error_envelope(
        "control_store_unavailable", retryable=True, recovery="retry_transaction"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(503, json=envelope)

    async with client(handler, max_retries=4) as gateway:
        with pytest.raises(GatewayServerError) as caught:
            await gateway.submit(submit_request())

    error = caught.value
    assert error.kind == "server"
    assert error.status_code == 503
    assert error.gateway_code == "control_store_unavailable"
    assert error.recovery == "retry_transaction"
    assert error.retryable is True
    assert calls == 1


async def test_a_deadline_is_classified_as_timeout_without_submit_retry() -> None:
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.2)
        return httpx.Response(200, json=submit_response())

    async with client(handler, timeout_seconds=0.01, max_retries=4) as gateway:
        with pytest.raises(GatewayTimeoutError) as caught:
            await gateway.submit(submit_request())

    error = caught.value
    assert error.kind == "timeout"
    assert error.request_may_have_run is True
    assert calls == 1
    assert TOKEN not in str(error)


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(
            200,
            content=b"not-json",
            headers={"content-type": "application/json"},
        ),
        httpx.Response(200, json={"submit_intent_id": "intent-1"}),
    ],
)
async def test_a_success_body_outside_the_schema_is_a_protocol_error(
    response: httpx.Response,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return response

    async with client(handler) as gateway:
        with pytest.raises(GatewayResponseError) as caught:
            await gateway.submit(submit_request())

    assert caught.value.kind == "invalid_response"
    assert caught.value.request_may_have_run is True


async def test_query_retries_a_transient_5xx_and_reads_the_original_identity() -> None:
    calls: list[httpx.Request] = []
    envelope = error_envelope(
        "control_store_unavailable", retryable=True, recovery="retry_transaction"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(503, json=envelope)
        return httpx.Response(200, json=status_response())

    async with client(handler, max_retries=1) as gateway:
        result = await gateway.query("intent-1")

    assert result == status_response()
    assert len(calls) == 2
    assert all(call.method == "GET" for call in calls)
    assert all(
        str(call.url) == "http://gateway.test/internal/v1/executions/intent-1" for call in calls
    )
    assert all(call.headers["authorization"] == "Bearer " + TOKEN for call in calls)


async def test_staging_result_reads_the_private_receipt_path() -> None:
    calls: list[httpx.Request] = []
    receipt = {
        "submit_intent_id": "intent-1",
        "request_digest": "sha256:" + "b" * 64,
        "staging_result": None,
    }

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json=receipt)

    async with client(handler) as gateway:
        result = await gateway.staging_result("intent-1")

    assert result == receipt
    assert str(calls[0].url) == (
        "http://gateway.test/internal/v1/executions/intent-1/staging-result"
    )


async def test_from_settings_requires_a_paired_credential_and_hides_it() -> None:
    base = _settings()
    with pytest.raises(ValueError):
        GatewayExecutionClient.from_settings(base)
    with pytest.raises(ValidationError):
        Settings(**(base.model_dump() | {"execution_gateway_base_url": "https://gateway.test"}))

    configured = Settings(
        **(
            base.model_dump()
            | {
                "execution_gateway_base_url": "https://gateway.test",
                "execution_gateway_component_credential": SecretStr(TOKEN),
            }
        )
    )
    gateway = GatewayExecutionClient.from_settings(configured)
    try:
        assert TOKEN not in repr(gateway)
    finally:
        await gateway.aclose()


class _FakeObjectResponse:
    def __init__(
        self,
        body: bytes,
        *,
        version: str,
        content_range: str | None = None,
        content_length: str | None = None,
    ) -> None:
        self.status = 206
        self._body = body
        self.headers = {
            "x-amz-version-id": version,
            "content-range": content_range or f"bytes 0-{len(body) - 1}/{len(body)}",
            "content-length": content_length or str(len(body)),
        }
        self.closed = False
        self.released = False

    def read(self, size: int) -> bytes:
        return self._body[:size]

    def close(self) -> None:
        self.closed = True

    def release_conn(self) -> None:
        self.released = True


class _FakeObjectClient:
    def __init__(self, response: _FakeObjectResponse) -> None:
        self.response = response
        self.calls: list[tuple[str, str, int, int, str]] = []

    def get_object(
        self,
        bucket: str,
        object_name: str,
        *,
        offset: int,
        length: int,
        version_id: str,
    ) -> _FakeObjectResponse:
        self.calls.append((bucket, object_name, offset, length, version_id))
        return self.response


def result_reference(body: bytes, *, bucket: str = "test") -> ArtifactStorageReference:
    return cast(
        ArtifactStorageReference,
        {
            "asset_or_artifact_id": "result-1",
            "workspace_id": "workspace-1",
            "storage_bucket": bucket,
            "object_key": "private/execution-results/result-1/transcription.json",
            "content_sha256": "sha256:" + hashlib.sha256(body).hexdigest(),
            "byte_length": len(body),
        },
    )


def result_reader(fake: _FakeObjectClient) -> MinioExecutionResultObjects:
    return MinioExecutionResultObjects(
        endpoint="http://127.0.0.1:9000",
        bucket="test",
        access_key="synthetic-access",
        secret_key="synthetic-secret",
        _client=fake,
    )


def test_result_reader_verifies_the_fixed_version_length_and_digest() -> None:
    body = b'{"text":"ok"}'
    response = _FakeObjectResponse(body, version="version-1")
    fake = _FakeObjectClient(response)

    assert result_reader(fake).read_json(result_reference(body), "version-1") == {"text": "ok"}
    assert fake.calls == [
        ("test", "private/execution-results/result-1/transcription.json", 0, len(body), "version-1")
    ]
    assert response.closed is True and response.released is True


def test_result_reader_rejects_another_bucket_without_touching_storage() -> None:
    body = b'{"text":"ok"}'
    fake = _FakeObjectClient(_FakeObjectResponse(body, version="version-1"))

    with pytest.raises(GatewayResultObjectError) as caught:
        result_reader(fake).read(result_reference(body, bucket="other"), "version-1")

    assert caught.value.code == "result_object_scope_rejected"
    assert fake.calls == []


def test_result_reader_rejects_a_body_that_does_not_match_the_receipt() -> None:
    body = b'{"text":"ok"}'
    tampered = b'{"text":"no"}'
    fake = _FakeObjectClient(_FakeObjectResponse(tampered, version="version-1"))

    with pytest.raises(GatewayResultObjectError) as caught:
        result_reader(fake).read(result_reference(body), "version-1")

    assert caught.value.code == "result_object_mismatch"


def test_result_reader_rejects_a_different_storage_version() -> None:
    body = b'{"text":"ok"}'
    fake = _FakeObjectClient(_FakeObjectResponse(body, version="version-2"))

    with pytest.raises(GatewayResultObjectError) as caught:
        result_reader(fake).read(result_reference(body), "version-1")

    assert caught.value.code == "result_object_mismatch"
