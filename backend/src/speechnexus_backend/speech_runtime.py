"""Closed synchronous TTS composition pending authoritative execution inputs.

There is deliberately no configuration switch: historical recipes cannot grant
voice eligibility or supply a recoverable private submit intent. Query helpers
are reviewable independently; no gateway, storage client or admission is built.
"""

from speechnexus_backend.settings import Settings
from speechnexus_backend.speech.application.speech_synthesis import SpeechSynthesis


def build_speech(settings: Settings) -> SpeechSynthesis | None:
    """Keep production unavailable until a separately reviewed chain replaces this gate."""
    return None


__all__ = ["build_speech"]
