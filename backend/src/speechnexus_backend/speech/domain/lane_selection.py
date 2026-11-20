"""Which dispatch kinds one resident TTS variant can serve.

The reference deployment assembles `asr` plus at most one resident TTS variant:
CustomVoice (`tts`), VoiceDesign and Base clone are three separate
checkpoint/task loads, and one gateway process refuses to start with two of them
configured (`execution-gateway/src/speechnexus_gateway/file_speech_runtime.py:179`).
A dispatch selection naming two of them could therefore never be served by the
gateway the deployment stands up.

This module is the single place that spells the rule: the container startup gate
(`deploy/job-startup-gate.py`) and the read-only pre-deployment self-check
(`scripts/dev-env/check-async-job-prereqs.py`) both call it, so the two cannot
drift into "the check passes, the gate refuses". It is deliberately free of
third-party imports so the self-check can load it without the workspace venv.
"""

from __future__ import annotations

from collections.abc import Iterable

DEFAULT_KINDS_ENV = "SPEECHNEXUS_JOB_DISPATCH_KINDS"

# The lanes whose gateway config the runtime loads *instead of* the others. A
# finding lists them in the order the selection names them, so the operator reads
# back what was written.
RESIDENT_TTS_VARIANT_KINDS: tuple[str, ...] = ("tts", "voice_design", "voice_clone")


def resident_tts_variant_findings(
    kinds: Iterable[str], *, kinds_env: str = DEFAULT_KINDS_ENV
) -> list[str]:
    """Refuse a selection that names more than one resident TTS variant.

    At most one finding is returned, so a caller prints one named failure per
    unservable selection; an empty list means the selection names one or none.
    Duplicate kinds are collapsed before counting, like the rest of the dispatch
    vocabulary.
    """
    selected = [kind for kind in dict.fromkeys(kinds) if kind in RESIDENT_TTS_VARIANT_KINDS]
    if len(selected) < 2:
        return []
    return [
        f"{kinds_env} names {', '.join(selected)}: this gateway serves one resident TTS "
        "variant at a time (CustomVoice, VoiceDesign or Base clone are three separate "
        "engine loads and the gateway refuses to start with two configured). Select the "
        "one variant this deployment runs"
    ]
