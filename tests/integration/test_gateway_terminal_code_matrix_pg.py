"""Real PostgreSQL terminal matrix: one error code, one dispatch/capacity shape.

Every path that ends an execution records a stable code together with
``cleanup_state``, ``dispatched_at`` and whether capacity is still held. This file
drives each path through the production ledger and the real worker seam, then
asserts the complete matrix in one place: a code that could mean both "never
left" and "may have left" would fail here.
"""

from __future__ import annotations

import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest
from tests.fake.execution import fake_ingress
from tests.fake.execution_command import command_example
from tests.integration.test_coordination_foundation import Foundation
from tests.integration.test_coordination_foundation import foundation as foundation
from tests.integration.test_gateway_recovery import Ledger
from tests.integration.test_gateway_recovery import recovery_ledger as recovery_ledger
from tests.integration.test_gateway_stop_evidence import stop_ledger as stop_ledger
from tests.integration.test_gateway_submit_ledger import (
    LEASE_DEADLINE_AT_US,
    identity_for,
)
from tests.integration.test_production_jobs import database as database

from speechnexus_gateway.application.command_dispatch import OriginalCommandDispatch
from speechnexus_gateway.application.submit_execution import SubmitExecutionService
from speechnexus_gateway.domain.canonical import JsonValue, command_digest
from speechnexus_gateway.domain.file_execution import (
    EngineRequestRejectedError,
    FileCommandInvalidError,
    FileExecutionLimits,
)
from speechnexus_gateway.domain.ports import DispatchOutcome, EngineObservation
from speechnexus_gateway.domain.submission import (
    ExecutionState,
    SubmissionIntent,
    SubmitRequest,
)
from speechnexus_gateway.file_speech_runtime import _RoutedExecution, _Stores
from speechnexus_gateway.infrastructure.file_execution_worker import FileExecutionWorker

pytestmark = [
    pytest.mark.integration,
    pytest.mark.parametrize("database", ["postgresql"], indirect=True),
]

_COLUMNS = (
    "execution_state, cleanup_state, error_code, dispatched_at_us, engine_request_id, version"
)


class PassThrough:
    """A stage seam that accepts the frozen request and never touches an engine."""

    def validate_request(self, request: SubmitRequest) -> SubmitRequest:
        return request

    async def stage_result(self, *args: Any, **kwargs: Any) -> Any:
        raise AssertionError("this case never stages")


class Refusing(PassThrough):
    def validate_request(self, request: SubmitRequest) -> SubmitRequest:
        raise FileCommandInvalidError("synthetic structural refusal")


class Mismatching(PassThrough):
    """The frozen request no longer describes the submitted document."""

    def validate_request(self, request: SubmitRequest) -> SubmitRequest:
        return replace(
            request,
            command=cast(JsonValue, {**cast(dict[str, Any], request.command), "kind": "other"}),
        )


class Rejecting(PassThrough):
    async def stage_result(self, *args: Any, **kwargs: Any) -> Any:
        raise EngineRequestRejectedError()


class LostEngine:
    def dispatch(self, request: SubmitRequest) -> DispatchOutcome:
        raise ConnectionError("synthetic lost response")


class NoopEngine:
    def dispatch(self, request: SubmitRequest) -> DispatchOutcome:
        raise AssertionError("an unavailable command source never reaches the engine")


class UnreadableCommands:
    def read_request(self, intent: SubmissionIntent, *, max_document_bytes: int) -> SubmitRequest:
        raise RuntimeError("synthetic command document read failure")


class MismatchedCommands:
    def read_request(self, intent: SubmissionIntent, *, max_document_bytes: int) -> SubmitRequest:
        return replace(intent_request(intent), command=cast(JsonValue, {"kind": "other"}))


class _DelayedEngine:
    """Let the frozen deadline expire between admission and the worker's guard."""

    def __init__(self, worker: FileExecutionWorker[Any], *, seconds: float) -> None:
        self._worker = worker
        self._seconds = seconds

    def dispatch(self, request: SubmitRequest) -> DispatchOutcome:
        time.sleep(self._seconds)
        return self._worker.dispatch(request)


class NoObservation:
    def observe(self, intent: SubmissionIntent) -> EngineObservation | None:
        return None


def intent_request(intent: SubmissionIntent) -> SubmitRequest:
    """The frozen request a command source would have read back for this intent."""
    return SubmitRequest(
        identity=intent.identity,
        command=cast(JsonValue, command_example(0)),
        output_stream_id=intent.output_stream_id,
    )


def _seed_leases(foundation: Foundation, lease_ids: list[str]) -> None:
    foundation.database.query(
        "INSERT INTO admission_attempts (operation_id, attempt_id, workspace_id, "
        "binding_revision_id, manifest_id, recipe_digest, policy_revision_id, service_class, "
        "request_digest, required_stage_ids) VALUES ('operation-test', 'attempt-alt', "
        "'workspace-test', 'binding-test', 'manifest-test', %s, 'policy-test', 'realtime', "
        "%s, %s)",
        ("sha256:" + "a" * 64, "sha256:" + "a" * 64, '["stage-main"]'),
    )
    for lease_id in lease_ids:
        foundation.database.query(
            "INSERT INTO admission_leases (lease_id, operation_id, attempt_id, stage_id, "
            "host_node_id, instance_id, instance_generation, pool_id, gateway_id, fencing_token, "
            "reserved_slots, deadline_at_us, reserved_unified_physical_memory_bytes) VALUES "
            "(%s, 'operation-test', 'attempt-alt', 'stage-main', 'host-test', 'instance-test', 1, "
            "'tts-realtime', 'gateway-test', 1, 1, %s, 1)",
            (lease_id, LEASE_DEADLINE_AT_US),
        )


def _command_request(lease_id: str, submit_intent_id: str) -> SubmitRequest:
    command = cast(JsonValue, command_example(0))
    identity = replace(
        identity_for(lease_id, submit_intent_id=submit_intent_id),
        command_digest=command_digest(command),
        sample_rate_hz=24000,
        max_output_audio_ms=2000,
    )
    return SubmitRequest(identity=identity, command=command, output_stream_id="tts-main")


def _service(
    ledger: Ledger,
    *,
    dispatch: Any,
    observation: Any = None,
    committed: Any = None,
    now: Any = None,
) -> SubmitExecutionService:
    return SubmitExecutionService(
        ingress=fake_ingress(node_id="host-test", gateway_id="gateway-test"),
        ledger=ledger,
        dispatch=dispatch,
        observation=observation if observation is not None else NoObservation(),
        committed_execution=committed,
        now=now,
    )


def _worker(
    ledger: Ledger,
    execution: Any,
    *,
    now: Any = None,
    results: Any = None,
) -> FileExecutionWorker[Any]:
    worker: FileExecutionWorker[Any] = FileExecutionWorker(
        ingress=fake_ingress(node_id="host-test", gateway_id="gateway-test"),
        ledger=ledger,
        execution=execution,
        results=results if results is not None else cast(Any, object()),
        limits=FileExecutionLimits(1024, 16384, 2, 10),
        engine_request_prefix="file-command",
        **(cast(dict[str, Any], {}) if now is None else {"now": now}),
    )
    worker.open()
    return worker


def _wait_for(check: Any) -> None:
    """Wait for the asynchronous stage fact; the row assertion reports the outcome."""
    deadline = time.monotonic() + 5
    while not check() and time.monotonic() < deadline:
        time.sleep(0.01)


def _row(foundation: Foundation, submit_intent_id: str) -> dict[str, Any]:
    rows = foundation.database.query(
        f"SELECT {_COLUMNS} FROM submit_intents WHERE submit_intent_id = %s",
        (submit_intent_id,),
    )
    assert len(rows) == 1
    return rows[0]


def _bridge(ledger: Ledger, engine: Any) -> OriginalCommandDispatch:
    return OriginalCommandDispatch(commands=ledger, engine=engine, max_document_bytes=16384)


@pytest.mark.parametrize(
    ("case", "submit_intent_id"),
    [
        ("prep-invalid", "intent-matrix-prep-invalid"),
        ("document-mismatch", "intent-matrix-document-mismatch"),
        ("deadline", "intent-matrix-deadline"),
        ("worker-unavailable", "intent-matrix-worker-unavailable"),
        ("command-read-failure", "intent-matrix-command-read-failure"),
        ("command-mismatch", "intent-matrix-command-mismatch"),
        ("stage-store-unavailable", "intent-matrix-stage-store-unavailable"),
        ("engine-rejected", "intent-matrix-engine-rejected"),
        ("lost-response", "intent-matrix-lost-response"),
    ],
)
def test_terminal_matrix_snapshot(
    foundation: Foundation,
    recovery_ledger: Ledger,
    case: str,
    submit_intent_id: str,
) -> None:
    lease_id = "lease-" + case
    _seed_leases(foundation, [lease_id])
    request = _command_request(lease_id, submit_intent_id)

    if case in {"prep-invalid", "document-mismatch", "deadline", "engine-rejected"}:
        execution = {
            "prep-invalid": Refusing(),
            "document-mismatch": Mismatching(),
            "deadline": PassThrough(),
            "engine-rejected": Rejecting(),
        }[case]
        if case == "deadline":
            # Admit while the deadline is still ahead, then let it expire before
            # the worker's local guard runs. The persisted fact must stay inside
            # the ledger's database clock, so the delay is real elapsed time.
            request = replace(
                request,
                identity=replace(
                    request.identity, deadline_at=datetime.now(UTC) + timedelta(seconds=3)
                ),
            )
        worker = _worker(recovery_ledger, execution)
        try:
            engine: Any = worker
            if case == "deadline":
                engine = _DelayedEngine(worker, seconds=3.5)
            dispatch = _bridge(recovery_ledger, engine)
            submit = _service(
                recovery_ledger,
                dispatch=dispatch,
                observation=worker,
                committed=worker,
            )
            submit.submit(request)
            if case == "engine-rejected":
                _wait_for(
                    lambda: (
                        _row(foundation, submit_intent_id)["execution_state"]
                        == "unknown_quarantined"
                    )
                )
            assert _row(foundation, submit_intent_id)["version"] >= 2
        finally:
            worker.close()
    elif case == "worker-unavailable":
        worker = _worker(recovery_ledger, PassThrough())
        worker.close()
        submit = _service(
            recovery_ledger,
            dispatch=_bridge(recovery_ledger, worker),
            observation=worker,
            committed=worker,
        )
        submit.submit(request)
    elif case == "command-read-failure":
        submit = _service(
            recovery_ledger,
            dispatch=OriginalCommandDispatch(
                commands=UnreadableCommands(), engine=NoopEngine(), max_document_bytes=16384
            ),
        )
        submit.submit(request)
    elif case == "command-mismatch":
        submit = _service(
            recovery_ledger,
            dispatch=OriginalCommandDispatch(
                commands=MismatchedCommands(), engine=NoopEngine(), max_document_bytes=16384
            ),
        )
        submit.submit(request)
    elif case == "stage-store-unavailable":
        routed = _RoutedExecution(None, cast(Any, PassThrough()), None)
        worker = _worker(recovery_ledger, routed, results=_Stores(None, None, None))
        try:
            submit = _service(
                recovery_ledger,
                dispatch=_bridge(recovery_ledger, worker),
                observation=worker,
                committed=worker,
            )
            submit.submit(request)
            _wait_for(lambda: _row(foundation, submit_intent_id)["execution_state"] == "failed")
        finally:
            worker.close()
    elif case == "lost-response":
        submit = _service(
            recovery_ledger,
            dispatch=OriginalCommandDispatch(
                commands=recovery_ledger,
                engine=LostEngine(),
                max_document_bytes=16384,
            ),
        )
        submit.submit(request)
    else:  # pragma: no cover - the parametrization is exhaustive
        raise AssertionError(case)

    row = _row(foundation, submit_intent_id)
    print(
        "| {case} | {execution_state} | {cleanup_state} | {error_code} | "
        "{dispatched_at_us} |".format(case=case, **row)
    )

    expected = {
        "prep-invalid": ("failed", "not_required", "invalid_execution_request", None),
        "document-mismatch": ("failed", "not_required", "invalid_execution_request", None),
        "deadline": ("failed", "not_required", "lease_not_active", None),
        "worker-unavailable": ("failed", "not_required", "policy_or_resource_disabled", None),
        "command-read-failure": ("failed", "not_required", "policy_or_resource_disabled", None),
        "command-mismatch": ("failed", "not_required", "invalid_execution_request", None),
        "stage-store-unavailable": ("failed", "not_required", "invalid_execution_request", None),
        "engine-rejected": (
            "unknown_quarantined",
            "quarantined",
            "execution_cleanup_pending",
            "not-null",
        ),
        "lost-response": (
            "unknown_quarantined",
            "quarantined",
            "engine_transport_unknown",
            "not-null",
        ),
    }[case]
    state, cleanup, code, dispatch_expectation = expected
    assert row["execution_state"] == state
    assert row["cleanup_state"] == cleanup
    assert row["error_code"] == code
    if dispatch_expectation is None:
        assert row["dispatched_at_us"] is None
        assert row["engine_request_id"] is None
        assert not ExecutionState(row["execution_state"]).holds_capacity
    else:
        assert row["dispatched_at_us"] is not None
        assert ExecutionState(row["execution_state"]).holds_capacity


def test_the_unavailable_worker_really_never_dispatches(
    foundation: Foundation, recovery_ledger: Ledger
) -> None:
    """The closed-worker case must not leave a queued preparation behind."""
    _seed_leases(foundation, ["lease-worker-check"])
    request = _command_request("lease-worker-check", "intent-worker-check")
    worker = _worker(recovery_ledger, PassThrough())
    worker.close()
    submit = _service(
        recovery_ledger,
        dispatch=_bridge(recovery_ledger, worker),
        observation=worker,
        committed=worker,
    )
    submit.submit(request)
    assert _row(foundation, "intent-worker-check")["error_code"] == ("policy_or_resource_disabled")
    assert worker._preparations == {}
