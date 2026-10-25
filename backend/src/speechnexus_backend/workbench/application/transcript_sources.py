"""Public-speech read boundary, with no transport DTOs in the application.

The shared code and port definitions live in ``speech_sources``; the historical names stay
importable so existing transcript consumers keep one definition, not a second copy.
"""

from speechnexus_backend.workbench.application.speech_sources import (
    SourceErrorCode as SourceErrorCode,
)
from speechnexus_backend.workbench.application.speech_sources import (
    SpeechSourceError as SpeechSourceError,
)
from speechnexus_backend.workbench.application.speech_sources import (
    TranscriptSourceReader as TranscriptSourceReader,
)

TranscriptSourceError = SpeechSourceError
