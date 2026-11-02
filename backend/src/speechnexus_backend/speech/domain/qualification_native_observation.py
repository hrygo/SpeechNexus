"""Compare an authoritative READY observation with independently fixed pins."""

from __future__ import annotations

from dataclasses import dataclass

from speechnexus_backend.speech.domain.runtime_intents import RuntimeBinding, digest
from speechnexus_backend.speech.domain.runtime_observations import RuntimeObservation


@dataclass(frozen=True, slots=True)
class ExpectedNativeRuntime:
    binding: RuntimeBinding
    instance_id: str
    instance_generation: int
    fencing_token: int
    image_digest: str
    template_digest: str
    controller_artifact_digest: str

    def __post_init__(self) -> None:
        for value in (self.image_digest, self.template_digest, self.controller_artifact_digest):
            digest(value)


def require_native_ready(
    observation: RuntimeObservation,
    expected: ExpectedNativeRuntime,
    *,
    now_us: int,
    max_age_us: int,
) -> None:
    """Reject missing, stale, future, mismatched, or non-native controller evidence.

    The caller must read the observation from the current authoritative runtime
    head and verify the original intent and readiness in the same transaction.
    The expected digests must come from the operator's trusted deployment pins.
    """
    native = observation.native_runtime_evidence
    if (
        native is None
        or observation.action != "prepare"
        or observation.intent_state != "succeeded"
        or observation.observed_instance_state != "ready"
        or observation.binding != expected.binding
        or observation.instance_id != expected.instance_id
        or observation.instance_generation != expected.instance_generation
        or observation.fencing_token != expected.fencing_token
        or native.image_digest != expected.image_digest
        or native.template_digest != expected.template_digest
        or native.controller_artifact_digest != expected.controller_artifact_digest
        or native.native_auth_kind != "vllm_api_key_v1"
        or type(now_us) is not int
        or type(max_age_us) is not int
        or max_age_us <= 0
        or native.observed_at_us > now_us
        or (native.observed_at_us == now_us and native.observed_submicrosecond != "")
        or now_us - native.observed_at_us > max_age_us
    ):
        raise ValueError("native_runtime_evidence_unavailable")
