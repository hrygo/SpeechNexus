"""Logical-flow intervals, canonical recovery and metering never infer execution or cleanup."""

from dataclasses import replace
from datetime import timedelta
from typing import Any

import pytest
from speechnexus_contracts import ContractValidationError
from speechnexus_contracts.validation import validate_coordination

from speechnexus_gateway.application.output_intervals import OutputIntervalService
from speechnexus_gateway.domain.observation import RecordExecutionObservation
from speechnexus_gateway.domain.output_intervals import (
    AppendInterval,
    MeterKind,
    OutputIntervalConflictError,
    OutputIntervalRecord,
    OutputOverlapError,
)
from speechnexus_gateway.domain.submission import (
    ExecutionCleanupPendingError,
    ExecutionErrorCode,
    ExecutionState,
    InvalidExecutionRequestError,
    RecordStop,
    SubmissionIntent,
    SubmissionOutcomeUnknownError,
    SubmissionStoreError,
    SubmitRequest,
)
from speechnexus_gateway.interfaces.output_interval_transactions import OutputIntervalBoundary
from tests.fake.execution import InMemorySubmissionLedger, fake_ingress
from tests.unit.gateway.test_submit_execution import stop_evidence
from tests.unit.gateway.test_submit_ledger_sql import COMMAND, IDENTITY, NOW

LATER = NOW + timedelta(seconds=5)


def pending(ledger: InMemorySubmissionLedger, **changes: Any) -> SubmissionIntent:
    identity = replace(IDENTITY, **changes)
    request = SubmitRequest(
        identity,
        COMMAND,
        output_stream_id="tts-main" if identity.sample_rate_hz is not None else None,
    )
    intent = SubmissionIntent(
        identity, NOW, NOW, request.request_digest, output_stream_id=request.output_stream_id
    )
    ledger.add(intent, request_document=request.request_document)
    return intent


def interval(start: int = 0, end: int = 100, **changes: Any) -> AppendInterval:
    return replace(
        AppendInterval(
            "interval-1", "intent-1", "tts-main", 24000, MeterKind.GENERATED, start, end
        ),
        **changes,
    )


def boundary_for(ledger: InMemorySubmissionLedger) -> OutputIntervalBoundary:
    return OutputIntervalBoundary(OutputIntervalService(ingress=fake_ingress(), ledger=ledger))


def test_watermark_is_not_a_usage_sum_and_late_gaps_do_not_regress_it() -> None:
    ledger = InMemorySubmissionLedger(now=lambda: NOW)
    original = pending(ledger)
    records = [
        interval(100, 200),
        interval(0, 100, interval_id="interval-2"),
        interval(300, 400, interval_id="interval-3"),
    ]
    for value in records:
        boundary_for(ledger).append_interval(value.document())
    current = ledger.get("intent-1")
    assert current is not None and current.generated_pcm_samples == 400
    assert sum(value.sample_end - value.sample_start for value in records) == 300
    assert current.version == original.version + 3
    assert current.dispatched_at is None and current.engine_request_id is None
    assert current.execution_state is original.execution_state
    before = current
    replay = boundary_for(ledger).append_interval(records[0].document())
    assert replay["outcome"] == "replayed" and "version" not in replay
    assert ledger.get("intent-1") == before and len(ledger._intervals) == 3


@pytest.mark.parametrize("start,end", [(0, 100), (10, 90), (50, 150)])
def test_a_new_id_does_not_allow_full_or_partial_overlap(start: int, end: int) -> None:
    ledger = InMemorySubmissionLedger(now=lambda: NOW)
    original = pending(ledger)
    ledger.append_interval(OutputIntervalRecord(interval(), original.identity))
    with pytest.raises(OutputOverlapError):
        ledger.append_interval(
            OutputIntervalRecord(
                interval(start, end, interval_id="interval-other"), original.identity
            )
        )
    assert len(ledger._intervals) == 1


def test_two_stages_share_one_stream_and_cannot_double_count_the_same_meter() -> None:
    ledger = InMemorySubmissionLedger(now=lambda: NOW)
    first = pending(ledger)
    second = pending(ledger, submit_intent_id="intent-2", lease_id="lease-2", stage_id="stage-2")
    ledger.append_interval(OutputIntervalRecord(interval(), first.identity))
    with pytest.raises(OutputOverlapError):
        ledger.append_interval(
            OutputIntervalRecord(
                interval(50, 150, interval_id="interval-2", submit_intent_id="intent-2"),
                second.identity,
            )
        )
    ledger.append_interval(
        OutputIntervalRecord(
            interval(100, 200, interval_id="interval-2", submit_intent_id="intent-2"),
            second.identity,
        )
    )
    assert len(ledger._intervals) == 2


def test_each_meter_is_independent_and_diagnostics_do_not_rewrite_generated_projection() -> None:
    ledger = InMemorySubmissionLedger(now=lambda: NOW)
    pending(ledger)
    boundary = boundary_for(ledger)
    for kind in MeterKind:
        result = boundary.append_interval(
            interval(interval_id=f"interval-{kind.value}", meter_kind=kind).document()
        )
        assert result["outcome"] == "committed" and "version" not in result
    current = ledger.get("intent-1")
    assert current is not None and current.generated_pcm_samples == 100 and current.version == 2
    assert len(ledger._intervals) == 3


@pytest.mark.parametrize(
    "changes",
    [
        {"sample_start": True},
        {"sample_end": 2**53},
        {"sample_end": 0},
        {"sample_start": 100},
        {"sample_start": -1},
        {"sample_end": 1.5},
        {"sample_rate_hz": True},
        {"sample_rate_hz": 0},
        {"sample_rate_hz": 384001},
        {"interval_id": "BAD ID"},
        {"meter_kind": "invented"},
    ],
)
def test_domain_enforces_strict_safe_sample_values(changes: dict[str, Any]) -> None:
    with pytest.raises(InvalidExecutionRequestError):
        interval(**changes)


def test_all_frozen_identities_and_stream_values_are_rechecked() -> None:
    ledger = InMemorySubmissionLedger(now=lambda: NOW)
    original = pending(ledger)
    for record in (
        OutputIntervalRecord(interval(), replace(original.identity, instance_generation=2)),
        OutputIntervalRecord(interval(), replace(original.identity, max_output_audio_ms=4000)),
        OutputIntervalRecord(interval(output_stream_id="stream-other"), original.identity),
        OutputIntervalRecord(interval(sample_rate_hz=16000), original.identity),
    ):
        with pytest.raises(InvalidExecutionRequestError):
            ledger.append_interval(record)
    assert ledger.get("intent-1") == original and not ledger._intervals


def test_interval_id_replay_requires_the_complete_canonical_request() -> None:
    ledger = InMemorySubmissionLedger(now=lambda: NOW)
    original = pending(ledger)
    ledger.append_interval(OutputIntervalRecord(interval(), original.identity))
    for changes in ({"sample_end": 101}, {"meter_kind": MeterKind.DELIVERED}):
        with pytest.raises(OutputIntervalConflictError):
            ledger.append_interval(OutputIntervalRecord(interval(**changes), original.identity))
    assert len(ledger._intervals) == 1


def test_hard_ceiling_applies_to_every_meter_without_releasing_capacity() -> None:
    ledger = InMemorySubmissionLedger(now=lambda: NOW)
    pending(ledger)
    boundary = boundary_for(ledger)
    accepted = boundary.append_interval(interval(end=48000).document())
    assert accepted["outcome"] == "committed"
    before = ledger.get("intent-1")
    for kind in MeterKind:
        rejected = boundary.append_interval(
            interval(48000, 48001, interval_id=f"overflow-{kind.value}", meter_kind=kind).document()
        )
        assert rejected["error_code"] == "output_limit_exceeded"
        assert rejected["recovery"] == "reconcile"
    assert ledger.get("intent-1") == before and len(ledger._intervals) == 1


def test_no_pcm_stream_cannot_be_given_an_audio_interval() -> None:
    ledger = InMemorySubmissionLedger(now=lambda: NOW)
    original = pending(ledger, sample_rate_hz=None, max_output_audio_ms=None)
    rejected = boundary_for(ledger).append_interval(interval().document())
    assert rejected["error_code"] == "invalid_execution_request"
    assert ledger.get("intent-1") == original and not ledger._intervals


def test_query_recovers_the_complete_original_fact_and_has_no_write_side_effects() -> None:
    ledger = InMemorySubmissionLedger(now=lambda: NOW)
    pending(ledger)
    boundary = boundary_for(ledger)
    written = boundary.append_interval(interval().document())
    before = ledger.get("intent-1")
    request = {
        "target": {"kind": "interval", "interval_id": "interval-1"},
        "request_digest": written["value"]["request_digest"],
    }
    for _ in range(2):
        found = boundary.query_outcome(request)
        assert found["outcome"] == "replayed" and found["value"] == written["value"]
        assert "version" not in found
        validate_coordination("TransactionResult", found)
    assert ledger.get("intent-1") == before
    wrong = boundary.query_outcome(request | {"request_digest": "sha256:" + "a" * 64})
    assert wrong["error_code"] == "idempotency_conflict"
    missing = boundary.query_outcome(
        {"target": {"kind": "interval", "interval_id": "interval-missing"}}
    )
    assert missing["outcome"] == "unknown" and missing["error_code"] == "outcome_unknown"
    with pytest.raises(ContractValidationError):
        boundary.query_outcome({"target": {"kind": "submit", "submit_intent_id": "intent-1"}})


def test_boundary_preserves_wire_numbers_and_rejects_booleans_or_reversed_ranges() -> None:
    ledger = InMemorySubmissionLedger(now=lambda: NOW)
    pending(ledger)
    request: dict[str, Any] = dict(interval().document()) | {
        "sample_start": 0.0,
        "sample_rate_hz": 24000.0,
    }
    result = boundary_for(ledger).append_interval(request)
    assert result["outcome"] == "committed"
    assert type(request["sample_start"]) is float and type(request["sample_rate_hz"]) is float
    stored = ledger.query_output_interval("interval-1")
    assert stored is not None and result["value"]["request_digest"] == stored.request_digest
    with pytest.raises(ContractValidationError):
        boundary_for(ledger).append_interval(request | {"sample_start": False})
    rejected = boundary_for(ledger).append_interval(
        interval().document() | {"interval_id": "interval-reversed", "sample_start": 101}
    )
    assert rejected["error_code"] == "invalid_execution_request"


def test_foreign_gateway_cannot_write_or_query_its_interval_through_this_service() -> None:
    ledger = InMemorySubmissionLedger(now=lambda: NOW)
    foreign = pending(ledger, gateway_id="gateway-other")
    boundary = boundary_for(ledger)
    rejected = boundary.append_interval(interval().document())
    assert rejected["error_code"] == "invalid_execution_request" and not ledger._intervals
    ledger.append_interval(OutputIntervalRecord(interval(), foreign.identity))
    hidden = boundary.query_outcome({"target": {"kind": "interval", "interval_id": "interval-1"}})
    assert hidden["outcome"] == "unknown" and hidden["error_code"] == "outcome_unknown"


def test_unknown_commit_uses_original_interval_recovery_without_writing_twice() -> None:
    class LoseOnceLedger(InMemorySubmissionLedger):
        lost = False

        def append_interval(self, record: OutputIntervalRecord) -> Any:
            result = super().append_interval(record)
            if not self.lost:
                self.lost = True
                raise SubmissionOutcomeUnknownError
            return result

    ledger = LoseOnceLedger(now=lambda: NOW)
    pending(ledger)
    boundary = boundary_for(ledger)
    unknown = boundary.append_interval(interval().document())
    assert unknown["outcome"] == "unknown" and unknown["recovery"] == "query_original"
    recovered = boundary.query_outcome(
        {"target": {"kind": "interval", "interval_id": "interval-1"}}
    )
    assert recovered["outcome"] == "replayed" and len(ledger._intervals) == 1
    current = ledger.get("intent-1")
    assert current is not None and current.version == 2


def test_late_generated_fact_keeps_a_verified_terminal_execution_closed() -> None:
    clock = [NOW]
    ledger = InMemorySubmissionLedger(now=lambda: clock[0])
    intent = pending(ledger)
    intent.mark_dispatched("engine-1", at=NOW)
    ledger.record_execution_observation(
        RecordExecutionObservation.from_intent(intent, expected_version=1)
    )
    closed = ledger.record_stop(RecordStop(stop_evidence(), ExecutionState.ABORTED))
    clock[0] = LATER
    result = ledger.append_interval(OutputIntervalRecord(interval(), closed.identity))
    assert result.intent.execution_state is ExecutionState.ABORTED
    assert result.intent.stop_evidence_digest == closed.stop_evidence_digest
    assert result.intent.terminated_at == closed.terminated_at
    assert result.intent.generated_pcm_samples == 100


@pytest.mark.parametrize("kind", list(MeterKind))
def test_output_intervals_cannot_contradict_a_never_dispatched_failure(kind: MeterKind) -> None:
    ledger = InMemorySubmissionLedger(now=lambda: NOW)
    intent = pending(ledger)
    intent.mark_not_dispatched_failure(ExecutionErrorCode.ENGINE_TRANSPORT_UNKNOWN, at=NOW)
    closed = ledger.record_execution_observation(
        RecordExecutionObservation.from_intent(intent, expected_version=1)
    )
    with pytest.raises(ExecutionCleanupPendingError):
        ledger.append_interval(OutputIntervalRecord(interval(meter_kind=kind), closed.identity))
    assert ledger.get("intent-1") == closed and not ledger._intervals


@pytest.mark.parametrize("kind", [MeterKind.DELIVERED, MeterKind.PLAYED])
def test_diagnostic_facts_alone_prevent_a_false_never_dispatched_failure(kind: MeterKind) -> None:
    ledger = InMemorySubmissionLedger(now=lambda: NOW)
    original = pending(ledger)
    ledger.append_interval(OutputIntervalRecord(interval(meter_kind=kind), original.identity))
    current = ledger.get("intent-1")
    assert (
        current is not None and current.generated_pcm_samples == 0 and current.has_output_intervals
    )
    claimed = replace(original)
    claimed.mark_not_dispatched_failure(ExecutionErrorCode.ENGINE_TRANSPORT_UNKNOWN, at=NOW)
    with pytest.raises(ExecutionCleanupPendingError):
        ledger.record_execution_observation(
            RecordExecutionObservation.from_intent(claimed, expected_version=1)
        )
    assert ledger.get("intent-1") == current


def test_output_projection_cannot_reverse_time_or_overflow_its_version() -> None:
    original_ledger = InMemorySubmissionLedger(now=lambda: NOW)
    original = pending(original_ledger)
    for current, at in (
        (replace(original, updated_at=LATER), NOW),
        (replace(original, version=2**63 - 1), LATER),
    ):
        with pytest.raises(SubmissionStoreError):
            OutputIntervalRecord(interval(), current.identity).apply(
                current, stream_sample_rate_hz=24000, previous=None, overlaps=False, at=at
            )
