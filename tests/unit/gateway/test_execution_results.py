"""Private result registration must never manufacture success or release evidence."""

from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from speechnexus_contracts.execution import parse

from speechnexus_gateway.domain.asr_results import PreparedAsrResult
from speechnexus_gateway.domain.execution_results import ExecutionStagingResult
from speechnexus_gateway.domain.observation import ExecutionObservation, RecordExecutionObservation
from speechnexus_gateway.domain.recovery import ExecutionObservationConflictError, ExecutionSnapshot
from speechnexus_gateway.domain.submission import (
    CleanupState,
    ExecutionCleanupPendingError,
    ExecutionState,
    InvalidExecutionRequestError,
    StaleSubmissionVersionError,
    SubmissionIntent,
)
from tests.fake.execution import InMemorySubmissionLedger
from tests.unit.gateway.test_barrier_http import HEADERS, app_for
from tests.unit.gateway.test_execution_observation import boundary_for
from tests.unit.gateway.test_execution_recovery import pending
from tests.unit.gateway.test_submit_execution import NOW
from tests.unit.gateway.test_submit_ledger_sql import INTENT

LATER = NOW + timedelta(seconds=2)


def result_for(
    intent: SubmissionIntent = INTENT, *, version: str = "synthetic-version"
) -> ExecutionStagingResult:
    prepared = PreparedAsrResult(
        intent.identity.workspace_id, intent.request_digest, b'{"text":""}'
    )
    return ExecutionStagingResult(
        prepared.request_digest, prepared.reference("synthetic-private"), version
    )


def executing() -> SubmissionIntent:
    current = replace(
        INTENT,
        identity=replace(INTENT.identity, sample_rate_hz=None, max_output_audio_ms=None),
        output_stream_id=None,
    )
    current.mark_dispatched("engine-request-1", at=NOW)
    return current


def command(
    current: SubmissionIntent, result: ExecutionStagingResult | None = None
) -> RecordExecutionObservation:
    return RecordExecutionObservation(
        ExecutionObservation.from_intent(current),
        current.version,
        result_for(current) if result is None else result,
    )


def test_receipt_roundtrip_is_bounded_canonical_and_keeps_fixed_version() -> None:
    result = result_for()
    assert ExecutionStagingResult.from_document(result.document) == result
    parse("ExecutionStagingResult", result.value())
    for bad in [
        result.document + " ",
        result.document.replace("{", '{"extra":1,', 1),
        result.document.replace("{", '{"storage_version":"hidden",', 1),
        "x" * 16385,
        "[" * 1200 + "0" + "]" * 1200,
    ]:
        with pytest.raises(ValueError):
            ExecutionStagingResult.from_document(bad)


@pytest.mark.parametrize("version", ["", "null", "x\nhidden", "\0", "\x7f", "x" * 1025, 4])
def test_unusable_storage_versions_are_not_receipts(version: Any) -> None:
    with pytest.raises(ValueError):
        result_for(version=version)


def test_first_registration_changes_only_version_time_and_result() -> None:
    current = executing()
    write = command(current)
    saved = write.apply(current, at=LATER)
    assert saved.version == current.version + 1
    assert saved.has_staging_output and saved.staging_result == write.staging_result
    assert saved.execution_state is ExecutionState.EXECUTING
    assert saved.cleanup_state is CleanupState.PENDING
    assert saved.stop_evidence_digest is None and saved.terminated_at is None
    assert saved.generated_pcm_samples == current.generated_pcm_samples
    assert saved.identity == current.identity
    with pytest.raises(StaleSubmissionVersionError):
        write.apply(saved, at=LATER)
    replay = replace(write, expected_version=saved.version)
    assert replay.apply(saved, at=LATER + timedelta(hours=1)) is saved
    stopping = replace(saved, execution_state=ExecutionState.STOPPING)
    assert replay.apply(stopping, at=LATER) is stopping
    with pytest.raises(ExecutionObservationConflictError):
        replace(replay, staging_result=result_for(current, version="other-version")).apply(
            saved, at=LATER
        )


@pytest.mark.parametrize(
    "state",
    [ExecutionState.SUBMITTING, ExecutionState.STOPPING, ExecutionState.UNKNOWN_QUARANTINED],
)
def test_first_registration_is_rejected_after_cancel_or_unknown(state: ExecutionState) -> None:
    current = executing()
    observed = command(current)
    current.execution_state = state
    with pytest.raises(ExecutionCleanupPendingError):
        observed.apply(current, at=LATER)


def test_expired_legacy_and_stale_registrations_do_not_fill_missing_facts() -> None:
    current = executing()
    write = command(current)
    with pytest.raises(ExecutionCleanupPendingError):
        write.apply(current, at=current.identity.deadline_at)
    with pytest.raises(ExecutionCleanupPendingError):
        write.apply(replace(current, has_staging_output=True), at=LATER)
    with pytest.raises(StaleSubmissionVersionError):
        replace(write, expected_version=1).apply(current, at=LATER)
    with pytest.raises(InvalidExecutionRequestError):
        replace(
            write, staging_result=replace(result_for(current), request_digest="sha256:" + "b" * 64)
        ).apply(current, at=LATER)


def registered_ledger() -> tuple[InMemorySubmissionLedger, SubmissionIntent]:
    ledger = InMemorySubmissionLedger(now=lambda: LATER)
    current = pending(ledger)
    current.mark_dispatched("engine-request-1", at=NOW)
    saved = ledger.record_execution_observation(
        RecordExecutionObservation.from_intent(current, expected_version=1)
    )
    return ledger, saved


def test_trusted_boundary_registers_complete_receipt_and_replay_reads_original() -> None:
    ledger, current = registered_ledger()
    result = result_for(current)
    payload: dict[str, Any] = {
        "observation": ExecutionSnapshot(current).status()
        | {"staging_artifact_ref": result.value()["reference"]},
        "expected_version": str(current.version),
        "staging_result": result.value(),
    }
    first = boundary_for(ledger).record_execution_observation(payload)
    replay = boundary_for(ledger).record_execution_observation(
        payload | {"expected_version": first["version"]}
    )
    assert first["outcome"] == "committed" and replay["outcome"] == "replayed"
    assert first["version"] == replay["version"]
    saved = ledger.get(current.identity.submit_intent_id)
    assert saved is not None
    assert saved.staging_result == result and saved.execution_state.holds_capacity
    incomplete = payload | {"observation": payload["observation"] | {"staging_artifact_ref": None}}
    assert boundary_for(ledger).record_execution_observation(incomplete)["outcome"] == "rejected"


def test_existing_result_projection_can_be_echoed_but_never_registers_or_rewrites() -> None:
    ledger, current = registered_ledger()
    saved = ledger.record_execution_observation(command(current))
    payload = {
        "observation": ExecutionSnapshot(saved).status() | {"execution_state": "stopping"},
        "expected_version": str(saved.version),
    }
    outcome = boundary_for(ledger).record_execution_observation(payload)
    assert outcome["outcome"] == "committed"
    stopping = ledger.get(current.identity.submit_intent_id)
    assert stopping is not None and stopping.execution_state is ExecutionState.STOPPING
    assert stopping.staging_result == saved.staging_result


async def test_private_http_reads_pending_then_fixed_receipt_without_running_work(
    tmp_path: Path,
) -> None:
    ledger, current = registered_ledger()
    app, _ = app_for(tmp_path, ledger=ledger)
    path = f"/internal/v1/executions/{current.identity.submit_intent_id}/staging-result"
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.get(path)).status_code == 401
        pending_result = await client.get(path, headers=HEADERS)
        assert pending_result.status_code == 200 and pending_result.json()["staging_result"] is None
        saved = ledger.record_execution_observation(command(current))
        assert saved.staging_result is not None
        before = ledger.get(current.identity.submit_intent_id)
        response = await client.get(path, headers=HEADERS)
        assert response.status_code == 200
        assert response.json()["staging_result"] == saved.staging_result.value()
        assert ledger.get(current.identity.submit_intent_id) == before
        assert (
            await client.get(
                path.replace(current.identity.submit_intent_id, "missing-id"), headers=HEADERS
            )
        ).status_code == 404
        parse("ExecutionStagingResultResponse", response.json())
