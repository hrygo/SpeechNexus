"""Gateway readiness for the VoiceDesign lane requires a real, cached warmup synthesis.

The probe and its budgets live in :mod:`speechnexus_gateway.application.engine_warmup`;
this module only names the lane so readiness reports identify which resident TTS
variant is still cold.
"""

from __future__ import annotations

from speechnexus_gateway.application.engine_warmup import (
    DEFAULT_RETRY_INTERVAL_SECONDS,
    DEFAULT_WARMUP_TIMEOUT_SECONDS,
    MAX_WARMUP_TIMEOUT_SECONDS,
    CachedWarmupProbe,
)

__all__ = [
    "DEFAULT_RETRY_INTERVAL_SECONDS",
    "DEFAULT_WARMUP_TIMEOUT_SECONDS",
    "MAX_WARMUP_TIMEOUT_SECONDS",
    "VoiceDesignWarmupProbe",
]


class VoiceDesignWarmupProbe(CachedWarmupProbe):
    """One cached VoiceDesign warmup synthesis; a health answer never passes."""

    name = "voice_design_warmup"
