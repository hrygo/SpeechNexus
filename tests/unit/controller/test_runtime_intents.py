"""Domain rules for runtime intents: reservation shape, evidence and head derivation."""

from __future__ import annotations

from datetime import datetime

import pytest
from tests.fake.controller import (
    FIXED_NOW,
    GIB,
    build_request,
    build_reservation,
)

from speechnexus_node_controller.domain.intents import (
    InstanceHead,
    InvalidRuntimeRequestError,
    MemoryArchitecture,
    MemoryHold,
    ObservedInstanceState,
    RuntimeActionKind,
    RuntimeInstanceIntent,
    RuntimeIntentErrorCode,
    RuntimeIntentState,
    RuntimeMemoryVector,
    RuntimeStopEvidence,
)


def test_peak_below_steady_is_rejected() -> None:
    with pytest.raises(InvalidRuntimeRequestError, match="peak unified_memory_bytes"):
        build_reservation(
            peak=RuntimeMemoryVector(unified_memory_bytes=2 * GIB),
            steady=RuntimeMemoryVector(unified_memory_bytes=4 * GIB),
        )


def test_unified_reservation_must_not_carry_device_memory() -> None:
    with pytest.raises(InvalidRuntimeRequestError, match="must not carry device"):
        build_reservation(
            peak=RuntimeMemoryVector(unified_memory_bytes=10 * GIB, device_memory_bytes=1 * GIB)
        )


def test_discrete_reservation_must_not_carry_unified_memory() -> None:
    with pytest.raises(InvalidRuntimeRequestError, match="must not carry unified"):
        build_reservation(
            architecture=MemoryArchitecture.DISCRETE_DEVICE_MEMORY,
            peak=RuntimeMemoryVector(unified_memory_bytes=1 * GIB, device_memory_bytes=8 * GIB),
            steady=RuntimeMemoryVector(device_memory_bytes=4 * GIB),
        )


def test_reservation_must_reserve_memory_in_the_acting_counters() -> None:
    with pytest.raises(InvalidRuntimeRequestError, match="must reserve memory"):
        build_reservation(peak=RuntimeMemoryVector(), steady=RuntimeMemoryVector())


def test_stop_evidence_requires_both_confirmations() -> None:
    with pytest.raises(InvalidRuntimeRequestError, match="released resources"):
        RuntimeStopEvidence(verified_at=FIXED_NOW, resources_released_confirmed=False)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("intent_id", "Intent 1", "intent_id"),
        ("pool_id", "asr-hyperscale", "unknown pool_id"),
        ("config_digest", "sha256:" + "A" * 64, "config_digest"),
        ("instance_generation", 0, "instance_generation"),
        ("fencing_token", 0, "fencing_token"),
    ],
)
def test_request_validation_rejects_out_of_grammar_values(
    field: str, value: object, message: str
) -> None:
    with pytest.raises(InvalidRuntimeRequestError, match=message):
        build_request(**{field: value})


def test_request_requires_a_timezone_aware_deadline() -> None:
    with pytest.raises(InvalidRuntimeRequestError, match="deadline_at"):
        build_request(deadline_at=datetime(2026, 10, 24, 10, 0))


def test_failed_intent_requires_a_stable_error_code() -> None:
    with pytest.raises(ValueError, match="stable error code"):
        RuntimeInstanceIntent(
            request=build_request(),
            created_at=FIXED_NOW,
            updated_at=FIXED_NOW,
            intent_state=RuntimeIntentState.FAILED,
        )


def test_succeeded_prepare_intent_only_observes_ready() -> None:
    with pytest.raises(ValueError, match="observes ready"):
        RuntimeInstanceIntent(
            request=build_request(),
            created_at=FIXED_NOW,
            updated_at=FIXED_NOW,
            intent_state=RuntimeIntentState.SUCCEEDED,
            observed_instance_state=ObservedInstanceState.LOADING,
            memory_hold=MemoryHold.STEADY,
        )


def test_stop_intent_never_reports_a_ready_memory_hold() -> None:
    intent = RuntimeInstanceIntent(
        request=build_request(action=RuntimeActionKind.STOP, intent_id="intent-stop-1"),
        created_at=FIXED_NOW,
        updated_at=FIXED_NOW,
    )
    assert intent.memory_hold is MemoryHold.NONE


def test_head_tracks_generation_watermark_fencing_and_drain_state() -> None:
    prepared = RuntimeInstanceIntent(
        request=build_request(),
        created_at=FIXED_NOW,
        updated_at=FIXED_NOW,
        intent_state=RuntimeIntentState.SUCCEEDED,
        observed_instance_state=ObservedInstanceState.READY,
        memory_hold=MemoryHold.STEADY,
    )
    head = InstanceHead.derive("instance-asr-1", [prepared])
    assert head.generation_high_water == 1
    assert head.current_generation == 1
    assert head.last_fencing_token == 1
    assert not head.is_drained

    stopping = RuntimeInstanceIntent(
        request=build_request(
            intent_id="intent-stop-1", action=RuntimeActionKind.STOP, fencing_token=2
        ),
        created_at=FIXED_NOW,
        updated_at=FIXED_NOW,
    )
    head = InstanceHead.derive("instance-asr-1", [prepared, stopping])
    assert head.unresolved_stop_intent_id == "intent-stop-1"
    assert head.last_fencing_token == 2
    assert not head.is_drained

    stopping.intent_state = RuntimeIntentState.SUCCEEDED
    stopping.observed_instance_state = ObservedInstanceState.STOPPED
    stopping.stop_evidence = RuntimeStopEvidence(verified_at=FIXED_NOW)
    head = InstanceHead.derive("instance-asr-1", [prepared, stopping])
    assert head.stopped_generations == frozenset({1})
    assert head.is_drained


def test_head_of_an_unknown_instance_is_drained() -> None:
    head = InstanceHead.derive("instance-unknown", [])
    assert head.generation_high_water is None
    assert head.last_fencing_token == 0
    assert head.is_drained


def test_unresolved_prepare_keeps_the_generation_undrained() -> None:
    intent = RuntimeInstanceIntent(
        request=build_request(),
        created_at=FIXED_NOW,
        updated_at=FIXED_NOW,
        intent_state=RuntimeIntentState.RECONCILING,
        observed_instance_state=ObservedInstanceState.LOADING,
        error_code=RuntimeIntentErrorCode.RUNTIME_UNAVAILABLE,
    )
    head = InstanceHead.derive("instance-asr-1", [intent])
    assert head.unresolved_prepare_intent_id == "intent-1"
    assert not head.is_drained
