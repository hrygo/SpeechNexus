"""PostgreSQL proof of abort acceptance, original recovery and dispatch ordering.

Uses a distinct disposable schema and synthetic commands with the production ledger;
no real engine, shared database, network-fault or HA qualification is implied.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Event
from typing import Any, Literal, cast

import httpx
import psycopg
import pytest
from psycopg.conninfo import make_conninfo
from pydantic import SecretStr
from tests.fake.execution import ScriptedAbort, ScriptedEngine
from tests.fake.execution_command import command_example
from tests.integration.test_coordination_foundation import Foundation
from tests.integration.test_coordination_foundation import foundation as foundation
from tests.integration.test_gateway_command_redaction import release_fixture
from tests.integration.test_gateway_stop_evidence import evidence_for
from tests.integration.test_gateway_submit_http import HEADERS, TOKEN, abort_body, post
from tests.integration.test_gateway_submit_ledger import (
    identity_for,
    open_pool,
    seed_second_attempt,
)
from tests.integration.test_tts_durable_pg import database as database

from speechnexus_backend.speech.infrastructure.gateway_abort_intent_schema import (
    gateway_abort_grants,
)
from speechnexus_backend.speech.infrastructure.gateway_query_grants import gateway_query_grants
from speechnexus_gateway.app import create_app
from speechnexus_gateway.application.abort_execution import ExecutionAbortService
from speechnexus_gateway.application.barrier_control import GatewayBarrierService
from speechnexus_gateway.application.ingress import GatewayIngress
from speechnexus_gateway.application.query_execution import ExecutionQueryService
from speechnexus_gateway.application.submit_execution import SubmitExecutionService
from speechnexus_gateway.domain.abort_intent import AbortDispatchClaim, AbortExecutionRequest
from speechnexus_gateway.domain.canonical import JsonValue, canonical_json, command_digest
from speechnexus_gateway.domain.command_redaction import (
    AuthorizedCommandRedaction,
    RedactExecutionCommand,
)
from speechnexus_gateway.domain.ports import AbortOutcome
from speechnexus_gateway.domain.recovery import ExecutionObservationConflictError
from speechnexus_gateway.domain.submission import (
    AbortAcknowledgement,
    ExecutionState,
    RecordStop,
    StopEvidenceKind,
    SubmissionIntent,
    SubmissionOutcomeUnknownError,
    SubmissionStoreError,
    SubmitRequest,
)
from speechnexus_gateway.infrastructure.abort_authority import verify_abort_authority
from speechnexus_gateway.infrastructure.execution_reader import PostgresExecutionReader
from speechnexus_gateway.infrastructure.postgres_pool import GatewayPool, gateway_pool
from speechnexus_gateway.infrastructure.process_ownership import ProcessIngressOwnership
from speechnexus_gateway.infrastructure.submit_postgres import PostgresSubmissionLedger
from speechnexus_gateway.settings import Settings

pytestmark = pytest.mark.integration
PASSWORD = "synthetic_foundation_test"
INTENT_ID = "intent-a"


class ObservableLedger(PostgresSubmissionLedger):
    """Observe only completed production transactions, without substituting the SQL."""

    def __init__(self, pool: GatewayPool) -> None:
        super().__init__(pool, timeout_seconds=5)
        self.receive_started = Event()
        self.received = Event()
        self.claims = 0

    def receive_abort(self, request: AbortExecutionRequest) -> None:
        self.receive_started.set()
        super().receive_abort(request)
        self.received.set()

    def claim_abort(
        self, request: AbortExecutionRequest, *, attempt_id: str, lease_ms: int
    ) -> AbortDispatchClaim | None:
        self.claims += 1
        return super().claim_abort(request, attempt_id=attempt_id, lease_ms=lease_ms)


@dataclass
class LiveAbort:
    ledger: ObservableLedger
    ingress: GatewayIngress
    engine: ScriptedAbort
    request: SubmitRequest
    abort_request: AbortExecutionRequest

    def service(self, ledger: ObservableLedger | None = None) -> ExecutionAbortService:
        return ExecutionAbortService(
            ingress=self.ingress,
            ledger=self.ledger if ledger is None else ledger,
            abort=self.engine,
            durable_abort_enabled=True,
        )


@pytest.fixture
def live(foundation: Foundation, tmp_path: Path) -> Iterator[LiveAbort]:
    for grant in gateway_abort_grants(foundation.database.schema, foundation.gateway_role):
        foundation.database.query(grant)
    seed_second_attempt(foundation)
    pool = open_pool(foundation, max_size=4)
    owner = ProcessIngressOwnership(tmp_path, node_id="host-test", gateway_id="gateway-test")
    try:
        assert verify_abort_authority(pool, foundation.database.schema)
        ingress = GatewayIngress(owner, timeout_seconds=4)
        ledger = ObservableLedger(pool)
        command = cast(JsonValue, command_example(0))
        identity = replace(
            identity_for("lease-alt-stage-main", submit_intent_id=INTENT_ID),
            command_digest=command_digest(command),
        )
        request = SubmitRequest(identity=identity, command=command, output_stream_id="tts-main")
        sender = ScriptedEngine()
        outcome = SubmitExecutionService(
            ingress=ingress, ledger=ledger, dispatch=sender, observation=sender
        ).submit(request)
        assert outcome.intent.execution_state is ExecutionState.EXECUTING
        assert sender.dispatch_count == 1
        abort_request = AbortExecutionRequest(
            identity.submit_intent_id,
            identity.operation_id,
            identity.attempt_id,
            identity.fencing_token,
            "client_cancelled",
            1000,
        )
        yield LiveAbort(ledger, ingress, ScriptedAbort(), request, abort_request)
    finally:
        owner.close()
        pool.close()


@pytest.fixture
def reader_pool(foundation: Foundation) -> Iterator[GatewayPool]:
    db = foundation.database
    role = db.schema + "_reader"
    db.query(f"CREATE ROLE \"{role}\" LOGIN PASSWORD '{PASSWORD}'")
    pool = None
    try:
        for grant in gateway_query_grants(db.schema, role):
            db.query(grant)
        pool = gateway_pool(
            make_conninfo(db.url, user=role, password=PASSWORD), schema=db.schema, max_size=2
        )
        pool.open()
        yield pool
    finally:
        if pool is not None:
            pool.close()
        db.query(f'DROP OWNED BY "{role}"')
        db.query(f'DROP ROLE "{role}"')


def abort_digest(request: AbortExecutionRequest) -> str:
    return command_digest(
        {
            "submit_intent_id": request.submit_intent_id,
            "operation_id": request.operation_id,
            "attempt_id": request.attempt_id,
            "fencing_token": request.fencing_token,
            "reason": request.reason,
            "wait_timeout_ms": request.wait_timeout_ms,
        }
    )


def receipt_row(foundation: Foundation) -> list[dict[str, Any]]:
    return foundation.database.query(
        "SELECT submit_intent_id, operation_id, attempt_id, fencing_token, reason, "
        "wait_timeout_ms, received_at_us, dispatched_at_us "
        "FROM gateway_abort_intents WHERE submit_intent_id=%s",
        (INTENT_ID,),
    )


def dispatch_row(foundation: Foundation) -> dict[str, Any]:
    return foundation.database.query(
        "SELECT dispatch_attempt_id, dispatch_started_at_us, dispatch_deadline_at_us, "
        "dispatch_outcome, dispatch_outcome_at_us, dispatch_attempts, next_check_at_us "
        "FROM gateway_abort_intents WHERE submit_intent_id=%s",
        (INTENT_ID,),
    )[0]


def original_abort_request(
    foundation: Foundation, request: AbortExecutionRequest
) -> AbortExecutionRequest:
    row = foundation.database.query(
        "SELECT instance_id, instance_generation FROM gateway_abort_intents "
        "WHERE submit_intent_id=%s",
        (request.submit_intent_id,),
    )[0]
    return replace(
        request,
        instance_id=row["instance_id"],
        instance_generation=row["instance_generation"],
    )


def test_receipt_commits_before_waiting_for_the_actual_ingress_lane(
    foundation: Foundation, live: LiveAbort, reader_pool: GatewayPool
) -> None:
    with ThreadPoolExecutor(max_workers=1) as executor:
        with live.ingress.dispatch(live.request.identity):
            future = executor.submit(live.service().abort, live.abort_request)
            assert live.ledger.received.wait(3)
            # Fresh reader role and connection see the committed receipt while the
            # real local sending lock is still held by this thread.
            recovered = PostgresExecutionReader(reader_pool).query_outcome(INTENT_ID)
            assert recovered is not None and recovered.abort_recovery is not None
            recovery = recovered.abort_recovery
            assert recovery.execution_request_digest == live.request.request_digest
            assert recovery.document_state == "verified" and recovery.receipt is not None
            assert recovery.receipt.abort_request_digest == abort_digest(live.abort_request)
            assert (
                recovery.receipt.claim_state == "received" and recovery.receipt.claimed_at is None
            )
            assert receipt_row(foundation)[0]["dispatched_at_us"] is None
            assert live.ledger.claims == 0 and live.engine.calls == [] and not future.done()
        result = future.result(timeout=4)
    assert (
        result is not None and result.intent.execution_state is ExecutionState.UNKNOWN_QUARANTINED
    )
    assert len(live.engine.calls) == 1 and live.ledger.claims == 1


def test_receive_waits_for_pool_lock_without_sending(
    foundation: Foundation, live: LiveAbort
) -> None:
    db = foundation.database
    blocker = db.blocker()
    try:
        blocker.execute(
            "SELECT pool_id FROM resource_pools WHERE pool_id=%s FOR UPDATE", ("tts-realtime",)
        )
        pid = blocker.execute("SELECT pg_backend_pid() AS pid").fetchone()["pid"]
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(live.service().abort, live.abort_request)
            try:
                assert live.ledger.receive_started.wait(2)
                deadline = time.monotonic() + 3
                while True:
                    waiters = db.query(
                        "SELECT pid FROM pg_stat_activity WHERE usename=%s "
                        "AND %s = ANY(pg_blocking_pids(pid))",
                        (foundation.gateway_role, pid),
                    )
                    if waiters:
                        break
                    assert time.monotonic() < deadline, "PostgreSQL lock wait was not observed"
                    Event().wait(0.01)
                assert receipt_row(foundation) == []
                assert not live.ledger.received.is_set()
                assert live.ledger.claims == 0 and live.engine.calls == []
            finally:
                blocker.commit()
            assert future.result(timeout=4) is not None
        assert live.ledger.received.is_set() and len(live.engine.calls) == 1
    finally:
        blocker.close()


class ReceiveFaultPool:
    """Inject an error at the actual pool connection's transaction exit."""

    def __init__(self, pool: GatewayPool, mode: Literal["rollback", "lost_ack"]) -> None:
        self.pool = pool
        self.mode = mode
        self.injected = Event()

    @contextmanager
    def connection(self, *, timeout: float) -> Iterator[Any]:
        with self.pool.connection(timeout=timeout) as connection:
            yield connection
            if self.mode == "rollback":
                self.injected.set()
                raise psycopg.errors.CheckViolation("synthetic pre-commit rollback")
        self.injected.set()  # Pool context returned only after the actual COMMIT.
        raise psycopg.OperationalError("synthetic lost commit acknowledgement")


class ReceiveFaultLedger(ObservableLedger):
    def __init__(self, pool: GatewayPool, fault: ReceiveFaultPool) -> None:
        super().__init__(pool)
        self.receive_writer = PostgresSubmissionLedger(cast(GatewayPool, fault), timeout_seconds=5)

    def receive_abort(self, request: AbortExecutionRequest) -> None:
        self.receive_started.set()
        self.receive_writer.receive_abort(request)
        self.received.set()


@pytest.mark.parametrize("mode", ["rollback", "lost_ack"])
def test_receive_failure_never_claims_and_restarted_reader_recovers_original(
    foundation: Foundation,
    live: LiveAbort,
    reader_pool: GatewayPool,
    mode: Literal["rollback", "lost_ack"],
) -> None:
    writer_pool = open_pool(foundation)
    try:
        fault = ReceiveFaultPool(writer_pool, mode)
        ledger = ReceiveFaultLedger(live.ledger._pool, fault)
        error = SubmissionOutcomeUnknownError if mode == "lost_ack" else SubmissionStoreError
        with pytest.raises(error):
            live.service(ledger).abort(live.abort_request)
        assert fault.injected.is_set()
        assert ledger.claims == 0 and live.engine.calls == []
        rows = receipt_row(foundation)
        assert len(rows) == (1 if mode == "lost_ack" else 0)
        if rows:
            assert rows[0]["dispatched_at_us"] is None
        # A new reader and a different restricted connection recover from database state,
        # not from the writer, its pool or a local receipt cache.
        recovered = PostgresExecutionReader(reader_pool).query_outcome(INTENT_ID)
        assert recovered is not None and recovered.abort_recovery is not None
        recovery = recovered.abort_recovery
        assert recovered.intent.execution_state is ExecutionState.EXECUTING
        assert recovery.document_state == "verified"
        assert recovery.execution_request_digest == live.request.request_digest
        if mode == "rollback":
            assert recovery.receipt is None
        else:
            assert recovery.receipt is not None
            assert recovery.receipt.abort_request_digest == abort_digest(live.abort_request)
            assert (
                recovery.receipt.claim_state == "received" and recovery.receipt.claimed_at is None
            )
    finally:
        writer_pool.close()


def test_exact_receive_replay_keeps_first_database_timestamp_and_conflicts(
    foundation: Foundation, live: LiveAbort
) -> None:
    live.ledger.receive_abort(live.abort_request)
    first = receipt_row(foundation)
    assert len(first) == 1 and first[0]["received_at_us"] > 0
    live.ledger.receive_abort(live.abort_request)
    assert receipt_row(foundation) == first
    for changed in (
        replace(live.abort_request, reason="deadline_exceeded"),
        replace(live.abort_request, wait_timeout_ms=2000),
    ):
        with pytest.raises(ExecutionObservationConflictError):
            live.ledger.receive_abort(changed)
        assert receipt_row(foundation) == first
    assert live.ledger.claims == 0 and live.engine.calls == []


def test_terminal_fact_after_receive_but_before_claim_is_never_rewritten(
    foundation: Foundation, live: LiveAbort
) -> None:
    with ThreadPoolExecutor(max_workers=1) as executor:
        with live.ingress.dispatch(live.request.identity):
            future = executor.submit(live.service().abort, live.abort_request)
            assert live.ledger.received.wait(3)
            evidence = evidence_for(
                foundation, evidence_kind=StopEvidenceKind.ENGINE_TERMINAL_RESPONSE
            )
            terminal = live.ledger.record_stop(RecordStop(evidence, ExecutionState.SUCCEEDED))
        result = future.result(timeout=4)
    assert result is not None and result.intent == terminal
    assert live.ledger.get(INTENT_ID) == terminal
    assert receipt_row(foundation)[0]["dispatched_at_us"] is None
    assert live.engine.calls == []


def test_terminal_fact_after_claim_survives_late_unconfirmed_abort(
    foundation: Foundation, live: LiveAbort
) -> None:
    entered, resume = Event(), Event()

    class BlockingAbort(ScriptedAbort):
        def abort(
            self, intent: SubmissionIntent, *, reason: str, wait_timeout_ms: int
        ) -> AbortOutcome:
            entered.set()
            assert resume.wait(4)
            return super().abort(intent, reason=reason, wait_timeout_ms=wait_timeout_ms)

    engine = BlockingAbort()
    service = ExecutionAbortService(
        ingress=live.ingress, ledger=live.ledger, abort=engine, durable_abort_enabled=True
    )
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(service.abort, live.abort_request)
        try:
            assert entered.wait(3)
            assert receipt_row(foundation)[0]["dispatched_at_us"] is not None
            current = live.ledger.get(INTENT_ID)
            assert current is not None
            assert current.execution_state is ExecutionState.UNKNOWN_QUARANTINED
            terminal = live.ledger.record_stop(
                RecordStop(evidence_for(foundation), ExecutionState.ABORTED)
            )
        finally:
            resume.set()
        assert future.result(timeout=4) is not None
    assert live.ledger.get(INTENT_ID) == terminal
    assert len(engine.calls) == 1


def redact_terminal_execution(foundation: Foundation, live: LiveAbort) -> SubmissionIntent:
    terminal = live.ledger.record_stop(RecordStop(evidence_for(foundation), ExecutionState.ABORTED))
    # This is a synthetic Scheduler-owned release fact, not release qualification.
    release_fixture(foundation)
    permit = AuthorizedCommandRedaction(
        RedactExecutionCommand(INTENT_ID, terminal.version, "delete-test", "sha256:" + "d" * 64),
        terminal.identity,
    )
    redacted = live.ledger.redact_execution_command(permit)
    assert not redacted.replayed
    return redacted.intent


@pytest.mark.parametrize("previously_claimed", [False, True])
@pytest.mark.parametrize("method", ["abort", "abort_idempotent"])
def test_exact_abort_receipt_replays_after_legitimate_command_redaction(
    foundation: Foundation,
    live: LiveAbort,
    reader_pool: GatewayPool,
    previously_claimed: bool,
    method: str,
) -> None:
    if previously_claimed:
        assert live.service().abort(live.abort_request) is not None
    else:
        live.ledger.receive_abort(live.abort_request)
    redacted = redact_terminal_execution(foundation, live)
    before_receipt = receipt_row(foundation)
    assert len(before_receipt) == 1
    assert (before_receipt[0]["dispatched_at_us"] is not None) is previously_claimed
    before_calls = list(live.engine.calls)
    before_intent = live.ledger.get(INTENT_ID)
    before_stop = foundation.database.query(
        "SELECT * FROM execution_stop_evidence WHERE submit_intent_id=%s", (INTENT_ID,)
    )
    assert before_intent == redacted

    result = getattr(live.service(), method)(live.abort_request)

    assert result is not None and result.intent == redacted
    assert live.ledger.get(INTENT_ID) == before_intent
    assert receipt_row(foundation) == before_receipt
    assert (
        foundation.database.query(
            "SELECT * FROM execution_stop_evidence WHERE submit_intent_id=%s", (INTENT_ID,)
        )
        == before_stop
    )
    assert live.engine.calls == before_calls
    recovered = PostgresExecutionReader(reader_pool).query_outcome(INTENT_ID)
    assert recovered is not None and recovered.abort_recovery is not None
    assert recovered.abort_recovery.document_state == "redacted"
    assert recovered.abort_recovery.receipt is not None
    assert recovered.abort_recovery.receipt.abort_request_digest == abort_digest(live.abort_request)
    for changed in (
        replace(live.abort_request, reason="deadline_exceeded"),
        replace(live.abort_request, wait_timeout_ms=2000),
    ):
        with pytest.raises(ExecutionObservationConflictError):
            getattr(live.service(), method)(changed)
    assert receipt_row(foundation) == before_receipt and live.engine.calls == before_calls


def test_first_abort_receipt_cannot_be_created_after_command_redaction(
    foundation: Foundation, live: LiveAbort
) -> None:
    redacted = redact_terminal_execution(foundation, live)
    with pytest.raises(SubmissionStoreError):
        live.service().abort(live.abort_request)
    assert receipt_row(foundation) == [] and live.engine.calls == []
    assert live.ledger.claims == 0 and live.ledger.get(INTENT_ID) == redacted


@pytest.mark.parametrize("suffix", ["abort", "abort-intents"])
def test_http_abort_replay_after_redaction_preserves_all_authoritative_rows(
    foundation: Foundation,
    live: LiveAbort,
    reader_pool: GatewayPool,
    tmp_path: Path,
    suffix: str,
) -> None:
    live.ledger.receive_abort(live.abort_request)
    terminal = redact_terminal_execution(foundation, live)
    db = foundation.database

    def snapshot() -> tuple[list[dict[str, Any]], ...]:
        return (
            db.query("SELECT * FROM submit_intents WHERE submit_intent_id=%s", (INTENT_ID,)),
            db.query("SELECT * FROM gateway_abort_intents WHERE submit_intent_id=%s", (INTENT_ID,)),
            db.query(
                "SELECT * FROM admission_leases WHERE lease_id=%s", (terminal.identity.lease_id,)
            ),
            db.query(
                "SELECT * FROM execution_stop_evidence WHERE submit_intent_id=%s", (INTENT_ID,)
            ),
        )

    before = snapshot()
    assert before[0][0]["request_document"] is None
    assert before[1][0]["dispatched_at_us"] is None
    engine = ScriptedEngine()
    app = create_app(
        settings=Settings(
            control_database_url=SecretStr(db.url),
            control_schema=db.schema,
            node_id="host-test",
            gateway_id="gateway-test",
            component_token=SecretStr(TOKEN),
            ingress_lock_directory=tmp_path,
        ),
        barrier_control=GatewayBarrierService(ingress=live.ingress, ledger=live.ledger),
        execution_query=ExecutionQueryService(
            ingress=live.ingress, ledger=PostgresExecutionReader(reader_pool)
        ),
        submit_execution=SubmitExecutionService(
            ingress=live.ingress, ledger=live.ledger, dispatch=engine, observation=engine
        ),
        abort_execution=live.service(),
    )
    path = f"/internal/v1/executions/{INTENT_ID}/{suffix}"
    response = post(app, path, abort_body())
    assert response.status_code == 200
    assert response.json()["execution_state"] == "aborted"
    for changed in (abort_body(reason="deadline_exceeded"), abort_body(wait_timeout_ms=2000)):
        conflict = post(app, path, changed)
        assert conflict.status_code == 409
        assert conflict.json()["error"]["code"] == "control_state_conflict"

    async def recover() -> httpx.Response:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://gateway"
        ) as client:
            return await client.get(
                f"/internal/v1/executions/{INTENT_ID}",
                headers={
                    **HEADERS,
                    "Accept": "application/vnd.speechnexus.execution-recovery.v1+json",
                },
            )

    recovered = asyncio.run(recover())
    assert recovered.status_code == 200
    recovery = recovered.json()["abort_recovery"]
    assert recovery["document_state"] == "redacted"
    assert recovery["execution_request_digest"] == live.request.request_digest
    assert recovery["receipt"]["abort_request_digest"] == abort_digest(live.abort_request)
    assert recovery["receipt"]["claim_state"] == "received"
    assert snapshot() == before
    assert live.engine.calls == [] and engine.dispatch_count == 0


@pytest.mark.parametrize(
    "corruption",
    [
        "deletion_digest",
        "deletion_id",
        "redacted_at",
        "after_updated",
        "unreleased",
        "active_execution",
        "request_digest",
        "command_digest",
    ],
)
def test_existing_abort_receipt_does_not_validate_an_illegal_tombstone(
    foundation: Foundation, live: LiveAbort, corruption: str
) -> None:
    live.ledger.receive_abort(live.abort_request)
    redact_terminal_execution(foundation, live)
    db = foundation.database
    if corruption == "unreleased":
        db.query(
            "UPDATE admission_leases SET lease_state='reserved', cleanup_state='pending', "
            "budget_released_at_us=NULL WHERE lease_id='lease-alt-stage-main'"
        )
    elif corruption == "after_updated":
        db.query(
            "UPDATE submit_intents SET command_redacted_at_us=updated_at_us + 1 "
            "WHERE submit_intent_id=%s",
            (INTENT_ID,),
        )
    elif corruption == "active_execution":
        db.query(
            "UPDATE submit_intents SET execution_state='executing', cleanup_state='pending', "
            "terminated_at_us=NULL WHERE submit_intent_id=%s",
            (INTENT_ID,),
        )
    else:
        column, value = {
            "deletion_digest": ("command_deletion_evidence_digest", "invalid"),
            "deletion_id": ("command_deletion_job_id", "!"),
            "redacted_at": ("command_redacted_at_us", 1),
            "request_digest": ("request_digest", "invalid"),
            "command_digest": ("command_digest", "invalid"),
        }[corruption]
        db.query(
            f"UPDATE submit_intents SET {column}=%s WHERE submit_intent_id=%s", (value, INTENT_ID)
        )
    before_receipt = receipt_row(foundation)
    before_version = db.query(
        "SELECT version FROM submit_intents WHERE submit_intent_id=%s", (INTENT_ID,)
    )
    with pytest.raises(SubmissionStoreError):
        live.service().abort(live.abort_request)
    assert receipt_row(foundation) == before_receipt
    assert (
        db.query("SELECT version FROM submit_intents WHERE submit_intent_id=%s", (INTENT_ID,))
        == before_version
    )
    assert live.ledger.claims == 0 and live.engine.calls == []


def test_changed_abort_conflicts_before_even_a_corrupt_tombstone_can_be_used(
    foundation: Foundation, live: LiveAbort
) -> None:
    live.ledger.receive_abort(live.abort_request)
    redact_terminal_execution(foundation, live)
    foundation.database.query(
        "UPDATE submit_intents SET command_deletion_evidence_digest=%s WHERE submit_intent_id=%s",
        ("invalid", INTENT_ID),
    )
    original = receipt_row(foundation)
    for changed in (
        replace(live.abort_request, reason="deadline_exceeded"),
        replace(live.abort_request, wait_timeout_ms=2000),
    ):
        with pytest.raises(ExecutionObservationConflictError):
            live.service().abort_idempotent(changed)
    assert receipt_row(foundation) == original
    assert live.ledger.claims == 0 and live.engine.calls == []


@pytest.mark.parametrize("corruption", ["document", "digest", "identity", "redacted"])
def test_missing_or_corrupt_original_document_is_never_received(
    foundation: Foundation, live: LiveAbort, corruption: str
) -> None:
    db = foundation.database
    if corruption == "redacted":
        db.query(
            "UPDATE submit_intents SET request_document=NULL, command_redacted_at_us=1, "
            "command_deletion_job_id='delete-synthetic', command_deletion_evidence_digest=%s "
            "WHERE submit_intent_id=%s",
            ("sha256:" + "d" * 64, INTENT_ID),
        )
    elif corruption == "digest":
        db.query(
            "UPDATE submit_intents SET request_digest=%s WHERE submit_intent_id=%s",
            ("sha256:" + "0" * 64, INTENT_ID),
        )
    else:
        value = json.loads(live.request.request_document)
        if corruption == "identity":
            value["execution_request"]["instance_generation"] += 1
            document = canonical_json(cast(JsonValue, value))
            digest = command_digest(cast(JsonValue, value))
        else:
            document, digest = "{}", live.request.request_digest
        db.query(
            "UPDATE submit_intents SET request_document=%s, request_digest=%s "
            "WHERE submit_intent_id=%s",
            (document, digest, INTENT_ID),
        )
    with pytest.raises(SubmissionStoreError):
        live.service().abort(live.abort_request)
    assert receipt_row(foundation) == []
    assert live.ledger.claims == 0 and live.engine.calls == []


def test_due_abort_survives_restart_and_recovers_original(live: LiveAbort) -> None:
    from speechnexus_gateway.application.recover_aborts import AbortRecoveryService

    live.ledger.receive_abort(live.abort_request)
    recovery = AbortRecoveryService(ingress=live.ingress, ledger=live.ledger, abort=live.service())
    assert recovery.sweep(limit=1) == 1
    assert len(live.engine.calls) == 1
    assert recovery.sweep(limit=1) == 0
    current = live.ledger.get(INTENT_ID)
    assert current is not None and current.execution_state is ExecutionState.UNKNOWN_QUARANTINED


def test_due_claimed_abort_waits_for_its_lease_before_any_resend(live: LiveAbort) -> None:
    from speechnexus_gateway.application.recover_aborts import AbortRecoveryService

    live.ledger.receive_abort(live.abort_request)
    assert (
        live.ledger.claim_abort(live.abort_request, attempt_id="abort-held", lease_ms=100)
        is not None
    )
    recovery = AbortRecoveryService(ingress=live.ingress, ledger=live.ledger, abort=live.service())
    # A live lease takes the receipt out of the due queue: no second sender exists.
    assert recovery.sweep(limit=1) == 0
    assert live.engine.calls == []
    time.sleep(0.2)
    assert recovery.sweep(limit=1) == 1
    assert len(live.engine.calls) == 1
    current = live.ledger.get(INTENT_ID)
    assert current is not None and current.execution_state is ExecutionState.UNKNOWN_QUARANTINED


def test_competing_due_scans_advance_one_receipt_once(
    live: LiveAbort, foundation: Foundation
) -> None:
    live.ledger.receive_abort(live.abort_request)
    db = foundation.database
    blocker = db.blocker()
    try:
        blocker.execute(
            "SELECT submit_intent_id FROM gateway_abort_intents "
            "WHERE submit_intent_id=%s FOR UPDATE",
            (INTENT_ID,),
        )
        with ThreadPoolExecutor(max_workers=2) as workers:
            futures = [
                workers.submit(
                    live.ledger.take_due_aborts,
                    gateway_id="gateway-test",
                    host_node_id="host-test",
                    limit=1,
                )
                for _ in range(2)
            ]
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                count = db.query(
                    "SELECT count(*) AS n FROM pg_stat_activity "
                    "WHERE usename=%s AND cardinality(pg_blocking_pids(pid)) > 0",
                    (foundation.gateway_role,),
                )[0]["n"]
                if count == 2:
                    break
                time.sleep(0.01)
            else:
                pytest.fail("two independent queue updates did not wait on the receipt lock")
            blocker.commit()
            results = [future.result(timeout=5) for future in futures]
        assert sorted(len(result) for result in results) == [0, 1]
        assert next(result[0] for result in results if result) == original_abort_request(
            foundation, live.abort_request
        )
    finally:
        blocker.close()


def test_due_order_does_not_starve_old_retry_behind_new_receipt(
    live: LiveAbort, foundation: Foundation
) -> None:
    live.ledger.receive_abort(live.abort_request)
    other_identity = replace(
        live.request.identity,
        submit_intent_id="intent-z",
        lease_id="lease-alt-stage-alt",
        stage_id="stage-alt",
        fencing_token=2,
    )
    other_request = SubmitRequest(
        other_identity, live.request.command, live.request.output_stream_id
    )
    sender = ScriptedEngine()
    SubmitExecutionService(
        ledger=live.ledger, ingress=live.ingress, dispatch=sender, observation=sender
    ).submit(other_request)
    other_abort = AbortExecutionRequest(
        "intent-z",
        other_identity.operation_id,
        other_identity.attempt_id,
        other_identity.fencing_token,
        "client_cancelled",
        1000,
    )
    live.ledger.receive_abort(other_abort)
    db = foundation.database
    now_us = int(time.time() * 1_000_000)
    db.query(
        "UPDATE gateway_abort_intents SET next_check_at_us=%s WHERE submit_intent_id=%s",
        (now_us - 10_000_000, INTENT_ID),
    )
    db.query(
        "UPDATE gateway_abort_intents SET received_at_us=%s WHERE submit_intent_id=%s",
        (now_us - 1_000_000, "intent-z"),
    )
    candidates = live.ledger.take_due_aborts(
        gateway_id="gateway-test", host_node_id="host-test", limit=1
    )
    assert candidates == [original_abort_request(foundation, live.abort_request)]


@pytest.mark.parametrize(
    "crash_window",
    ["after_receive", "after_claim", "after_adapter_accept", "after_outcome_lost"],
)
def test_process_exit_keeps_the_original_abort_retryable(
    live: LiveAbort, foundation: Foundation, tmp_path: Path, crash_window: str
) -> None:
    """Every crash window leaves the original receipt retryable and sends the same identity.

    `after_adapter_accept` exits inside the engine call, and `after_outcome_lost`
    exits inside the outcome write. Both leave "engine accepted, no local outcome";
    they differ only in which side of the adapter boundary the process died.
    """
    import os
    import subprocess
    import sys

    from speechnexus_gateway.domain.ports import AbortOutcome

    # The fixture's process must relinquish the actual lifetime lock before a
    # separate gateway process can own the same node and gateway identity.
    live.ingress._ownership.close()  # type: ignore[attr-defined]
    accept_log = tmp_path / "synthetic-engine-accepted"
    script = """
import os
from datetime import UTC, datetime
from pathlib import Path
from psycopg.conninfo import make_conninfo
from speechnexus_gateway.application.abort_execution import ExecutionAbortService
from speechnexus_gateway.application.ingress import GatewayIngress
from speechnexus_gateway.domain.abort_intent import AbortExecutionRequest
from speechnexus_gateway.domain.ports import AbortOutcome
from speechnexus_gateway.domain.submission import AbortAcknowledgement
from speechnexus_gateway.infrastructure.postgres_pool import gateway_pool
from speechnexus_gateway.infrastructure.process_ownership import ProcessIngressOwnership
from speechnexus_gateway.infrastructure.submit_postgres import PostgresSubmissionLedger

WINDOW = os.environ['SN_CRASH_WINDOW']

class CrashLedger(PostgresSubmissionLedger):
    def receive_abort(self, request):
        super().receive_abort(request)
        if WINDOW == 'after_receive':
            os._exit(70)

    def claim_abort(self, request, *, attempt_id, lease_ms):
        claim = super().claim_abort(request, attempt_id=attempt_id, lease_ms=lease_ms)
        if WINDOW == 'after_claim':
            os._exit(71)
        return claim

    def record_abort_dispatch(self, result):
        if WINDOW == 'after_outcome_lost':
            os._exit(73)
        return super().record_abort_dispatch(result)

class CrashEngine:
    def abort(self, intent, *, reason, wait_timeout_ms):
        Path(os.environ['SN_MARKER']).write_text(intent.identity.submit_intent_id)
        if WINDOW == 'after_adapter_accept':
            os._exit(72)
        return AbortOutcome(observed_at=datetime.now(UTC),
                            engine_request_id=intent.engine_request_id,
                            acknowledgement=AbortAcknowledgement.ABORTED)

conninfo = make_conninfo(os.environ['SN_DB'], user=os.environ['SN_ROLE'],
                         password='synthetic_foundation_test')
pool = gateway_pool(conninfo, schema=os.environ['SN_SCHEMA'])
pool.open()
owner = ProcessIngressOwnership(Path(os.environ['SN_LOCK']), node_id='host-test',
                                gateway_id='gateway-test')
service = ExecutionAbortService(ingress=GatewayIngress(owner), ledger=CrashLedger(pool),
                                abort=CrashEngine(), durable_abort_enabled=True)
service.abort(AbortExecutionRequest('intent-a', 'operation-test', 'attempt-alt', 1,
                                    'client_cancelled', 1000))
"""
    env = os.environ.copy()
    env.update(
        SN_DB=foundation.database.url,
        SN_ROLE=foundation.gateway_role,
        SN_SCHEMA=foundation.database.schema,
        SN_LOCK=str(tmp_path),
        SN_MARKER=str(accept_log),
        SN_CRASH_WINDOW=crash_window,
    )
    result = subprocess.run(
        [sys.executable, "-c", script], env=env, capture_output=True, timeout=15
    )
    expected_code = {
        "after_receive": 70,
        "after_claim": 71,
        "after_adapter_accept": 72,
        "after_outcome_lost": 73,
    }[crash_window]
    assert result.returncode == expected_code, result.stderr.decode()
    engine_ran = crash_window in ("after_adapter_accept", "after_outcome_lost")
    assert accept_log.exists() is engine_ran
    if not engine_ran:
        assert dispatch_row(foundation)["dispatch_attempt_id"] is not None or crash_window == (
            "after_receive"
        )
    # Simulate the bounded lease expiring; the retry itself is decided from durable
    # facts, not from the memory of the process that crashed.
    foundation.database.query(
        "UPDATE gateway_abort_intents SET dispatch_deadline_at_us=dispatch_started_at_us + 1 "
        "WHERE submit_intent_id=%s AND dispatch_attempt_id IS NOT NULL",
        (INTENT_ID,),
    )

    class IdempotentAbort:
        """The same original identity is accepted at most once, however often it is sent."""

        def __init__(self) -> None:
            self.intents: list[SubmissionIntent] = []

        def abort(
            self, intent: SubmissionIntent, *, reason: str, wait_timeout_ms: int
        ) -> AbortOutcome:
            assert intent.identity == live.request.identity
            assert intent.request_digest == live.request.request_digest
            assert intent.engine_request_id is not None
            assert (reason, wait_timeout_ms) == ("client_cancelled", 1000)
            self.intents.append(intent)
            accepted = accept_log.read_text().splitlines() if accept_log.exists() else []
            if intent.identity.submit_intent_id not in accepted:
                accept_log.write_text(
                    "\n".join([*accepted, intent.identity.submit_intent_id]) + "\n"
                )
            return AbortOutcome(
                observed_at=datetime.now(UTC),
                engine_request_id=intent.engine_request_id,
                acknowledgement=AbortAcknowledgement.ABORTED,
            )

    owner = ProcessIngressOwnership(tmp_path, node_id="host-test", gateway_id="gateway-test")
    engine = IdempotentAbort()
    try:
        service = ExecutionAbortService(
            ingress=GatewayIngress(owner),
            ledger=live.ledger,
            abort=engine,
            durable_abort_enabled=True,
        )
        service.recover_abort(original_abort_request(foundation, live.abort_request))
        assert len(engine.intents) == 1
        assert accept_log.read_text().splitlines() == [INTENT_ID]
        row = dispatch_row(foundation)
        assert row["dispatch_outcome"] == "acknowledged"
        assert row["dispatch_attempt_id"] != "abort-unknown"
        assert row["dispatch_attempts"] == (1 if crash_window == "after_receive" else 2)
        current = live.ledger.get(INTENT_ID)
        assert current is not None
        assert current.execution_state is ExecutionState.UNKNOWN_QUARANTINED
        executions = foundation.database.query(
            "SELECT count(*) AS n FROM submit_intents WHERE submit_intent_id=%s", (INTENT_ID,)
        )[0]["n"]
        assert executions == 1
    finally:
        owner.close()


def test_repeated_dispatch_records_the_verified_stop_once(
    foundation: Foundation, live: LiveAbort
) -> None:
    """A decided abort ends the receipt: the same stop evidence is never recorded twice."""
    evidence = evidence_for(foundation, submit_intent_id=INTENT_ID)
    live.engine.script_outcome(
        AbortOutcome(
            observed_at=datetime.now(UTC),
            evidence=evidence,
            acknowledgement=AbortAcknowledgement.ABORTED,
        )
    )
    first = live.service().abort(live.abort_request)
    assert first is not None and first.intent.execution_state is ExecutionState.ABORTED
    row = dispatch_row(foundation)
    assert row["dispatch_outcome"] == "acknowledged"
    assert (
        foundation.database.query(
            "SELECT count(*) AS n FROM execution_stop_evidence WHERE submit_intent_id=%s",
            (INTENT_ID,),
        )[0]["n"]
        == 1
    )
    # A later caller holding the same original receipt has no dispatch authority.
    second = live.service().abort(live.abort_request)
    assert second is not None and second.intent.execution_state is ExecutionState.ABORTED
    assert len(live.engine.calls) == 1
    assert (
        foundation.database.query(
            "SELECT count(*) AS n FROM execution_stop_evidence WHERE submit_intent_id=%s",
            (INTENT_ID,),
        )[0]["n"]
        == 1
    )
    assert (
        foundation.database.query(
            "SELECT count(*) AS n FROM submit_intents WHERE submit_intent_id=%s", (INTENT_ID,)
        )[0]["n"]
        == 1
    )


def test_recovery_refuses_a_receipt_recorded_against_another_generation(
    foundation: Foundation, live: LiveAbort
) -> None:
    """A receipt whose origin no longer matches the authority is never sent again."""
    live.ledger.receive_abort(live.abort_request)
    foundation.database.query(
        "UPDATE gateway_abort_intents SET instance_generation=instance_generation + 1 "
        "WHERE submit_intent_id=%s",
        (INTENT_ID,),
    )
    drifted = original_abort_request(foundation, live.abort_request)
    result = live.service().recover_abort(drifted)
    assert result is not None and result.intent.execution_state is ExecutionState.EXECUTING
    assert live.engine.calls == []
    row = dispatch_row(foundation)
    assert row["dispatch_outcome"] == "rejected_fenced" and row["dispatch_attempts"] == 0
    # The refusal is durable: no later attempt can resend this receipt.
    live.service().recover_abort(drifted)
    assert live.engine.calls == []


def test_lost_outcome_acknowledgement_does_not_resend_once_recorded(
    live: LiveAbort, foundation: Foundation, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The outcome committed but the caller never learned it: the fact is authoritative."""
    stored = live.ledger.get(INTENT_ID)
    assert stored is not None
    live.engine.script_outcome(
        AbortOutcome(
            observed_at=datetime.now(UTC),
            engine_request_id=stored.engine_request_id,
            acknowledgement=AbortAcknowledgement.ABORTED,
        )
    )
    original = live.ledger.record_abort_dispatch

    def committed_then_lost(result: Any) -> bool:
        assert original(result)
        raise SubmissionOutcomeUnknownError("synthetic lost outcome acknowledgement")

    monkeypatch.setattr(live.ledger, "record_abort_dispatch", committed_then_lost)
    with pytest.raises(SubmissionOutcomeUnknownError):
        live.service().abort(live.abort_request)
    assert dispatch_row(foundation)["dispatch_outcome"] == "acknowledged"
    monkeypatch.setattr(live.ledger, "record_abort_dispatch", original)
    live.service().recover_abort(original_abort_request(foundation, live.abort_request))
    assert len(live.engine.calls) == 1


def _database_now_us(foundation: Foundation) -> int:
    return int(
        foundation.database.query(
            "SELECT CAST(EXTRACT(EPOCH FROM clock_timestamp()) * 1000000 AS BIGINT) AS now_us"
        )[0]["now_us"]
    )


def _expire_abort_attempt(foundation: Foundation) -> None:
    """Make the recorded lease and its backoff due without waiting real time."""
    foundation.database.query(
        "UPDATE gateway_abort_intents SET dispatch_deadline_at_us=dispatch_started_at_us + 1, "
        "next_check_at_us=0 WHERE submit_intent_id=%s",
        (INTENT_ID,),
    )


def test_undecided_attempts_back_off_and_stop_at_the_bounded_cap(
    live: LiveAbort, foundation: Foundation
) -> None:
    """Retries grow their delay and end in a terminal outcome instead of looping forever."""
    from speechnexus_gateway.application.recover_aborts import AbortRecoveryService
    from speechnexus_gateway.domain.abort_intent import MAX_ABORT_DISPATCH_ATTEMPTS

    live.ledger.receive_abort(live.abort_request)
    # The adapter observation is an audit fact only: even an hour-old answer must
    # schedule the retry from the database clock, not from the reported instant.
    skewed = datetime.now(UTC) - timedelta(hours=1)
    live.engine.script_outcome(AbortOutcome(observed_at=skewed))
    service = AbortRecoveryService(ingress=live.ingress, ledger=live.ledger, abort=live.service())
    delays: list[int] = []
    for attempt in range(1, MAX_ABORT_DISPATCH_ATTEMPTS + 1):
        assert service.sweep(limit=1) == 1
        assert len(live.engine.calls) == attempt
        row = dispatch_row(foundation)
        assert row["dispatch_attempts"] == attempt
        if attempt < MAX_ABORT_DISPATCH_ATTEMPTS:
            assert row["dispatch_outcome"] == "unconfirmed"
            delays.append(row["next_check_at_us"] - _database_now_us(foundation))
            if attempt == 1:
                assert row["dispatch_outcome_at_us"] <= _database_now_us(foundation) - 3_500_000_000
        else:
            assert row["dispatch_outcome"] == "exhausted"
        _expire_abort_attempt(foundation)
    assert 29_000_000 <= delays[0] <= 30_000_000
    assert 58_000_000 <= delays[1] <= 60_000_000
    assert delays == sorted(delays) and max(delays) <= 1_800_000_000
    assert service.sweep(limit=1) == 0
    assert len(live.engine.calls) == MAX_ABORT_DISPATCH_ATTEMPTS
    # A crash inside the final attempt leaves no outcome; the next sweep gives up too.
    foundation.database.query(
        "UPDATE gateway_abort_intents SET dispatch_outcome=NULL, dispatch_outcome_at_us=NULL "
        "WHERE submit_intent_id=%s",
        (INTENT_ID,),
    )
    _expire_abort_attempt(foundation)
    assert service.sweep(limit=1) == 1
    assert len(live.engine.calls) == MAX_ABORT_DISPATCH_ATTEMPTS
    assert dispatch_row(foundation)["dispatch_outcome"] == "exhausted"
    assert service.sweep(limit=1) == 0


def _stop_evidence_rows(foundation: Foundation) -> int:
    return int(
        foundation.database.query(
            "SELECT count(*) AS n FROM execution_stop_evidence WHERE submit_intent_id=%s",
            (INTENT_ID,),
        )[0]["n"]
    )


def test_recorded_stop_evidence_survives_a_lost_dispatch_outcome(
    live: LiveAbort, foundation: Foundation, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The stop fact commits before the outcome; recovery reads it and sends nothing."""
    live.engine.script_outcome(
        AbortOutcome(
            observed_at=datetime.now(UTC),
            evidence=evidence_for(foundation, submit_intent_id=INTENT_ID),
            acknowledgement=AbortAcknowledgement.ABORTED,
        )
    )
    original = live.ledger.record_abort_dispatch

    def lost_before_writing(result: Any) -> bool:
        raise SubmissionOutcomeUnknownError("synthetic lost dispatch outcome")

    monkeypatch.setattr(live.ledger, "record_abort_dispatch", lost_before_writing)
    with pytest.raises(SubmissionOutcomeUnknownError):
        live.service().abort(live.abort_request)
    assert len(live.engine.calls) == 1
    stopped = live.ledger.get(INTENT_ID)
    assert stopped is not None and stopped.execution_state is ExecutionState.ABORTED
    assert _stop_evidence_rows(foundation) == 1
    pending = dispatch_row(foundation)
    assert pending["dispatch_outcome"] is None and pending["dispatch_attempts"] == 1
    monkeypatch.setattr(live.ledger, "record_abort_dispatch", original)
    recovered = live.service().recover_abort(original_abort_request(foundation, live.abort_request))
    assert recovered is not None and recovered.intent.execution_state is ExecutionState.ABORTED
    assert len(live.engine.calls) == 1
    assert _stop_evidence_rows(foundation) == 1
    assert dispatch_row(foundation)["dispatch_outcome"] is None
