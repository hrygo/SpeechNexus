"""Gateway readiness for the Base voice-clone lane requires a real warmup synthesis.

A Base instance is a resident TTS variant as well, so the same cached background
probe applies; its attempt reuses the approved reference clip and transcript
instead of inventing a voice.
"""

from __future__ import annotations

from speechnexus_gateway.application.engine_warmup import CachedWarmupProbe


class VoiceCloneWarmupProbe(CachedWarmupProbe):
    """One cached Base clone warmup synthesis; a health answer never passes."""

    name = "voice_clone_warmup"
