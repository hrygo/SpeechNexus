"""The native VAD port preserves existing public ranges, clocks and closure reasons."""

from dataclasses import replace

import pytest

from speechnexus_gateway.application.vad import CpuVadSession
from speechnexus_gateway.domain.vad import VadCloseReason, VadPolicy, VadSegment
from tests.contract.test_p0_public_contracts import (
    _REALTIME_SCHEMA,
    _load_json,
    _validator,
)
from tests.fake.vad import ScriptedVadModel


@pytest.mark.parametrize("reason", list(VadCloseReason))
def test_native_closure_maps_to_the_frozen_public_event(reason: VadCloseReason) -> None:
    document = _load_json(_REALTIME_SCHEMA)
    policy = VadPolicy("contract-1", max_segment_ms=1000)
    session = CpuVadSession(model=ScriptedVadModel(fallback=0.9), policy=policy)
    segments: list[VadSegment] = []
    for _ in range(11):
        segments.extend(session.push(b"\x01\x00" * 1600, sample_start=session.accepted_samples))
    segment = replace(segments[0], reason=reason)
    start, end = segment.wire_sample_span
    schema = {**document, "$ref": "#/$defs/NexusSegmentClosed"}
    _validator(schema).validate(
        {
            "type": "nexus.transcription.segment_closed",
            "event_id": "event-vad-1",
            "session_id": "session-vad-1",
            "operation_id": "operation-vad-1",
            "segment_id": "segment-vad-1",
            "item_id": "item-vad-1",
            "seq": segment.sequence,
            "stream_epoch": 1,
            "sent_at": "2026-10-24T01:00:00Z",
            "sample_span": {"start": start, "end": end},
            "reason": segment.reason.value,
        }
    )
    assert end > start and end - start <= policy.max_segment_ms * 24
    session.close()


@pytest.mark.parametrize(
    "threshold,prefix,silence,maximum",
    [(0.0, 0, 200, 1000), (1.0, 3000, 3000, 30000)],
)
def test_policy_extremes_remain_within_the_public_parameter_domains(
    threshold: float, prefix: int, silence: int, maximum: int
) -> None:
    policy = VadPolicy(
        "contract-1",
        threshold=threshold,
        end_threshold=threshold,
        prefix_padding_ms=prefix,
        silence_duration_ms=silence,
        max_segment_ms=maximum,
    )
    document = _load_json(_REALTIME_SCHEMA)
    _validator({"$defs": document["$defs"], "$ref": "#/$defs/ServerVad"}).validate(
        {
            "type": "server_vad",
            "threshold": policy.threshold,
            "prefix_padding_ms": policy.prefix_padding_ms,
            "silence_duration_ms": policy.silence_duration_ms,
        }
    )
    _validator(
        {
            "$defs": document["$defs"],
            "$ref": "#/$defs/AsrEffectiveParameters/properties/max_segment_ms",
        }
    ).validate(policy.max_segment_ms)
