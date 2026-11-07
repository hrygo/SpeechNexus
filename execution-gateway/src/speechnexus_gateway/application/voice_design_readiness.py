"""Gateway readiness for the VoiceDesign lane requires a real warmup synthesis.

An HTTP 200 from ``/health`` only proves that something listens; the verified
vLLM-Omni process answers health long before image capture finished, and its
first real request took about 60 s. The probe therefore issues one bounded
synthesis and reports ready only when that answer was a valid WAV preview. The
node controller owns the longer deployment-level warmup budget; this probe is the
gateway's own admission gate for serving a design request.
"""

from __future__ import annotations

from speechnexus_gateway.domain.voice_design import VoiceDesignSynthesizer


class VoiceDesignWarmupProbe:
    """One real synthesis per readiness check; a health-only answer never passes."""

    name = "voice_design_warmup"

    def __init__(self, synthesizer: VoiceDesignSynthesizer) -> None:
        self._synthesizer = synthesizer

    async def check(self) -> None:
        try:
            await self._synthesizer.warmup()
        except Exception as error:
            raise RuntimeError("VoiceDesign warmup synthesis failed") from error
