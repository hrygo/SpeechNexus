"""Only a terminated engine process or container may release VoiceDesign capacity.

The verified vLLM-Omni image answers ``POST /v1/omni/sleep`` with ``SUCCESS`` and
``freed_bytes=0``, and the next inference then times out after 300 s. Recorded
here as a counterexample: an engine sleep acknowledgment is never release.
"""

from datetime import UTC, datetime, timedelta

import pytest

from speechnexus_gateway.domain.recovery import (
    InstanceTerminationEvidence,
    whole_instance_stop_evidence,
)
from speechnexus_gateway.domain.submission import (
    CleanupState,
    ExecutionState,
    StopEvidenceKind,
    SubmissionIdentity,
    SubmissionIntent,
)
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


# --- the production receipt and the one evidence row ---------------------------------


def termination(**changes: object) -> InstanceTerminationEvidence:
    fields: dict[str, object] = {
        "instance_id": "instance-voice-1",
        "instance_generation": 2,
        "evidence_id": "stop-node-voice-1",
        "evidence_kind": StopEvidenceKind.INSTANCE_TERMINATED_BY_CONTROLLER,
        "evidence_digest": "sha256:" + "c" * 64,
        "observed_at": NOW,
        "process_exited": True,
        "resources_released": True,
    }
    fields.update(changes)
    return InstanceTerminationEvidence(**fields)  # type: ignore[arg-type]


def test_a_checkpoint_backed_termination_becomes_a_receipt() -> None:
    receipt = VoiceDesignStopReceipt.from_instance_termination(
        submit_intent_id="intent-voice-design", evidence=termination()
    )

    assert receipt.release.kind is ReleaseEvidenceKind.CONTAINER_STOP
    assert receipt.release.exited is True and receipt.release.release_confirmed is True
    assert receipt.release.observed_at == NOW
    assert receipt.termination is not None


@pytest.mark.parametrize(
    "changes",
    [
        {"process_exited": False},
        {"resources_released": False},
        {"evidence_kind": StopEvidenceKind.ENGINE_ABORT_ACK},
        {"evidence_kind": StopEvidenceKind.ENGINE_TERMINAL_RESPONSE},
        {"evidence_kind": StopEvidenceKind.UNCONFIRMED_TIMEOUT},
    ],
)
def test_a_partial_or_sleep_shaped_termination_cannot_build_a_receipt(
    changes: dict[str, object],
) -> None:
    with pytest.raises(ReleaseEvidenceError) as raised:
        VoiceDesignStopReceipt.from_instance_termination(
            submit_intent_id="intent-voice-design", evidence=termination(**changes)
        )
    assert raised.value.code == "termination_unconfirmed"


def test_the_ledger_cannot_even_express_a_sleep_acknowledgment() -> None:
    """The stop ledger's own vocabulary has no sleep member to smuggle in."""
    assert "engine_sleep_ack" not in {kind.value for kind in StopEvidenceKind}
    assert ReleaseEvidenceKind.ENGINE_SLEEP_ACK.value == "engine_sleep_ack"


def test_a_missing_termination_cannot_build_a_receipt() -> None:
    with pytest.raises(ReleaseEvidenceError):
        VoiceDesignStopReceipt.from_instance_termination(
            submit_intent_id="intent-voice-design", evidence=None
        )


def test_the_receipt_hands_the_shared_converter_the_fact_it_validated() -> None:
    """No second evidence identity, digest or column for the voice-design lane."""
    evidence = termination()
    receipt = VoiceDesignStopReceipt.from_instance_termination(
        submit_intent_id="intent-voice-design", evidence=evidence
    )
    intent = voice_intent()

    assert receipt.verified_stop(intent) == whole_instance_stop_evidence(intent, evidence)
    assert receipt.verified_stop(intent).evidence_kind is (  # type: ignore[union-attr]
        StopEvidenceKind.INSTANCE_TERMINATED_BY_CONTROLLER
    )


def test_a_receipt_holding_only_release_evidence_yields_no_execution_row() -> None:
    receipt = VoiceDesignStopReceipt(
        "intent-voice-design",
        VoiceDesignReleaseEvidence(
            kind=ReleaseEvidenceKind.CONTAINER_STOP,
            observed_at=NOW,
            exited=True,
            release_confirmed=True,
        ),
    )

    assert receipt.verified_stop(voice_intent()) is None


def test_the_receipt_refuses_another_generations_termination() -> None:
    receipt = VoiceDesignStopReceipt.from_instance_termination(
        submit_intent_id="intent-voice-design", evidence=termination(instance_generation=3)
    )

    assert receipt.verified_stop(voice_intent()) is None


def voice_intent() -> SubmissionIntent:
    identity = SubmissionIdentity(
        submit_intent_id="intent-voice-design",
        lease_id="lease-voice-1",
        operation_id="op-voice-1",
        attempt_id="attempt-voice-1",
        stage_id="stage-design",
        workspace_id="workspace-1",
        pool_id="voice-creation",
        host_node_id="node-1",
        gateway_id="gateway-1",
        instance_id="instance-voice-1",
        instance_generation=2,
        binding_revision_id="binding-rev-1",
        policy_revision_id="policy-rev-1",
        voice_revision_id=None,
        fencing_token=1,
        command_digest="sha256:" + "d" * 64,
        deadline_at=NOW + timedelta(hours=1),
        sample_rate_hz=None,
        max_output_audio_ms=None,
    )
    return SubmissionIntent(
        identity=identity,
        recorded_at=NOW - timedelta(minutes=1),
        updated_at=NOW - timedelta(minutes=1),
        request_digest="sha256:" + "e" * 64,
        execution_state=ExecutionState.UNKNOWN_QUARANTINED,
        cleanup_state=CleanupState.QUARANTINED,
        dispatched_at=NOW - timedelta(minutes=1),
    )
