"""Compatibility exports for the shared native media kernel."""

from speechnexus_media.domain import (
    MEDIA_RECIPE_VERSION,
    AudioDecodeLimits,
    AudioDecoder,
    AudioDecodeStream,
    AudioDecodeSummary,
    AudioSourceFormat,
    MediaCancelledError,
    MediaError,
    MediaLimitError,
    MediaTimelineError,
    PcmBlock,
    SourceAudioClock,
)

__all__ = [
    "MEDIA_RECIPE_VERSION",
    "AudioDecodeLimits",
    "AudioDecodeStream",
    "AudioDecodeSummary",
    "AudioDecoder",
    "AudioSourceFormat",
    "MediaCancelledError",
    "MediaError",
    "MediaLimitError",
    "MediaTimelineError",
    "PcmBlock",
    "SourceAudioClock",
]
