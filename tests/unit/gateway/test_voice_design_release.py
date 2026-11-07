"""Only a terminated engine process or container may release VoiceDesign capacity.

The verified vLLM-Omni image answers ``POST /v1/omni/sleep`` with ``SUCCESS`` and
``freed_bytes=0``, and the next inference then times out after 300 s. Recorded
here as a counterexample: an engine sleep acknowledgment is never release.
"""

from datetime import UTC, datetime

import pytest

from speechnexus_gateway.domain.voice_design_release import (
    ReleaseEvidenceError,
    ReleaseEvidenceKind,
    VoiceDesignReleaseEvidence,
    VoiceDesignStopReceipt,
)

NOW = datetime(2026, 11, 7, 12, 0, tzinfo=UTC)


def test_an_engine_sleep_acknowledgment_is_not_release_evidence() -> None:
    evidence = VoiceDesignReleaseEvidence.engine_sleep_ack(
        observed_at=NOW, status="SUCCESS", freed_bytes=0
    )

    assert evidence.kind is ReleaseEvidenceKind.ENGINE_SLEEP_ACK
    assert evidence.is_release_evidence is False
    with pytest.raises(ReleaseEvidenceError) as raised:
        evidence.require_release()
    assert raised.value.code == "engine_sleep_is_not_release"


def test_a_zero_byte_sleep_ack_cannot_build_a_stop_receipt() -> None:
    evidence = VoiceDesignReleaseEvidence.engine_sleep_ack(
        observed_at=NOW, status="SUCCESS", freed_bytes=0
    )
    with pytest.raises(ReleaseEvidenceError):
        VoiceDesignStopReceipt("intent-voice-design", evidence)


def test_an_unconfirmed_exit_does_not_release() -> None:
    evidence = VoiceDesignReleaseEvidence(
        kind=ReleaseEvidenceKind.PROCESS_EXIT, observed_at=NOW, exited=True
    )
    assert evidence.is_release_evidence is False
    with pytest.raises(ReleaseEvidenceError) as raised:
        evidence.require_release()
    assert raised.value.code == "release_unconfirmed"


def test_an_unexited_container_does_not_release() -> None:
    evidence = VoiceDesignReleaseEvidence(
        kind=ReleaseEvidenceKind.CONTAINER_STOP,
        observed_at=NOW,
        exited=False,
        release_confirmed=True,
    )
    assert evidence.is_release_evidence is False


def test_a_verified_process_or_container_exit_is_release_evidence() -> None:
    process = VoiceDesignReleaseEvidence(
        kind=ReleaseEvidenceKind.PROCESS_EXIT,
        observed_at=NOW,
        exited=True,
        release_confirmed=True,
    )
    container = VoiceDesignReleaseEvidence(
        kind=ReleaseEvidenceKind.CONTAINER_STOP,
        observed_at=NOW,
        exited=True,
        release_confirmed=True,
    )

    assert process.is_release_evidence is True
    assert container.is_release_evidence is True
    process.require_release()
    container.require_release()
    assert VoiceDesignStopReceipt("intent-voice-design", process).release is process


def test_freed_bytes_belong_only_to_the_sleep_acknowledgment() -> None:
    with pytest.raises(ValueError):
        VoiceDesignReleaseEvidence(
            kind=ReleaseEvidenceKind.CONTAINER_STOP,
            observed_at=NOW,
            exited=True,
            release_confirmed=True,
            freed_bytes=1024,
        )
