"""Write port for hiding a published voice revision without rewriting it."""

from typing import Protocol

from speechnexus_backend.speech.domain.voice_lifecycle import (
    VoiceRetirementCommand,
    VoiceRetirementReceipt,
)


class VoiceRetirements(Protocol):
    def retire(self, command: VoiceRetirementCommand) -> VoiceRetirementReceipt: ...
