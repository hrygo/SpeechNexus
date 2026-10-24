"""Authenticated submit/abort HTTP: durable intent, one dispatch, honest refusals."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any, cast

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr
from speechnexus_contracts.execution import parse

from speechnexus_gateway.app import create_app
from speechnexus_gateway.application.abort_execution import ExecutionAbortService
from speechnexus_gateway.application.barrier_control import GatewayBarrierService
from speechnexus_gateway.application.query_execution import ExecutionQueryService
from speechnexus_gateway.application.readiness import ReadinessService
from speechnexus_gateway.application.submit_execution import SubmitExecutionService
from speechnexus_gateway.domain.canonical import JsonValue, command_digest
from speechnexus_gateway.domain.ports import AbortOutcome, DispatchOutcome
from speechnexus_gateway.domain.recovery import observed_at_microseconds
from speechnexus_gateway.domain.submission import (
    CleanupState,
    DispatchResult,
    ExecutionState,
    RecordStop,
    StopEvidenceKind,
    VerifiedStopEvidence,
    derived_output_stream_id,
)
from speechnexus_gateway.settings import Settings
from tests.fake.execution import (
    InMemorySubmissionLedger,
    ScriptedAbort,
    ScriptedEngine,
    fake_ingress,
)
from tests.fake.readiness import StaticProbe
from tests.unit.gateway.test_submit_execution import NOW, identity_for

ROOT = Path(__file__).resolve().parents[3]
TOKEN = "synthetic-gateway-component-token"
HEADERS = {"Authorization": f"Bearer {TOKEN}", "X-Request-ID": "synthetic-submit-http"}
SUBMIT_PATH = "/internal/v1/executions/submit"
ABORT_PATH = "/internal/v1/executions/{submit_intent_id}/abort"


def contract_command(index: int = 0) -> JsonValue:
    """One frozen command example; the served route validates the real command schema."""
    schema = json.loads((ROOT / "contracts/internal/execution-command.schema.json").read_text())
    return cast(JsonValue, copy.deepcopy(schema["examples"][index]))


COMMAND = contract_command()
OTHER_COMMAND = contract_command(1)


def submit_body(
    *,
    command: JsonValue | None = None,
    digest: str | None = None,
    identity_changes: dict[str, Any] | None = None,
    **request_changes: Any,
) -> dict[str, Any]:
    resolved = COMMAND if command is None else command
    identity = identity_for(resolved, **(identity_changes or {}))
    body: dict[str, Any] = {
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
        "command_digest": command_digest(resolved) if digest is None else digest,
        "command": resolved,
        "deadline_at": identity.deadline_at.isoformat(),
        "max_output_audio_ms": identity.max_output_audio_ms,
        "sample_rate_hz": identity.sample_rate_hz,
    }
    return body | request_changes


def abort_body(**changes: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "operation_id": "op-1",
        "attempt_id": "attempt-1",
        "fencing_token": 7,
        "reason": "client_cancelled",
        "wait_timeout_ms": 1000,
    }
    return body | changes


def evidence_for(submit_intent_id: str = "intent-1") -> VerifiedStopEvidence:
    return VerifiedStopEvidence(
        evidence_id=f"evidence-{submit_intent_id}",
        submit_intent_id=submit_intent_id,
        evidence_kind=StopEvidenceKind.ENGINE_ABORT_ACK,
        evidence_digest="sha256:" + "c" * 64,
        observed_at_us=observed_at_microseconds(NOW),
        execution_stopped=True,
        resources_released=True,
        reason=None,
    )


class Harness:
    """One app with real ingress, an in-memory ledger and scripted engine doubles."""

    def __init__(self, tmp_path: Path, *, engine_adapter: bool = True) -> None:
        self.ingress = fake_ingress(timeout_seconds=0.01)
        self.ledger = InMemorySubmissionLedger(now=lambda: NOW)
        self.engine = ScriptedEngine(now=lambda: NOW)
        self.abort = ScriptedAbort(now=lambda: NOW)
        self.engine.script_outcome(
            DispatchOutcome(
                result=DispatchResult.ACKNOWLEDGED,
                observed_at=NOW,
                engine_request_id="engine-request-1",
            )
        )
        services: dict[str, Any] = {}
        if engine_adapter:
            services["submit_execution"] = SubmitExecutionService(
                ingress=self.ingress,
                ledger=self.ledger,
                dispatch=self.engine,
                observation=self.engine,
                now=lambda: NOW,
            )
            services["abort_execution"] = ExecutionAbortService(
                ingress=self.ingress, ledger=self.ledger, abort=self.abort, now=lambda: NOW
            )
        self.app = create_app(
            settings=Settings(
                control_database_url=SecretStr("postgresql://unused@127.0.0.1/unused"),
                node_id="node-1",
                gateway_id="gateway-1",
                component_token=SecretStr(TOKEN),
                ingress_lock_directory=tmp_path,
            ),
            barrier_control=GatewayBarrierService(ingress=self.ingress, ledger=self.ledger),
            execution_query=ExecutionQueryService(ingress=self.ingress, ledger=self.ledger),
            readiness=ReadinessService([StaticProbe("control_postgresql")], timeout_seconds=1),
            **services,
        )


async def post(app: FastAPI, path: str, body: dict[str, Any], **kwargs: Any) -> Any:
    headers = kwargs.pop("headers", HEADERS)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        return await client.post(path, json=body, headers=headers, **kwargs)


async def test_submit_records_the_intent_before_one_dispatch_and_replays_idempotently(
    tmp_path: Path,
) -> None:
    harness = Harness(tmp_path)

    first = await post(harness.app, SUBMIT_PATH, submit_body())
    replay = await post(harness.app, SUBMIT_PATH, submit_body())

    assert first.status_code == replay.status_code == 200
    parse("SubmitExecutionResponse", first.json())
    assert first.json()["execution_state"] == "executing"
    assert first.json()["cleanup_state"] == "pending"
    assert first.json()["dispatched_at"] is not None
    assert first.json()["idempotent_replay"] is False
    assert replay.json()["idempotent_replay"] is True
    assert replay.json()["recorded_at"] == first.json()["recorded_at"]
    assert harness.engine.dispatch_count == 1
    stored = harness.ledger.get("intent-1")
    assert stored is not None
    assert stored.execution_state is ExecutionState.EXECUTING
    assert stored.dispatched_at == NOW


async def test_a_conflicting_replay_and_a_mismatched_digest_never_dispatch_again(
    tmp_path: Path,
) -> None:
    harness = Harness(tmp_path)
    assert (await post(harness.app, SUBMIT_PATH, submit_body())).status_code == 200

    conflict = await post(harness.app, SUBMIT_PATH, submit_body(command=OTHER_COMMAND))
    mismatched = await post(
        harness.app,
        SUBMIT_PATH,
        submit_body(
            identity_changes={"submit_intent_id": "intent-2"},
            digest=command_digest(OTHER_COMMAND),
        ),
    )

    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "submit_intent_conflict"
    # A digest that does not describe the carried command is refused before any dispatch.
    assert mismatched.status_code == 422
    assert mismatched.json()["error"]["code"] == "invalid_execution_request"
    assert harness.engine.dispatch_count == 1
    assert harness.ledger.get("intent-2") is None


@pytest.mark.parametrize("authorization", [None, "Bearer wrong"])
async def test_submit_requires_the_component_credential(
    tmp_path: Path, authorization: str | None
) -> None:
    harness = Harness(tmp_path)
    headers = {} if authorization is None else {"Authorization": authorization}

    response = await post(harness.app, SUBMIT_PATH, submit_body(), headers=headers)

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "component_credential_invalid"
    assert response.headers["x-request-id"]
    assert harness.engine.dispatch_count == 0
    assert harness.ledger.get("intent-1") is None


async def test_a_malformed_request_is_refused_with_the_frozen_envelope(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    body = submit_body() | {"output_stream_id": "synthetic-not-a-wire-field"}

    response = await post(harness.app, SUBMIT_PATH, body)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_execution_request"
    assert harness.ledger.get("intent-1") is None


async def test_without_an_engine_adapter_nothing_is_recorded(tmp_path: Path) -> None:
    harness = Harness(tmp_path, engine_adapter=False)

    submitted = await post(harness.app, SUBMIT_PATH, submit_body())
    aborted = await post(harness.app, ABORT_PATH.format(submit_intent_id="intent-1"), abort_body())

    for response in (submitted, aborted):
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "control_store_unavailable"
        assert response.json()["error"]["retryable"] is True
    assert harness.ledger.get("intent-1") is None
    assert harness.engine.dispatch_count == 0


async def test_a_pcm_stage_names_one_stable_stream_for_the_whole_attempt(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    pcm: dict[str, Any] = {"max_output_audio_ms": 1000, "sample_rate_hz": 24000}

    first = await post(harness.app, SUBMIT_PATH, submit_body(**pcm))
    second = await post(
        harness.app,
        SUBMIT_PATH,
        submit_body(
            identity_changes={"submit_intent_id": "intent-2", "stage_id": "stage-2"}, **pcm
        ),
    )
    text_only = await post(
        harness.app, SUBMIT_PATH, submit_body(identity_changes={"submit_intent_id": "intent-3"})
    )

    assert first.status_code == second.status_code == text_only.status_code == 200
    stream = derived_output_stream_id("op-1", "attempt-1")
    assert stream == "op-1.attempt-1"
    for intent_id in ("intent-1", "intent-2"):
        stored = harness.ledger.get(intent_id)
        assert stored is not None and stored.output_stream_id == stream
    without_stream = harness.ledger.get("intent-3")
    assert without_stream is not None and without_stream.output_stream_id is None


async def test_verified_engine_abort_evidence_closes_the_execution(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    assert (await post(harness.app, SUBMIT_PATH, submit_body())).status_code == 200
    harness.abort.script_outcome(AbortOutcome(observed_at=NOW, evidence=evidence_for()))

    response = await post(harness.app, ABORT_PATH.format(submit_intent_id="intent-1"), abort_body())

    assert response.status_code == 200
    parse("ExecutionStatusResponse", response.json())
    assert response.json()["execution_state"] == "aborted"
    assert response.json()["cleanup_state"] == "complete"
    assert response.json()["stop_evidence_kind"] == "engine_abort_ack"
    assert harness.abort.calls == [("intent-1", "client_cancelled", 1000)]


async def test_an_unverified_abort_quarantines_and_keeps_holding_capacity(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    assert (await post(harness.app, SUBMIT_PATH, submit_body())).status_code == 200

    response = await post(harness.app, ABORT_PATH.format(submit_intent_id="intent-1"), abort_body())

    assert response.status_code == 200
    assert response.json()["execution_state"] == "unknown_quarantined"
    assert response.json()["cleanup_state"] == "quarantined"
    assert response.json()["stop_evidence_kind"] == "unconfirmed_timeout"
    assert response.json()["error_code"] == "cancellation_unconfirmed"
    stored = harness.ledger.get("intent-1")
    assert stored is not None
    assert stored.execution_state is ExecutionState.UNKNOWN_QUARANTINED
    assert stored.execution_state.holds_capacity is True
    assert stored.cleanup_state is CleanupState.QUARANTINED
    assert stored.stop_evidence_kind is not StopEvidenceKind.ENGINE_ABORT_ACK


async def test_abort_refuses_another_attempt_or_fencing_token_without_calling_the_engine(
    tmp_path: Path,
) -> None:
    harness = Harness(tmp_path)
    assert (await post(harness.app, SUBMIT_PATH, submit_body())).status_code == 200
    path = ABORT_PATH.format(submit_intent_id="intent-1")

    stale = await post(harness.app, path, abort_body(fencing_token=8))
    foreign = await post(harness.app, path, abort_body(attempt_id="attempt-2"))
    missing = await post(
        harness.app, ABORT_PATH.format(submit_intent_id="intent-404"), abort_body()
    )

    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "stale_fencing_token"
    assert foreign.status_code == 422
    assert foreign.json()["error"]["code"] == "invalid_execution_request"
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "control_execution_not_found"
    assert harness.abort.calls == []


async def test_abort_of_a_closed_execution_returns_the_recorded_state(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    assert (await post(harness.app, SUBMIT_PATH, submit_body())).status_code == 200
    harness.ledger.record_stop(RecordStop(evidence_for(), ExecutionState.ABORTED))

    response = await post(harness.app, ABORT_PATH.format(submit_intent_id="intent-1"), abort_body())

    assert response.status_code == 200
    assert response.json()["execution_state"] == "aborted"
    assert harness.abort.calls == []
