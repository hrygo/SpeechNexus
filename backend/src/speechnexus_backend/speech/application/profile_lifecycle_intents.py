"""Intent journal only: no evidence lookup, publication result or pointer mutation."""

from typing import Protocol

from speechnexus_backend.speech.domain.profile_lifecycle import ProfileLifecycleCommand
from speechnexus_backend.speech.domain.profile_lifecycle_intents import (
    ProfileLifecycleIntentResult,
)


class ProfileLifecycleIntents(Protocol):
    def record(self, command: ProfileLifecycleCommand) -> ProfileLifecycleIntentResult:
        """Persist the full intent once; the private key cannot acquire another meaning."""
        ...

    def recover(self, command: ProfileLifecycleCommand) -> ProfileLifecycleIntentResult:
        """Read only the original intent. Absence/unavailability proves no public outcome."""
        ...
