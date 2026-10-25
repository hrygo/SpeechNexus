"""Audio receipts preserve JSON compatibility, original metering and stop authority."""

import hashlib
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from speechnexus_contracts.execution import parse
from speechnexus_contracts.validation import ContractValidationError

from speechnexus_gateway.domain.execution_inputs import ExecutionObjectReference
from speechnexus_gateway.domain.execution_results import (
    ExecutionAudioMetadata,
    ExecutionAudioStagingResult,
    staging_result_from_document,
)
from speechnexus_gateway.domain.observation import ExecutionObservation, RecordExecutionObservation
from speechnexus_gateway.domain.output_intervals import (
    AppendInterval,
    MeterKind,
    OutputIntervalRecord,
)
from speechnexus_gateway.domain.recovery import ExecutionSnapshot
from speechnexus_gateway.domain.submission import (
    ExecutionCleanupPendingError,
    ExecutionErrorCode,
    ExecutionState,
    InvalidExecutionRequestError,
    SubmissionIntent,
    SubmitRequest,
)
from tests.fake.execution import InMemorySubmissionLedger
from tests.fake.execution_command import command_example
from tests.unit.gateway.test_barrier_http import HEADERS, app_for
from tests.unit.gateway.test_execution_observation import boundary_for
from tests.unit.gateway.test_execution_results import LATER, executing, result_for
from tests.unit.gateway.test_submit_execution import NOW, identity_for

PCM = b"\x01\x00" * 2400
PCM_DIGEST = "sha256:" + hashlib.sha256(PCM).hexdigest()


def audio_receipt(current: SubmissionIntent) -> ExecutionAudioStagingResult:
    return ExecutionAudioStagingResult(
        current.request_digest,
        ExecutionObjectReference(
            "audio-result",
            current.identity.workspace_id,
            "synthetic-private",
            "private/execution-results/synthetic/speech.pcm",
            PCM_DIGEST,
            len(PCM),
        ),
        "synthetic-version",
        "audio/pcm",
        ExecutionAudioMetadata(24000, 1, len(PCM) // 2, 100, PCM_DIGEST),
    )


def audio_ledger(*, gap: bool = False) -> tuple[InMemorySubmissionLedger, SubmissionIntent]:
    command = command_example(0)
    request = SubmitRequest(
        identity_for(
            command, pool_id="tts-realtime", sample_rate_hz=24000, max_output_audio_ms=2000
        ),
        command,
        output_stream_id="tts-main",
    )
    initial = SubmissionIntent(
        request.identity, NOW, NOW, request.request_digest, output_stream_id="tts-main"
    )
    ledger = InMemorySubmissionLedger(now=lambda: LATER)
    ledger.add(initial, request_document=request.request_document)
    initial.mark_dispatched("synthetic-tts", at=NOW)
    current = ledger.record_execution_observation(
        RecordExecutionObservation.from_intent(initial, expected_version=1)
    )
    ledger.append_interval(
        OutputIntervalRecord(
            AppendInterval(
                "generated-a",
                current.identity.submit_intent_id,
                "tts-main",
                24000,
                MeterKind.GENERATED,
                100 if gap else 0,
                len(PCM) // 2,
            ),
            current.identity,
        )
    )
    saved = ledger.get(current.identity.submit_intent_id)
    assert saved is not None
    return ledger, saved


def registration(current: SubmissionIntent) -> RecordExecutionObservation:
    return RecordExecutionObservation(
        ExecutionObservation.from_intent(current), current.version, audio_receipt(current)
    )


def test_audio_roundtrip_and_old_json_format_remain_distinct() -> None:
    _, current = audio_ledger()
    result = audio_receipt(current)
    assert staging_result_from_document(result.document) == result
    json_result = result_for(executing())
    assert staging_result_from_document(json_result.document) == json_result
    parse("ExecutionAudioStagingResult", result.value())
    for bad in [
        result.document + " ",
        result.document.replace("{", '{"extra":1,', 1),
        result.document.replace("{", '{"storage_version":"hidden",', 1),
        "x" * 16385,
        "[" * 1200 + "0" + "]" * 1200,
    ]:
        with pytest.raises(ValueError):
            staging_result_from_document(bad)


@pytest.mark.parametrize(
    "changes",
    [
        {"sample_rate_hz": 8000},
        {"sample_rate_hz": True},
        {"channels": True},
        {"channels": 2},
        {"source_pcm_samples": 23},
        {"source_pcm_samples": 33554433},
        {"source_pcm_samples": 1.5},
        {"duration_ms": 101},
        {"source_pcm_digest": "sha256:invalid"},
    ],
)
def test_source_clock_rejects_out_of_scope_or_inconsistent_metadata(
    changes: dict[str, Any],
) -> None:
    fields: dict[str, Any] = {
        "sample_rate_hz": 24000,
        "channels": 1,
        "source_pcm_samples": 2400,
        "duration_ms": 100,
        "source_pcm_digest": PCM_DIGEST,
    } | changes
    with pytest.raises(ValueError):
        ExecutionAudioMetadata(**fields)


def test_pcm_receipt_requires_exact_size_and_source_digest() -> None:
    _, current = audio_ledger()
    result = audio_receipt(current)
    for reference in [
        replace(result.reference, byte_length=len(PCM) + 2),
        replace(result.reference, content_sha256="sha256:" + "b" * 64),
    ]:
        with pytest.raises(ValueError):
            replace(result, reference=reference)


def test_audio_registration_uses_original_intervals_without_changing_meter_or_stop() -> None:
    ledger, current = audio_ledger()
    write = registration(current)
    saved = ledger.record_execution_observation(write)
    assert saved.version == current.version + 1
    assert saved.staging_result == write.staging_result
    assert saved.identity == current.identity
    assert saved.generated_pcm_samples == current.generated_pcm_samples
    assert saved.execution_state is ExecutionState.EXECUTING
    assert saved.stop_evidence_digest is None and saved.terminated_at is None
    stopping = ledger.record_execution_observation(
        RecordExecutionObservation.from_intent(
            replace(saved, execution_state=ExecutionState.STOPPING), expected_version=saved.version
        )
    )
    replay = ledger.record_execution_observation(replace(write, expected_version=stopping.version))
    assert replay == stopping
    assert ExecutionSnapshot(replay).staging_response()["staging_result"] is None
    assert (
        ExecutionSnapshot(replay).audio_staging_response()["staging_result"]
        == audio_receipt(current).value()
    )


def test_max_end_without_full_generated_coverage_cannot_register_audio() -> None:
    ledger, current = audio_ledger(gap=True)
    assert current.generated_pcm_samples == len(PCM) // 2
    with pytest.raises(InvalidExecutionRequestError, match="coverage"):
        ledger.record_execution_observation(registration(current))
    assert ledger.get(current.identity.submit_intent_id) == current


def test_audio_cannot_replace_asr_or_register_before_matching_output() -> None:
    current = executing()
    with pytest.raises(InvalidExecutionRequestError):
        registration(current).apply(current, at=LATER)
    _, current = audio_ledger()
    for changed in [
        replace(current, generated_pcm_samples=2399),
        replace(current, has_output_intervals=False),
    ]:
        with pytest.raises(InvalidExecutionRequestError):
            registration(changed).apply(changed, at=LATER)
    stopping = replace(current, execution_state=ExecutionState.STOPPING)
    with pytest.raises(ExecutionCleanupPendingError):
        registration(stopping).apply(stopping, at=LATER)


def test_a_registered_audio_result_cannot_gain_more_generated_samples() -> None:
    ledger, current = audio_ledger()
    saved = ledger.record_execution_observation(registration(current))
    assert saved.staging_result is not None
    extra = OutputIntervalRecord(
        AppendInterval(
            "generated-b",
            saved.identity.submit_intent_id,
            "tts-main",
            24000,
            MeterKind.GENERATED,
            len(PCM) // 2,
            len(PCM) // 2 + 100,
        ),
        saved.identity,
    )
    with pytest.raises(ExecutionCleanupPendingError):
        ledger.append_interval(extra)
    assert ledger.get(saved.identity.submit_intent_id) == saved


async def test_audio_http_and_trusted_boundary_consume_one_receipt_with_legacy_json_view(
    tmp_path: Path,
) -> None:
    ledger, current = audio_ledger()
    result = audio_receipt(current)
    app, _ = app_for(tmp_path, ledger=ledger)
    base = f"/internal/v1/executions/{current.identity.submit_intent_id}"
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.get(base + "/audio-staging-result")).status_code == 401
        pending = await client.get(base + "/audio-staging-result", headers=HEADERS)
        assert pending.json()["staging_result"] is None
        payload = {
            "observation": ExecutionSnapshot(current).status()
            | {"staging_artifact_ref": result.value()["reference"]},
            "expected_version": str(current.version),
            "audio_staging_result": result.value(),
        }
        assert boundary_for(ledger).record_execution_observation(payload)["outcome"] == "committed"
        before = ledger.get(current.identity.submit_intent_id)
        response = await client.get(base + "/audio-staging-result", headers=HEADERS)
        assert response.status_code == 200
        assert response.json()["staging_result"] == result.value()
        parse("ExecutionAudioStagingResultResponse", response.json())
        old = await client.get(base + "/staging-result", headers=HEADERS)
        parse("ExecutionStagingResultResponse", old.json())
        assert old.json()["staging_result"] is None
        assert ledger.get(current.identity.submit_intent_id) == before
        assert (
            await client.get(
                "/internal/v1/executions/missing-id/audio-staging-result", headers=HEADERS
            )
        ).status_code == 404


def test_audio_receipt_travels_only_in_its_own_optional_property() -> None:
    """The legacy JSON property keeps its shape; audio is additive and unambiguous."""
    ledger, current = audio_ledger()
    audio = audio_receipt(current)
    json_receipt = result_for(current)
    boundary = boundary_for(ledger)
    observation = ExecutionSnapshot(current).status() | {
        "staging_artifact_ref": audio.value()["reference"]
    }
    expected_version = str(current.version)
    # An audio payload is invalid inside the unchanged closed JSON property.
    with pytest.raises(ContractValidationError):
        boundary.record_execution_observation(
            {
                "observation": observation,
                "expected_version": expected_version,
                "staging_result": audio.value(),
            }
        )
    # A JSON payload is invalid inside the audio property.
    with pytest.raises(ContractValidationError):
        boundary.record_execution_observation(
            {
                "observation": observation,
                "expected_version": expected_version,
                "audio_staging_result": json_receipt.value(),
            }
        )
    # Two registrations in one observation are refused rather than ordered.
    both = boundary.record_execution_observation(
        {
            "observation": observation,
            "expected_version": expected_version,
            "staging_result": json_receipt.value(),
            "audio_staging_result": audio.value(),
        }
    )
    assert both["outcome"] == "rejected"
    assert both["error_code"] == ExecutionErrorCode.INVALID_EXECUTION_REQUEST.value
    assert ledger.get(current.identity.submit_intent_id) == current
