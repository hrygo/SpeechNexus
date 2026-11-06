"""Abort acceptance must commit before a sender can wait, claim or call the engine."""

from __future__ import annotations

import json
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime
from threading import Event
from typing import Any, cast

import pytest

from speechnexus_gateway.application.abort_execution import ExecutionAbortService
from speechnexus_gateway.application.ingress import GatewayIngress
from speechnexus_gateway.application.submit_execution import SubmitExecutionService
from speechnexus_gateway.domain.abort_intent import (
    AbortClaimRefusal,
    AbortDispatchClaim,
    AbortExecutionRequest,
)
from speechnexus_gateway.domain.canonical import JsonValue, canonical_json, command_digest
from speechnexus_gateway.domain.ports import AbortOutcome, ExecutionFactLedger
from speechnexus_gateway.domain.recovery import (
    ExecutionObservationConflictError,
    MarkExecutionUnknown,
)
from speechnexus_gateway.domain.submission import (
    ExecutionState,
    InvalidExecutionRequestError,
    RecordStop,
    StaleFencingTokenError,
    StopEvidenceKind,
    SubmissionIntent,
    SubmissionOutcomeUnknownError,
    SubmissionStoreError,
    SubmissionStoreUnavailableError,
    SubmitRequest,
    VerifiedStopEvidence,
)
from speechnexus_gateway.infrastructure.abort_intent_sql import receive_abort
from speechnexus_gateway.infrastructure.submit_sql import to_unix_micros
from tests.fake.execution import (
    InMemorySubmissionLedger,
    ScriptedAbort,
    ScriptedEngine,
    fake_ingress,
)
from tests.fake.execution_command import original_request
from tests.unit.gateway.test_submit_execution import NOW
from tests.unit.gateway.test_submit_ledger_sql import stored_row


class ObservableLedger(InMemorySubmissionLedger):
    """A separately readable receipt store with controllable commit acknowledgement."""

    def __init__(self, now: Callable[[], datetime] = lambda: NOW) -> None:
        super().__init__(now=now)
        self.received = Event()
        self.failure: str | None = None
        self.claims = 0

    def receive_abort(self, request: AbortExecutionRequest) -> None:
        if self.failure == "rollback":
            raise SubmissionStoreUnavailableError("synthetic rollback")
        super().receive_abort(request)
        self.received.set()
        if self.failure == "unknown":
            # The receipt exists, but the caller has not received a commit acknowledgement.
            raise SubmissionOutcomeUnknownError("synthetic lost acknowledgement")

    def claim_abort(
        self, request: AbortExecutionRequest, *, attempt_id: str, lease_ms: int
    ) -> AbortDispatchClaim | AbortClaimRefusal:
        self.claims += 1
        return super().claim_abort(request, attempt_id=attempt_id, lease_ms=lease_ms)

    def receipt(self, submit_intent_id: str) -> AbortExecutionRequest | None:
        with self._abort_lock:
            return self._abort_receipts.get(submit_intent_id)


def setup_abort(
    now: Callable[[], datetime] = lambda: NOW,
) -> tuple[
    ExecutionAbortService,
    ObservableLedger,
    ScriptedAbort,
    GatewayIngress,
    SubmitRequest,
    AbortExecutionRequest,
]:
    ingress = fake_ingress(timeout_seconds=4)
    ledger = ObservableLedger(now=now)
    engine = ScriptedEngine(now=lambda: NOW)
    request = original_request()
    request = replace(
        request, identity=replace(request.identity, host_node_id="node-1", gateway_id="gateway-1")
    )
    SubmitExecutionService(
        ledger=ledger, ingress=ingress, dispatch=engine, observation=engine, now=now
    ).submit(request)
    abort = ScriptedAbort(now=now)
    service = ExecutionAbortService(
        ingress=ingress, ledger=ledger, abort=abort, durable_abort_enabled=True
    )
    identity = request.identity
    abort_request = AbortExecutionRequest(
        identity.submit_intent_id,
        identity.operation_id,
        identity.attempt_id,
        identity.fencing_token,
        "client_cancelled",
        1000,
    )
    return service, ledger, abort, ingress, request, abort_request


class NonDurableLedger:
    """Usable execution facts without receipt or claim capability."""

    def __init__(self, ledger: ObservableLedger) -> None:
        self.ledger = ledger

    def get(self, submit_intent_id: str) -> SubmissionIntent | None:
        return self.ledger.get(submit_intent_id)

    def record_stop(self, request: RecordStop) -> SubmissionIntent:
        return self.ledger.record_stop(request)

    def mark_execution_unknown(self, request: MarkExecutionUnknown) -> SubmissionIntent:
        return self.ledger.mark_execution_unknown(request)


@pytest.mark.parametrize("method", ["abort", "abort_idempotent"])
def test_receipt_is_observable_while_an_existing_send_holds_the_real_lane(method: str) -> None:
    service, ledger, engine, ingress, request, abort_request = setup_abort()
    with ThreadPoolExecutor(max_workers=1) as workers:
        with ingress.dispatch(request.identity):
            future = workers.submit(getattr(service, method), abort_request)
            assert ledger.received.wait(1), "receipt was blocked behind the existing sender"
            # This read is independent of the blocked abort request and its sending lock.
            assert ledger.receipt(request.identity.submit_intent_id) == abort_request
            assert ledger.claims == 0 and engine.calls == []
            assert not future.done()
        result = future.result(timeout=3)
    assert (
        result is not None and result.intent.execution_state is ExecutionState.UNKNOWN_QUARANTINED
    )
    assert ledger.claims == 1 and len(engine.calls) == 1


@pytest.mark.parametrize("method", ["abort", "abort_idempotent"])
@pytest.mark.parametrize("failure", ["rollback", "unknown"])
def test_unacknowledged_receive_never_claims_or_sends(method: str, failure: str) -> None:
    service, ledger, engine, _, request, abort_request = setup_abort()
    ledger.failure = failure
    error = (
        SubmissionStoreUnavailableError if failure == "rollback" else SubmissionOutcomeUnknownError
    )
    with pytest.raises(error):
        getattr(service, method)(abort_request)
    assert ledger.claims == 0 and engine.calls == []
    assert ledger.receipt(request.identity.submit_intent_id) == (
        abort_request if failure == "unknown" else None
    )
    # An explicit replay of the exact original resolves the unknown without a second receipt.
    ledger.failure = None
    result = getattr(service, method)(abort_request)
    assert (
        result is not None and result.intent.execution_state is ExecutionState.UNKNOWN_QUARANTINED
    )
    assert ledger.receipt(request.identity.submit_intent_id) == abort_request
    assert ledger.claims == 1 and len(engine.calls) == 1


@pytest.mark.parametrize("method", ["abort", "abort_idempotent"])
@pytest.mark.parametrize("missing", ["disabled", "implementation"])
def test_without_durable_authority_abort_fails_closed(method: str, missing: str) -> None:
    service, ledger, engine, ingress, _, abort_request = setup_abort()
    del service
    fact_ledger = (
        ledger if missing == "disabled" else cast(ExecutionFactLedger, NonDurableLedger(ledger))
    )
    service = ExecutionAbortService(
        ingress=ingress,
        ledger=fact_ledger,
        abort=engine,
        durable_abort_enabled=missing != "disabled",
    )
    with pytest.raises(SubmissionStoreUnavailableError, match="durable abort authority"):
        getattr(service, method)(abort_request)
    assert ledger.claims == 0 and engine.calls == []
    assert ledger.receipt(abort_request.submit_intent_id) is None


@pytest.mark.parametrize("method", ["abort", "abort_idempotent"])
def test_exact_replay_sends_once_and_changed_abort_intent_conflicts(method: str) -> None:
    service, ledger, engine, _, _, request = setup_abort()
    first = getattr(service, method)(request)
    replay = getattr(service, method)(request)
    assert first == replay
    for changed in (
        replace(request, reason="deadline_exceeded"),
        replace(request, wait_timeout_ms=2000),
    ):
        with pytest.raises(ExecutionObservationConflictError):
            getattr(service, method)(changed)
    assert ledger.receipt(request.submit_intent_id) == request
    assert len(engine.calls) == 1


@pytest.mark.parametrize("method", ["abort", "abort_idempotent"])
@pytest.mark.parametrize("field", ["operation_id", "attempt_id", "fencing_token"])
def test_wrong_original_identity_is_not_received(method: str, field: str) -> None:
    service, ledger, engine, _, _, request = setup_abort()
    expected: type[Exception]
    if field == "fencing_token":
        changed = replace(request, fencing_token=2)
        expected = StaleFencingTokenError
    elif field == "attempt_id":
        changed = replace(request, attempt_id="another-identity")
        expected = InvalidExecutionRequestError
    else:
        changed = replace(request, operation_id="another-identity")
        expected = InvalidExecutionRequestError
    with pytest.raises(expected):
        getattr(service, method)(changed)
    assert ledger.receipt(request.submit_intent_id) is None
    assert ledger.claims == 0 and engine.calls == []


@pytest.mark.parametrize("method", ["abort", "abort_idempotent"])
@pytest.mark.parametrize("change", ["identity", "digest"])
def test_a_claim_that_no_longer_matches_the_original_never_reaches_the_engine(
    monkeypatch: pytest.MonkeyPatch, method: str, change: str
) -> None:
    service, ledger, engine, _, _, request = setup_abort()
    original_claim = ledger.claim_abort

    def changed_claim(
        abort_request: AbortExecutionRequest, *, attempt_id: str, lease_ms: int
    ) -> AbortDispatchClaim | AbortClaimRefusal:
        claimed = original_claim(abort_request, attempt_id=attempt_id, lease_ms=lease_ms)
        assert isinstance(claimed, AbortDispatchClaim)
        if change == "identity":
            identity = replace(claimed.intent.identity, instance_generation=2)
            return replace(claimed, intent=replace(claimed.intent, identity=identity))
        return replace(claimed, intent=replace(claimed.intent, request_digest="sha256:" + "0" * 64))

    monkeypatch.setattr(ledger, "claim_abort", changed_claim)
    with pytest.raises(InvalidExecutionRequestError):
        getattr(service, method)(request)
    assert ledger.receipt(request.submit_intent_id) == request
    assert ledger.claims == 1 and engine.calls == []
    stored = ledger.get(request.submit_intent_id)
    assert stored is not None and stored.execution_state is ExecutionState.UNKNOWN_QUARANTINED


@pytest.mark.parametrize("method", ["abort", "abort_idempotent"])
def test_changed_original_digest_while_waiting_for_the_lane_never_claims(method: str) -> None:
    service, ledger, engine, ingress, original, request = setup_abort()
    with ThreadPoolExecutor(max_workers=1) as workers:
        with ingress.dispatch(original.identity):
            future = workers.submit(getattr(service, method), request)
            assert ledger.received.wait(1)
            stored = ledger.get(request.submit_intent_id)
            assert stored is not None
            ledger._intents[request.submit_intent_id] = replace(
                stored, request_digest="sha256:" + "0" * 64
            )
        with pytest.raises(InvalidExecutionRequestError):
            future.result(timeout=3)
    assert ledger.claims == 0 and engine.calls == []


@pytest.mark.parametrize("method", ["abort", "abort_idempotent"])
def test_terminal_result_arriving_while_abort_waits_is_not_overwritten(method: str) -> None:
    service, ledger, engine, ingress, request, abort_request = setup_abort()
    with ThreadPoolExecutor(max_workers=1) as workers:
        with ingress.dispatch(request.identity):
            future = workers.submit(getattr(service, method), abort_request)
            assert ledger.received.wait(1)
            terminal = ledger.record_stop(
                RecordStop(
                    VerifiedStopEvidence(
                        evidence_id="evidence-terminal",
                        submit_intent_id=request.identity.submit_intent_id,
                        evidence_kind=StopEvidenceKind.ENGINE_TERMINAL_RESPONSE,
                        evidence_digest="sha256:" + "d" * 64,
                        observed_at_us=to_unix_micros(NOW),
                        execution_stopped=True,
                        resources_released=True,
                        reason=None,
                    ),
                    ExecutionState.SUCCEEDED,
                )
            )
        result = future.result(timeout=3)
    assert result is not None and result.intent == terminal
    assert ledger.get(request.identity.submit_intent_id) == terminal
    assert engine.calls == []


@pytest.mark.parametrize("method", ["abort", "abort_idempotent"])
def test_terminal_stop_arriving_during_unconfirmed_abort_is_not_overwritten(method: str) -> None:
    _, ledger, _, ingress, request, abort_request = setup_abort()
    entered, resume = Event(), Event()

    class WaitingAbort(ScriptedAbort):
        def abort(
            self, intent: SubmissionIntent, *, reason: str, wait_timeout_ms: int
        ) -> AbortOutcome:
            entered.set()
            assert resume.wait(3)
            return super().abort(intent, reason=reason, wait_timeout_ms=wait_timeout_ms)

    engine = WaitingAbort(now=lambda: NOW)
    service = ExecutionAbortService(
        ingress=ingress, ledger=ledger, abort=engine, durable_abort_enabled=True
    )
    with ThreadPoolExecutor(max_workers=1) as workers:
        future = workers.submit(getattr(service, method), abort_request)
        try:
            assert entered.wait(1)
            terminal = ledger.record_stop(
                RecordStop(
                    VerifiedStopEvidence(
                        evidence_id="evidence-stop",
                        submit_intent_id=request.identity.submit_intent_id,
                        evidence_kind=StopEvidenceKind.ENGINE_ABORT_ACK,
                        evidence_digest="sha256:" + "d" * 64,
                        observed_at_us=to_unix_micros(NOW),
                        execution_stopped=True,
                        resources_released=True,
                        reason=None,
                    ),
                    ExecutionState.ABORTED,
                )
            )
        finally:
            resume.set()
        future.result(timeout=3)
    assert ledger.get(request.identity.submit_intent_id) == terminal
    assert len(engine.calls) == 1


class AbortSqlCursor:
    """Minimal row store for the production SQL helpers; it has no database semantics."""

    rowcount = 1

    def __init__(self, request: SubmitRequest) -> None:
        identity = request.identity
        self.stored = stored_row(
            submit_intent_id=identity.submit_intent_id,
            lease_id=identity.lease_id,
            operation_id=identity.operation_id,
            attempt_id=identity.attempt_id,
            command_digest=identity.command_digest,
            request_digest=request.request_digest,
            voice_revision_id=identity.voice_revision_id,
            deadline_at_us=to_unix_micros(identity.deadline_at),
            output_stream_id=request.output_stream_id,
            sample_rate_hz=identity.sample_rate_hz,
            hard_output_ceiling_samples=None,
            stage_id=identity.stage_id,
            pool_id=identity.pool_id,
            host_node_id=identity.host_node_id,
            gateway_id=identity.gateway_id,
            instance_id=identity.instance_id,
            instance_generation=identity.instance_generation,
            fencing_token=identity.fencing_token,
            workspace_id=identity.workspace_id,
            binding_revision_id=identity.binding_revision_id,
            policy_revision_id=identity.policy_revision_id,
        )
        self.document: dict[str, Any] = {
            "version": self.stored["version"],
            "request_digest": request.request_digest,
            "command_digest": identity.command_digest,
            "document_present": True,
            "document_bytes": len(request.request_document.encode("utf-8")),
            "bounded_document": request.request_document,
            "command_redacted_at_us": None,
            "command_deletion_job_id": None,
            "command_deletion_evidence_digest": None,
        }
        self.receipt: dict[str, Any] | None = None
        self.calls: list[str] = []
        self.rows: list[dict[str, Any]] = []

    def execute(self, query: str, parameters: tuple[object, ...] = ()) -> object:
        self.calls.append(query)
        if query.startswith("SELECT l.pool_id, l.lease_id"):
            self.rows = [{"pool_id": self.stored["pool_id"], "lease_id": self.stored["lease_id"]}]
        elif query.startswith("SELECT pool_id FROM resource_pools"):
            self.rows = [{"pool_id": self.stored["pool_id"]}]
        elif query.startswith("SELECT submit_intent_id FROM submit_intents"):
            self.rows = [{"submit_intent_id": self.stored["submit_intent_id"]}]
        elif query.startswith("SELECT l.lease_state"):
            self.rows = [{**self.document, "lease_state": "reserved"}]
        elif "FROM submit_intents AS s" in query:
            self.rows = [self.stored]
        elif "OCTET_LENGTH(request_document)" in query:
            self.rows = [self.document]
        elif "FROM gateway_abort_intents" in query:
            self.rows = [] if self.receipt is None else [self.receipt]
        elif query.startswith("INSERT INTO gateway_abort_intents"):
            names = (
                "submit_intent_id",
                "operation_id",
                "attempt_id",
                "fencing_token",
                "reason",
                "wait_timeout_ms",
                "received_at_us",
                "instance_id",
                "instance_generation",
            )
            self.receipt = dict(zip(names, parameters, strict=True)) | {
                "dispatched_at_us": None,
                "dispatch_attempt_id": None,
                "dispatch_started_at_us": None,
                "dispatch_deadline_at_us": None,
                "dispatch_outcome": None,
                "dispatch_outcome_at_us": None,
                "dispatch_attempts": 0,
            }
            self.rows = []
        else:
            raise AssertionError(f"unexpected SQL: {query}")
        return None

    def fetchall(self) -> list[dict[str, Any]]:
        return self.rows


def sql_request(request: SubmitRequest) -> AbortExecutionRequest:
    identity = request.identity
    return AbortExecutionRequest(
        identity.submit_intent_id,
        identity.operation_id,
        identity.attempt_id,
        identity.fencing_token,
        "client_cancelled",
        1000,
    )


@pytest.mark.parametrize("corruption", ["document", "digest", "identity", "redacted", "missing"])
def test_sql_receive_refuses_unverifiable_original_without_a_receipt(corruption: str) -> None:
    request = original_request()
    cursor = AbortSqlCursor(request)
    if corruption == "document":
        cursor.document["bounded_document"] = "{}"
        cursor.document["document_bytes"] = 2
    elif corruption == "digest":
        cursor.document["request_digest"] = "sha256:" + "0" * 64
    elif corruption == "identity":
        document = json.loads(request.request_document)
        document["execution_request"]["instance_generation"] += 1
        text = canonical_json(cast(JsonValue, document))
        digest = command_digest(cast(JsonValue, document))
        cursor.document.update(
            bounded_document=text, document_bytes=len(text.encode()), request_digest=digest
        )
        cursor.stored["request_digest"] = digest
    elif corruption == "redacted":
        cursor.document["command_redacted_at_us"] = 1
    else:
        cursor.document["document_present"] = False
    with pytest.raises(SubmissionStoreError):
        receive_abort(cursor, sql_request(request), clock=lambda: NOW)
    assert cursor.receipt is None


def test_sql_replay_keeps_first_receive_time_and_conflicts_on_changed_intent() -> None:
    request = original_request()
    cursor = AbortSqlCursor(request)
    abort = sql_request(request)
    receive_abort(cursor, abort, clock=lambda: NOW)
    first = dict(cast(dict[str, Any], cursor.receipt))
    assert first["received_at_us"] == to_unix_micros(NOW)
    receive_abort(cursor, abort, clock=lambda: NOW.replace(year=NOW.year + 1))
    assert cursor.receipt == first
    for changed in (
        replace(abort, reason="deadline_exceeded"),
        replace(abort, wait_timeout_ms=2000),
    ):
        with pytest.raises(ExecutionObservationConflictError):
            receive_abort(cursor, changed, clock=lambda: NOW)
        assert cursor.receipt == first
    locks = [query for query in cursor.calls if "FOR UPDATE" in query]
    assert locks[0].startswith("SELECT pool_id FROM resource_pools")
    assert locks[1].startswith("SELECT submit_intent_id FROM submit_intents")
    assert "FROM gateway_abort_intents" in locks[2]
    assert all("runtime_instance_heads" not in query and "binding" not in query for query in locks)
