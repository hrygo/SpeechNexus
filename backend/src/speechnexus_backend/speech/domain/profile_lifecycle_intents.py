"""Private durable intent receipts; none of these outcomes mean publication."""

from dataclasses import dataclass, field
from typing import Literal

from speechnexus_backend.speech.domain.profile_lifecycle import ProfileLifecycleCommand


@dataclass(frozen=True, slots=True)
class ProfileLifecycleIntent:
    command: ProfileLifecycleCommand = field(repr=False)
    recorded_at_us: int

    def __post_init__(self) -> None:
        if not isinstance(self.command, ProfileLifecycleCommand):
            raise ValueError("Invalid profile lifecycle intent")
        if type(self.recorded_at_us) is not int or self.recorded_at_us < 0:
            raise ValueError("Invalid profile intent time")


@dataclass(frozen=True, slots=True)
class ProfileLifecycleIntentResult:
    outcome: Literal["recorded", "replayed", "conflict", "unknown"]
    intent: ProfileLifecycleIntent | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if (
            self.outcome not in ("recorded", "replayed", "conflict", "unknown")
            or (self.intent is not None) != (self.outcome in ("recorded", "replayed"))
            or (self.intent is not None and not isinstance(self.intent, ProfileLifecycleIntent))
        ):
            raise ValueError("Invalid profile intent receipt")
