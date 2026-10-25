"""Metadata projections, independent of transport and transcript content loading."""

from dataclasses import dataclass, field
from datetime import datetime

from speechnexus_backend.workbench.domain.versions import SpeechSource, Transcript


@dataclass(frozen=True, slots=True)
class TranscriptSummary:
    id: str
    project_id: str
    display_name: str = field(repr=False)
    head_revision: int
    source: SpeechSource | None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_transcript(cls, transcript: Transcript) -> TranscriptSummary:
        return cls(
            transcript.id,
            transcript.project_id,
            transcript.display_name,
            transcript.head.number,
            transcript.head.source,
            transcript.created_at,
            transcript.head.created_at,
        )
