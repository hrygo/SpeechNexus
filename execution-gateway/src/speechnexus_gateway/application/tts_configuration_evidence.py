"""Projection of the frozen TTS values installed in the file worker."""

from dataclasses import dataclass
from datetime import UTC, datetime

from speechnexus_gateway.domain.tts import BuiltinVoiceBinding, TtsExecutionTarget, TtsPolicy


@dataclass(frozen=True, slots=True)
class TtsConfigurationEvidence:
    policy: TtsPolicy
    voice: BuiltinVoiceBinding
    target: TtsExecutionTarget

    def observe(self) -> dict[str, str | int]:
        return {
            "gateway_policy_digest": self.policy.digest,
            "policy_revision_id": self.policy.config_revision,
            "voice_id": self.voice.voice_id,
            "voice_revision": self.voice.voice_revision,
            "voice_revision_id": self.voice.voice_revision_id,
            "native_speaker_id": self.voice.native_speaker_id,
            "binding_revision_id": self.target.binding_revision_id,
            "node_id": self.target.host_node_id,
            "gateway_id": self.target.gateway_id,
            "instance_id": self.target.instance_id,
            "instance_generation": self.target.instance_generation,
            "observed_at": datetime.now(UTC).isoformat(),
        }
