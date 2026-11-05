"""Gateway stop observation for an accepted public Job cancellation."""

from __future__ import annotations

import json
from typing import Any, cast

import httpx
import pytest
from pydantic import SecretStr

from speechnexus_backend.speech.domain.job_dispatch import submit_intent_identity
from speechnexus_backend.speech.infrastructure.gateway_execution_client import (
    GatewayExecutionClient,
)
from speechnexus_backend.speech.infrastructure.gateway_job_cancellation import (
    GatewayJobCancellationStopper,
)

DIGEST = "sha256:" + "a" * 64
JOB = "job-synthetic-cancel"
OPERATION = JOB
ATTEMPT = JOB + "-a1"


def _document(state: str, *, submit_id: str, operation: str = OPERATION) -> dict[str, Any]:
    return {
        "submit_intent_id": submit_id,
        "lease_id": "lease-1",
        "operation_id": operation,
        "attempt_id": ATTEMPT,
        "stage_id": "stage-1",
        "workspace_id": "workspace-default",
        "pool_id": "tts-batch",
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
        "execution_state": state,
        "cleanup_state": "pending",
        "stop_evidence_kind": "none",
        "updated_at": "2026-10-27T14:59:01+00:00",
    }


def _aborted(submit_id: str) -> dict[str, Any]:
    document = _document("aborted", submit_id=submit_id)
    document["cleanup_state"] = "complete"
    document["stop_evidence_kind"] = "engine_abort_ack"
    return document


def _client(handler: Any) -> GatewayExecutionClient:
    return GatewayExecutionClient(
        base_url="http://gateway.test",
        credential=SecretStr("synthetic-token-0001"),
        timeout_seconds=1,
        max_retries=0,
        backoff_seconds=0,
        _transport=httpx.MockTransport(handler),
    )


def _job() -> Any:
    return cast(Any, type("Job", (), {"job_id": JOB, "state": "cancelling"})())


def _receipt() -> Any:
    return cast(
        Any,
        type("Receipt", (), {"operation_id": OPERATION, "attempt_id": ATTEMPT})(),
    )


def _install(monkeypatch: pytest.MonkeyPatch, handler: Any) -> None:
    monkeypatch.setattr(
        GatewayExecutionClient,
        "from_settings",
        classmethod(lambda cls, settings, _transport=None: _client(handler)),
    )


@pytest.mark.asyncio
async def test_aborted_status_is_verified_stop(monkeypatch: pytest.MonkeyPatch) -> None:
    submit_id = submit_intent_identity(JOB)
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.method + " " + request.url.path)
        return httpx.Response(200, json=_aborted(submit_id))

    _install(monkeypatch, handler)
    observation = await GatewayJobCancellationStopper(cast(Any, object())).stop(_job(), _receipt())
    assert observation.state == "stopped"
    assert calls == ["GET /internal/v1/executions/" + submit_id]


@pytest.mark.asyncio
async def test_live_execution_sends_one_durable_abort(monkeypatch: pytest.MonkeyPatch) -> None:
    submit_id = submit_intent_identity(JOB)
    bodies: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json=_document("executing", submit_id=submit_id))
        bodies.append(json.loads(request.content))
        assert request.url.path.endswith("/abort-intents")
        return httpx.Response(200, json=_aborted(submit_id))

    _install(monkeypatch, handler)
    observation = await GatewayJobCancellationStopper(cast(Any, object())).stop(_job(), _receipt())
    assert observation.state == "stopped"
    assert bodies == [
        {
            "operation_id": OPERATION,
            "attempt_id": ATTEMPT,
            "fencing_token": 7,
            "reason": "client_cancelled",
            "wait_timeout_ms": 5000,
        }
    ]


@pytest.mark.asyncio
async def test_absent_execution_is_not_stop_evidence(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            404,
            json={"error": {"code": "control_execution_not_found", "message": "missing"}},
        )

    _install(monkeypatch, handler)
    observation = await GatewayJobCancellationStopper(cast(Any, object())).stop(_job(), _receipt())
    assert observation.state == "not_found"


@pytest.mark.asyncio
async def test_identity_mismatch_or_failed_execution_never_claims_cancelled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    submit_id = submit_intent_identity(JOB)

    def mismatched(request: httpx.Request) -> httpx.Response:
        document = _aborted(submit_id)
        document["operation_id"] = "other-job"
        return httpx.Response(200, json=document)

    _install(monkeypatch, mismatched)
    observation = await GatewayJobCancellationStopper(cast(Any, object())).stop(_job(), _receipt())
    assert observation.state == "unconfirmed"

    def failed(request: httpx.Request) -> httpx.Response:
        document = _document("failed", submit_id=submit_id)
        document["error_code"] = "cancellation_unconfirmed"
        return httpx.Response(200, json=document)

    _install(monkeypatch, failed)
    observation = await GatewayJobCancellationStopper(cast(Any, object())).stop(_job(), _receipt())
    assert observation == type(observation)("failed", "cancellation_unconfirmed")


@pytest.mark.asyncio
@pytest.mark.asyncio
async def test_aborted_without_complete_cleanup_is_not_stop_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    submit_id = submit_intent_identity(JOB)

    def handler(request: httpx.Request) -> httpx.Response:
        document = _aborted(submit_id)
        document["cleanup_state"] = "pending"
        document["stop_evidence_kind"] = "none"
        return httpx.Response(200, json=document)

    _install(monkeypatch, handler)
    observation = await GatewayJobCancellationStopper(cast(Any, object())).stop(_job(), _receipt())
    assert observation.state == "unconfirmed"


async def test_abort_rejection_remains_unconfirmed(monkeypatch: pytest.MonkeyPatch) -> None:
    submit_id = submit_intent_identity(JOB)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json=_document("executing", submit_id=submit_id))
        return httpx.Response(
            409,
            json={
                "error": {
                    "code": "stale_fencing_token",
                    "message": "synthetic",
                    "retryable": False,
                }
            },
        )

    _install(monkeypatch, handler)
    observation = await GatewayJobCancellationStopper(cast(Any, object())).stop(_job(), _receipt())
    assert observation.state == "unconfirmed"
