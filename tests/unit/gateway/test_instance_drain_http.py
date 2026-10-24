"""Instance drain HTTP: report unfinished work and never claim an unverified stop.

The drain state is a safety assertion, so these tests check the conservative direction
first: a generation with unfinished or quarantined work is never reported as drained, and a
stop the adapter could not verify leaves the execution quarantined and still holding
capacity.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr
from speechnexus_contracts.execution import parse

from speechnexus_gateway.app import create_app
from speechnexus_gateway.application.abort_execution import ExecutionAbortService
from speechnexus_gateway.application.barrier_control import GatewayBarrierService
from speechnexus_gateway.application.instance_drain import InstanceDrainService
from speechnexus_gateway.application.query_execution import ExecutionQueryService
from speechnexus_gateway.application.readiness import ReadinessService
from speechnexus_gateway.application.submit_execution import SubmitExecutionService
from speechnexus_gateway.domain.canonical import JsonValue
from speechnexus_gateway.domain.instance_drain import InstanceIdentity
from speechnexus_gateway.domain.ports import AbortOutcome, DispatchOutcome
from speechnexus_gateway.domain.recovery import observed_at_microseconds
from speechnexus_gateway.domain.submission import (
    DispatchResult,
    ExecutionErrorCode,
    ExecutionState,
    StopEvidenceKind,
    SubmissionIdentity,
    SubmissionIntent,
    SubmitRequest,
    VerifiedStopEvidence,
)
from speechnexus_gateway.settings import Settings
from tests.fake.execution import (
    InMemorySubmissionLedger,
    ScriptedAbort,
    ScriptedEngine,
    fake_ingress,
)
from tests.fake.readiness import StaticProbe
from tests.unit.gateway.test_barrier_http import BODY as BARRIER_BODY
from tests.unit.gateway.test_barrier_http import PATH as BARRIER_PATH
from tests.unit.gateway.test_submit_execution import NOW, identity_for
from tests.unit.gateway.test_submit_http import COMMAND, HEADERS, SUBMIT_PATH, TOKEN, submit_body

INSTANCE = "instance-1"
GENERATION = 3
DRAIN_PATH = "/internal/v1/instances/instance-1/drain"
ABORT_PATH = "/internal/v1/executions/{submit_intent_id}/abort"
NOW_TEXT = "2026-10-24T00:00:00.000000Z"


class Ticking:
    """A monotonic clock that advances by a fixed step on every read."""

    def __init__(self, step: float) -> None:
        self._value = 0.0
        self._step = step

    def __call__(self) -> float:
        value = self._value
        self._value += self._step
        return value


def drain_body(**changes: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "host_node_id": "node-1",
        "pool_id": "asr-batch",
        "instance_generation": GENERATION,
        "fencing_token": 7,
        "max_drain_wait_ms": 1000,
    }
    return body | changes


def abort_body(**changes: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "operation_id": "op-1",
        "attempt_id": "attempt-1",
        "fencing_token": 7,
        "reason": "client_cancelled",
        "wait_timeout_ms": 1000,
    }
    return body | changes


def evidence_for(submit_intent_id: str) -> VerifiedStopEvidence:
    return VerifiedStopEvidence(
        evidence_id=f"evidence-{submit_intent_id}",
        submit_intent_id=submit_intent_id,
        evidence_kind=StopEvidenceKind.ENGINE_ABORT_ACK,
        evidence_digest="sha256:" + "d" * 64,
        observed_at_us=observed_at_microseconds(NOW),
        execution_stopped=True,
        resources_released=True,
        reason=None,
    )


def seed_unfinished_intent(
    ledger: InMemorySubmissionLedger,
    identity: SubmissionIdentity,
    command: JsonValue = COMMAND,
) -> SubmissionIntent:
    """Leave one recorded intent before its engine request, as an interrupted gateway would."""
    request = SubmitRequest(identity=identity, command=command)
    intent = SubmissionIntent(
        identity=identity,
        recorded_at=NOW,
        updated_at=NOW,
        request_digest=request.request_digest,
    )
    ledger.add(intent, request_document=request.request_document)
    return intent


class Harness:
    """One app on the in-memory ledger, with or without an engine abort adapter."""

    def __init__(
        self,
        tmp_path: Path,
        *,
        abort_adapter: bool = False,
        drain_service: bool = True,
        monotonic: Ticking | None = None,
    ) -> None:
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
        self.ledger.register_instance(
            InstanceIdentity(INSTANCE, GENERATION, "node-1", "asr-batch", "gateway-1")
        )
        abort_service = ExecutionAbortService(
            ingress=self.ingress, ledger=self.ledger, abort=self.abort, now=lambda: NOW
        )
        services: dict[str, Any] = {
            "barrier_control": GatewayBarrierService(ingress=self.ingress, ledger=self.ledger),
            "execution_query": ExecutionQueryService(ingress=self.ingress, ledger=self.ledger),
            "submit_execution": SubmitExecutionService(
                ingress=self.ingress,
                ledger=self.ledger,
                dispatch=self.engine,
                observation=self.engine,
                now=lambda: NOW,
            ),
            "abort_execution": abort_service,
            "readiness": ReadinessService([StaticProbe("control_postgresql")], timeout_seconds=1),
        }
        if drain_service:
            services["instance_drain"] = InstanceDrainService(
                ingress=self.ingress,
                ledger=self.ledger,
                abort=abort_service if abort_adapter else None,
                now=lambda: NOW,
                monotonic=monotonic,
            )
        self.app = create_app(
            settings=Settings(
                control_database_url=SecretStr("postgresql://unused@127.0.0.1/unused"),
                node_id="node-1",
                gateway_id="gateway-1",
                component_token=SecretStr(TOKEN),
                ingress_lock_directory=tmp_path,
            ),
            **services,
        )


async def post(
    app: FastAPI, path: str, body: dict[str, Any], headers: dict[str, str] | None = None
) -> Any:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        return await client.post(path, json=body, headers=HEADERS if headers is None else headers)


def call(app: FastAPI, path: str, body: dict[str, Any], **kwargs: Any) -> Any:
    return asyncio.run(post(app, path, body, **kwargs))


def test_an_empty_generation_is_drained_and_safe_to_stop(tmp_path: Path) -> None:
    harness = Harness(tmp_path)

    response = call(harness.app, DRAIN_PATH, drain_body())

    assert response.status_code == 200
    body = parse("DrainInstanceResponse", response.json())
    assert body["drain_state"] == "drained"
    assert body["safe_to_stop_instance"] is True
    assert body["active_intent_count"] == 0
    assert body["submitting_intent_count"] == 0
    assert body["quarantined_intent_count"] == 0
    assert body["instance_id"] == INSTANCE
    assert body["instance_generation"] == GENERATION
    assert body["updated_at"] == NOW_TEXT
    assert response.headers["x-request-id"] == HEADERS["X-Request-ID"]


def test_recorded_but_undispatched_work_reports_draining_without_a_safe_stop(
    tmp_path: Path,
) -> None:
    harness = Harness(tmp_path)
    seed_unfinished_intent(harness.ledger, identity_for(COMMAND))

    response = call(harness.app, DRAIN_PATH, drain_body())

    assert response.status_code == 200
    body = parse("DrainInstanceResponse", response.json())
    assert body["drain_state"] == "draining"
    assert body["safe_to_stop_instance"] is False
    assert body["submitting_intent_count"] == 1
    assert body["active_intent_count"] == 0
    # A drain without an adapter records nothing and never releases capacity.
    stored = harness.ledger.get("intent-1")
    assert stored is not None
    assert stored.execution_state is ExecutionState.SUBMITTING
    assert harness.abort.calls == []


def test_an_executing_intent_is_active_and_never_safe_to_stop(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    assert call(harness.app, SUBMIT_PATH, submit_body()).status_code == 200

    response = call(harness.app, DRAIN_PATH, drain_body())

    assert response.status_code == 200
    body = parse("DrainInstanceResponse", response.json())
    assert body["drain_state"] == "draining"
    assert body["safe_to_stop_instance"] is False
    assert body["active_intent_count"] == 1
    assert body["submitting_intent_count"] == 0
    assert harness.engine.dispatch_count == 1


def test_an_unconfirmed_stop_keeps_the_generation_quarantined(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    assert call(harness.app, SUBMIT_PATH, submit_body()).status_code == 200
    aborted = call(harness.app, ABORT_PATH.format(submit_intent_id="intent-1"), abort_body())
    assert aborted.status_code == 200
    assert aborted.json()["execution_state"] == "unknown_quarantined"

    response = call(harness.app, DRAIN_PATH, drain_body())

    assert response.status_code == 200
    body = parse("DrainInstanceResponse", response.json())
    assert body["drain_state"] == "quarantined"
    assert body["safe_to_stop_instance"] is False
    assert body["quarantined_intent_count"] == 1
    assert body["active_intent_count"] == 0
    assert body["submitting_intent_count"] == 0


def test_another_generation_or_node_is_a_conflict(tmp_path: Path) -> None:
    harness = Harness(tmp_path)

    other_generation = call(harness.app, DRAIN_PATH, drain_body(instance_generation=4))
    other_node = call(harness.app, DRAIN_PATH, drain_body(host_node_id="node-2"))
    other_pool = call(harness.app, DRAIN_PATH, drain_body(pool_id="tts-batch"))

    for response in (other_generation, other_node, other_pool):
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "instance_generation_mismatch"


def test_a_token_the_ingress_already_rejects_is_a_conflict(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    sealed = call(
        harness.app,
        BARRIER_PATH,
        BARRIER_BODY | {"min_accepted_fencing_token": 8},
    )
    assert sealed.status_code == 200
    assert harness.ingress.enforced_minimum("asr-batch") == 8

    response = call(harness.app, DRAIN_PATH, drain_body(fencing_token=7))

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "stale_fencing_token"


def test_a_verified_stop_drains_the_generation(tmp_path: Path) -> None:
    harness = Harness(tmp_path, abort_adapter=True, monotonic=Ticking(0.0))
    assert call(harness.app, SUBMIT_PATH, submit_body()).status_code == 200
    harness.abort.script_outcome(AbortOutcome(observed_at=NOW, evidence=evidence_for("intent-1")))

    response = call(harness.app, DRAIN_PATH, drain_body())

    assert response.status_code == 200
    body = parse("DrainInstanceResponse", response.json())
    assert body["drain_state"] == "drained"
    assert body["safe_to_stop_instance"] is True
    assert body["active_intent_count"] == 0
    stored = harness.ledger.get("intent-1")
    assert stored is not None
    assert stored.execution_state is ExecutionState.ABORTED
    assert harness.abort.calls == [("intent-1", "gateway_takeover_reconcile", 1000)]


def test_an_unverified_stop_quarantines_the_generation_and_keeps_capacity(
    tmp_path: Path,
) -> None:
    harness = Harness(tmp_path, abort_adapter=True, monotonic=Ticking(0.0))
    assert call(harness.app, SUBMIT_PATH, submit_body()).status_code == 200

    # The scripted adapter's default answer is honest: it verified nothing about the engine.
    response = call(harness.app, DRAIN_PATH, drain_body())

    assert response.status_code == 200
    body = parse("DrainInstanceResponse", response.json())
    assert body["drain_state"] == "quarantined"
    assert body["safe_to_stop_instance"] is False
    assert body["quarantined_intent_count"] == 1
    assert body["active_intent_count"] == 0
    stored = harness.ledger.get("intent-1")
    assert stored is not None
    assert stored.execution_state is ExecutionState.UNKNOWN_QUARANTINED
    assert stored.error_code is ExecutionErrorCode.CANCELLATION_UNCONFIRMED


def submit_two_dispatched_intents(harness: Harness) -> None:
    """Two executing intents of the same instance generation, in submit-intent order."""
    for suffix in ("a", "b"):
        response = call(
            harness.app,
            SUBMIT_PATH,
            submit_body(
                identity_changes={
                    "submit_intent_id": f"intent-{suffix}",
                    "lease_id": f"lease-{suffix}",
                    "attempt_id": f"attempt-{suffix}",
                }
            ),
        )
        assert response.status_code == 200


def test_a_long_drain_wait_still_bounds_every_single_stop_attempt(tmp_path: Path) -> None:
    harness = Harness(tmp_path, abort_adapter=True, monotonic=Ticking(0.0))
    submit_two_dispatched_intents(harness)

    response = call(harness.app, DRAIN_PATH, drain_body(max_drain_wait_ms=120000))

    assert response.status_code == 200
    # The frozen abort wait stops at one minute, so a two-minute drain takes two attempts.
    assert [attempt[2] for attempt in harness.abort.calls] == [60000, 60000]


def test_the_drain_does_not_start_a_stop_it_cannot_afford_to_wait_for(tmp_path: Path) -> None:
    harness = Harness(tmp_path, abort_adapter=True, monotonic=Ticking(0.5))
    submit_two_dispatched_intents(harness)

    response = call(harness.app, DRAIN_PATH, drain_body(max_drain_wait_ms=1000))

    assert response.status_code == 200
    # The first attempt consumed the remaining wait; the second intent keeps its capacity.
    assert harness.abort.calls == [("intent-a", "gateway_takeover_reconcile", 500)]
    assert response.json()["drain_state"] == "quarantined"


def test_a_drain_never_aborts_work_the_engine_never_received(tmp_path: Path) -> None:
    harness = Harness(tmp_path, abort_adapter=True, monotonic=Ticking(0.0))
    seed_unfinished_intent(harness.ledger, identity_for(COMMAND))

    response = call(harness.app, DRAIN_PATH, drain_body())

    assert response.status_code == 200
    body = parse("DrainInstanceResponse", response.json())
    assert body["drain_state"] == "draining"
    assert body["submitting_intent_count"] == 1
    assert body["safe_to_stop_instance"] is False
    # An intent with no engine request identity has nothing an abort could stop.
    assert harness.abort.calls == []
    stored = harness.ledger.get("intent-1")
    assert stored is not None
    assert stored.execution_state is ExecutionState.SUBMITTING


def test_an_invalid_body_and_missing_credential_are_rejected(tmp_path: Path) -> None:
    harness = Harness(tmp_path)

    invalid = call(harness.app, DRAIN_PATH, drain_body(max_drain_wait_ms=99))
    unknown = call(harness.app, DRAIN_PATH, drain_body(extra=True))
    unauthorized = call(harness.app, DRAIN_PATH, drain_body(), headers={})

    assert invalid.status_code == 422
    assert unknown.status_code == 422
    assert unauthorized.status_code == 401
    assert invalid.json()["error"]["code"] == "invalid_execution_request"


def test_a_missing_drain_service_reports_unavailable_and_writes_nothing(tmp_path: Path) -> None:
    harness = Harness(tmp_path, drain_service=False)

    response = call(harness.app, DRAIN_PATH, drain_body())

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "control_store_unavailable"
    assert response.json()["error"]["retryable"] is True
    assert harness.ledger.get("intent-1") is None
    assert harness.abort.calls == []
