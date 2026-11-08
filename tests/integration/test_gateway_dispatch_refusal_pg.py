"""Real PostgreSQL keeps a dispatch-time refusal deterministic and separate."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from typing import Any, cast

import pytest
from tests.fake.execution import fake_ingress
from tests.fake.execution_command import command_example
from tests.integration.test_coordination_foundation import Foundation
from tests.integration.test_coordination_foundation import foundation as foundation
from tests.integration.test_gateway_recovery import Ledger
from tests.integration.test_gateway_recovery import recovery_ledger as recovery_ledger
from tests.integration.test_gateway_stop_evidence import stop_ledger as stop_ledger
from tests.integration.test_gateway_submit_ledger import identity_for, seed_second_attempt
from tests.integration.test_production_jobs import database as database

from speechnexus_gateway.application.command_dispatch import OriginalCommandDispatch
from speechnexus_gateway.application.submit_execution import SubmitExecutionService
from speechnexus_gateway.domain.canonical import JsonValue, command_digest
from speechnexus_gateway.domain.file_execution import FileCommandInvalidError, FileExecutionLimits
from speechnexus_gateway.domain.ports import DispatchOutcome, EngineObservation
from speechnexus_gateway.domain.recovery import MarkExecutionUnknown
from speechnexus_gateway.domain.submission import (
    ExecutionCleanupPendingError,
    ExecutionErrorCode,
    ExecutionState,
    SubmissionIntent,
    SubmitRequest,
)
from speechnexus_gateway.infrastructure.file_execution_worker import FileExecutionWorker

pytestmark = [
    pytest.mark.integration,
    pytest.mark.parametrize("database", ["postgresql"], indirect=True),
]

_COLUMNS = (
    "execution_state, cleanup_state, error_code, dispatched_at_us, engine_request_id, "
    "version, generated_pcm_samples, staging_output_object_key"
)


class Refusing:
    """The lane seam: validation always refuses the frozen command structurally."""

    def validate_request(self, request: SubmitRequest) -> SubmitRequest:
        raise FileCommandInvalidError("synthetic structural refusal")

    async def stage_result(self, *args: Any, **kwargs: Any) -> Any:
        raise AssertionError("a refused dispatch never stages")


class LostEngine:
    def dispatch(self, request: SubmitRequest) -> DispatchOutcome:
        raise ConnectionError("synthetic lost response")


class NoObservation:
    def observe(self, intent: SubmissionIntent) -> EngineObservation | None:
        return None


def _request(lease_id: str, submit_intent_id: str) -> SubmitRequest:
    """A real contract-valid file command document, not a synthetic body."""
    command = cast(JsonValue, command_example(1))
    identity = identity_for(lease_id, submit_intent_id=submit_intent_id)
    return SubmitRequest(
        identity=replace(
            identity,
            command_digest=command_digest(command),
            sample_rate_hz=None,
            max_output_audio_ms=None,
        ),
        command=command,
        output_stream_id=None,
    )


def _row(foundation: Foundation, submit_intent_id: str) -> dict[str, Any]:
    rows = foundation.database.query(
        f"SELECT {_COLUMNS} FROM submit_intents WHERE submit_intent_id = %s",
        (submit_intent_id,),
    )
    assert len(rows) == 1
    return rows[0]


def _stop_evidence(foundation: Foundation, submit_intent_id: str) -> int:
    return int(
        foundation.database.query(
            "SELECT count(*) AS n FROM execution_stop_evidence WHERE submit_intent_id = %s",
            (submit_intent_id,),
        )[0]["n"]
    )


def test_a_dispatch_refusal_is_persisted_as_a_deterministic_terminal(
    foundation: Foundation, recovery_ledger: Ledger
) -> None:
    seed_second_attempt(foundation)
    request = _request("lease-alt-stage-main", "intent-refused")
    ingress = fake_ingress(node_id="host-test", gateway_id="gateway-test")
    worker = FileExecutionWorker(
        ingress=ingress,
        ledger=recovery_ledger,
        execution=Refusing(),
        results=cast(Any, object()),
        limits=FileExecutionLimits(1024, 16384, 2, 10),
        engine_request_prefix="file-command",
    )
    worker.open()
    try:
        submit = SubmitExecutionService(
            ingress=ingress,
            ledger=recovery_ledger,
            dispatch=OriginalCommandDispatch(
                commands=recovery_ledger, engine=worker, max_document_bytes=16384
            ),
            observation=worker,
            committed_execution=worker,
        )
        outcome = submit.submit(request)
        assert not outcome.idempotent_replay

        assert _row(foundation, "intent-refused") == {
            "execution_state": "failed",
            "cleanup_state": "not_required",
            "error_code": "invalid_execution_request",
            "dispatched_at_us": None,
            "engine_request_id": None,
            "version": 2,
            "generated_pcm_samples": 0,
            "staging_output_object_key": None,
        }
        assert _stop_evidence(foundation, "intent-refused") == 0
        assert outcome.intent.execution_state is ExecutionState.FAILED
        assert outcome.intent.error_code is ExecutionErrorCode.INVALID_EXECUTION_REQUEST
        assert not outcome.intent.execution_state.holds_capacity

        # A repeated submit reads the original identity; it never dispatches or
        # creates a replacement execution.
        replay = submit.submit(request)
        assert replay.idempotent_replay
        assert replay.intent == outcome.intent
        count = foundation.database.query(
            "SELECT count(*) AS n FROM submit_intents WHERE lease_id = 'lease-alt-stage-main'"
        )[0]["n"]
        assert count == 1
        assert (
            recovery_ledger.list_unfinished(
                gateway_id="gateway-test", host_node_id="host-test", limit=10
            )
            == []
        )

        # Recovery may not turn the terminal refusal into an ambiguous execution.
        with pytest.raises(ExecutionCleanupPendingError):
            recovery_ledger.mark_execution_unknown(
                MarkExecutionUnknown.for_intent(
                    outcome.intent,
                    reason_code=ExecutionErrorCode.ENGINE_TRANSPORT_UNKNOWN,
                    at=datetime.now(UTC),
                )
            )
        assert _row(foundation, "intent-refused")["execution_state"] == "failed"
    finally:
        worker.close()


def test_an_unknown_handoff_is_persisted_as_engine_transport_unknown(
    foundation: Foundation, recovery_ledger: Ledger
) -> None:
    seed_second_attempt(foundation)
    request = _request("lease-alt-stage-main", "intent-lost")
    submit = SubmitExecutionService(
        ingress=fake_ingress(node_id="host-test", gateway_id="gateway-test"),
        ledger=recovery_ledger,
        dispatch=OriginalCommandDispatch(
            commands=recovery_ledger,
            engine=cast(Any, LostEngine()),
            max_document_bytes=16384,
        ),
        observation=NoObservation(),
    )
    submit.submit(request)

    row = _row(foundation, "intent-lost")
    assert row["execution_state"] == "unknown_quarantined"
    assert row["cleanup_state"] == "quarantined"
    assert row["error_code"] == "engine_transport_unknown"
    assert row["dispatched_at_us"] is not None
    assert row["engine_request_id"] is None
    assert _stop_evidence(foundation, "intent-lost") == 0
