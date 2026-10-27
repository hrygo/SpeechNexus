"""One frozen REST transcription: uploaded audio, decoded facts and the result.

The public ``POST /v1/audio/transcriptions`` surface returns one complete
transcript. This module models exactly what such a request fixes before any
engine call: the uploaded bytes, the requested transcript format, the resolved
language, the promoted prompt and the media ceiling the executor must honour.
It never selects defaults, relaxes a value or fabricates evidence; every
absence is an explicit failure upstream.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from enum import StrEnum

_IDENTIFIER = re.compile(r"[a-z0-9][a-z0-9._:-]{1,126}[a-z0-9]\Z")
_LANGUAGE = re.compile(r"[a-z]{2,3}(-[A-Za-z0-9]{2,8}){0,4}\Z")
# A request may ask the engine to detect the language; a finished transcript
# may not, so the sentinel is accepted on the plan only.
_LANGUAGE_REQUEST = re.compile(r"auto|[a-z]{2,3}(-[A-Za-z0-9]{2,8}){0,4}\Z")

MAX_TRANSCRIPTION_PROMPT_CHARS = 4000
MAX_TRANSCRIPTION_LANGUAGE_CHARS = 35
MAX_TRANSCRIPTION_CLIENT_REFERENCE_CHARS = 256
MIN_TRANSCRIPTION_AUDIO_MS = 100
MAX_TRANSCRIPTION_AUDIO_MS = 3_600_000
MIN_TRANSCRIPTION_UPLOAD_BYTES = 1024
MAX_TRANSCRIPTION_UPLOAD_BYTES = 64 * 1024 * 1024
TIMESTAMP_GRANULARITIES = ("segment", "word")


class TranscriptFormat(StrEnum):
    """Declared REST transcript formats; verbose formats need real evidence."""

    JSON = "json"
    TEXT = "text"
    VERBOSE_JSON = "verbose_json"
    SRT = "srt"
    VTT = "vtt"

    @property
    def media_type(self) -> str:
        if self is TranscriptFormat.TEXT:
            return "text/plain"
        if self in (TranscriptFormat.SRT, TranscriptFormat.VTT):
            return "text/plain"
        return "application/json"

    @property
    def needs_segments(self) -> bool:
        return self is not TranscriptFormat.JSON and self is not TranscriptFormat.TEXT


@dataclass(frozen=True, slots=True)
class TranscriptionCommand:
    """The request as the frozen public contract declares it, still unresolved."""

    model: str
    audio: bytes = field(repr=False)
    response_format: TranscriptFormat = TranscriptFormat.JSON
    language: str | None = None
    prompt: str = field(default="", repr=False)
    timestamp_granularities: tuple[str, ...] = ()
    workspace_id: str = "workspace-default"
    profile_id: str | None = None
    profile_revision: int | None = None
    deadline_ms: int | None = None
    client_reference: str | None = None
    # The frozen multipart schema has no media-ceiling field yet; the HTTP
    # boundary passes the deployment ceiling and a later contract field must be
    # folded in with ``min`` rather than replacing it.
    max_audio_ms: int | None = None
    max_upload_bytes: int | None = None
    max_segment_ms: int | None = None

    def __post_init__(self) -> None:
        if type(self.model) is not str or not 1 <= len(self.model) <= 128:
            raise ValueError("Invalid transcription model")
        if (
            type(self.audio) is not bytes
            or not MIN_TRANSCRIPTION_UPLOAD_BYTES
            <= len(self.audio)
            <= MAX_TRANSCRIPTION_UPLOAD_BYTES
        ):
            raise ValueError("Invalid transcription audio")
        if not isinstance(self.response_format, TranscriptFormat):
            raise ValueError("Invalid transcript format")
        if self.language is not None and (
            type(self.language) is not str
            or len(self.language) > MAX_TRANSCRIPTION_LANGUAGE_CHARS
            or _LANGUAGE.fullmatch(self.language) is None
        ):
            raise ValueError("Invalid transcription language")
        if (
            type(self.prompt) is not str
            or "\x00" in self.prompt
            or len(self.prompt) > MAX_TRANSCRIPTION_PROMPT_CHARS
        ):
            raise ValueError("Invalid transcription prompt")
        granularities = self.timestamp_granularities
        if (
            type(granularities) is not tuple
            or len(granularities) > 2
            or len(set(granularities)) != len(granularities)
            or any(value not in TIMESTAMP_GRANULARITIES for value in granularities)
        ):
            raise ValueError("Invalid timestamp granularities")
        if granularities and self.response_format is not TranscriptFormat.VERBOSE_JSON:
            raise ValueError("Timestamp granularities require verbose_json")
        identifier(self.workspace_id)
        if (self.profile_id is None) != (self.profile_revision is None):
            raise ValueError("A profile revision and identity are required together")
        if self.profile_id is not None:
            identifier(self.profile_id)
            assert self.profile_revision is not None
            revision_number(self.profile_revision)
        if self.deadline_ms is not None and (
            type(self.deadline_ms) is not int or not 1 <= self.deadline_ms <= 600_000
        ):
            raise ValueError("Invalid transcription deadline")
        if self.client_reference is not None and (
            type(self.client_reference) is not str
            or "\x00" in self.client_reference
            or len(self.client_reference) > MAX_TRANSCRIPTION_CLIENT_REFERENCE_CHARS
        ):
            raise ValueError("Invalid transcription client reference")
        if self.max_audio_ms is not None and (
            type(self.max_audio_ms) is not int
            or not MIN_TRANSCRIPTION_AUDIO_MS <= self.max_audio_ms <= MAX_TRANSCRIPTION_AUDIO_MS
        ):
            raise ValueError("Invalid transcription audio ceiling")
        if self.max_upload_bytes is not None and (
            type(self.max_upload_bytes) is not int
            or not MIN_TRANSCRIPTION_UPLOAD_BYTES
            <= self.max_upload_bytes
            <= MAX_TRANSCRIPTION_UPLOAD_BYTES
        ):
            raise ValueError("Invalid transcription upload ceiling")
        if self.max_segment_ms is not None and (
            type(self.max_segment_ms) is not int or not 1000 <= self.max_segment_ms <= 30_000
        ):
            raise ValueError("Invalid transcription segment ceiling")


@dataclass(frozen=True, slots=True)
class AudioFacts:
    """Observed facts about the uploaded audio; never inferred from the name."""

    media_type: str
    codec: str
    duration_ms: int
    sample_rate_hz: int
    channels: int

    def __post_init__(self) -> None:
        if type(self.media_type) is not str or not 1 <= len(self.media_type) <= 64:
            raise ValueError("Invalid audio media type")
        if type(self.codec) is not str or not 1 <= len(self.codec) <= 64:
            raise ValueError("Invalid audio codec")
        if (
            type(self.duration_ms) is not int
            or not 0 < self.duration_ms <= MAX_TRANSCRIPTION_AUDIO_MS
        ):
            raise ValueError("Invalid audio duration")
        if type(self.sample_rate_hz) is not int or not 8000 <= self.sample_rate_hz <= 192000:
            raise ValueError("Invalid audio sample rate")
        if type(self.channels) is not int or not 1 <= self.channels <= 8:
            raise ValueError("Invalid audio channel count")


@dataclass(frozen=True, slots=True)
class TranscriptionPlan:
    """Everything one bounded transcription may use; the ceilings are applied."""

    request_id: str
    audio: bytes = field(repr=False)
    audio_media_type: str
    response_format: TranscriptFormat
    language: str
    prompt: str = field(default="", repr=False)
    timestamp_granularities: tuple[str, ...] = ()
    max_audio_ms: int = MAX_TRANSCRIPTION_AUDIO_MS
    max_upload_bytes: int = MAX_TRANSCRIPTION_UPLOAD_BYTES
    max_segment_ms: int = 30_000
    require_segments: bool = False
    require_words: bool = False
    timeout_seconds: float = 120.0

    def __post_init__(self) -> None:
        if type(self.request_id) is not str or _IDENTIFIER.fullmatch(self.request_id) is None:
            raise ValueError("Invalid transcription request identity")
        if (
            type(self.audio) is not bytes
            or not MIN_TRANSCRIPTION_UPLOAD_BYTES
            <= len(self.audio)
            <= MAX_TRANSCRIPTION_UPLOAD_BYTES
        ):
            raise ValueError("Invalid transcription audio")
        if len(self.audio) > self.max_upload_bytes:
            raise ValueError("Transcription audio exceeds its upload ceiling")
        if type(self.audio_media_type) is not str or not 1 <= len(self.audio_media_type) <= 64:
            raise ValueError("Invalid audio media type")
        if not isinstance(self.response_format, TranscriptFormat):
            raise ValueError("Invalid transcript format")
        if (
            type(self.language) is not str
            or len(self.language) > MAX_TRANSCRIPTION_LANGUAGE_CHARS
            or _LANGUAGE_REQUEST.fullmatch(self.language) is None
        ):
            raise ValueError("Invalid transcription language")
        if (
            type(self.prompt) is not str
            or "\x00" in self.prompt
            or len(self.prompt) > MAX_TRANSCRIPTION_PROMPT_CHARS
        ):
            raise ValueError("Invalid transcription prompt")
        granularities = self.timestamp_granularities
        if (
            type(granularities) is not tuple
            or len(granularities) > 2
            or any(value not in TIMESTAMP_GRANULARITIES for value in granularities)
        ):
            raise ValueError("Invalid timestamp granularities")
        if (
            type(self.max_audio_ms) is not int
            or not MIN_TRANSCRIPTION_AUDIO_MS <= self.max_audio_ms <= MAX_TRANSCRIPTION_AUDIO_MS
        ):
            raise ValueError("Invalid transcription audio ceiling")
        if (
            type(self.max_upload_bytes) is not int
            or not MIN_TRANSCRIPTION_UPLOAD_BYTES
            <= self.max_upload_bytes
            <= MAX_TRANSCRIPTION_UPLOAD_BYTES
        ):
            raise ValueError("Invalid transcription upload ceiling")
        if type(self.max_segment_ms) is not int or not 1000 <= self.max_segment_ms <= 30_000:
            raise ValueError("Invalid transcription segment ceiling")
        if type(self.require_segments) is not bool or type(self.require_words) is not bool:
            raise ValueError("Invalid transcript evidence requirement")
        if (
            type(self.timeout_seconds) not in (int, float)
            or not math.isfinite(self.timeout_seconds)
            or not 0 < self.timeout_seconds <= 600
        ):
            raise ValueError("Invalid transcription timeout")


@dataclass(frozen=True, slots=True)
class TranscriptSegment:
    """One real segment boundary reported by the engine, already bounded."""

    id: int
    start_ms: int
    end_ms: int
    text: str
    tokens: tuple[int, ...]
    avg_logprob: float
    compression_ratio: float
    no_speech_prob: float
    seek: int
    temperature: float

    def __post_init__(self) -> None:
        if type(self.id) is not int or self.id < 0:
            raise ValueError("Invalid transcript segment identity")
        if (
            type(self.start_ms) is not int
            or type(self.end_ms) is not int
            or not 0 <= self.start_ms < self.end_ms
        ):
            raise ValueError("Invalid transcript segment boundary")
        if type(self.text) is not str or "\x00" in self.text:
            raise ValueError("Invalid transcript segment text")
        if type(self.tokens) is not tuple or any(
            type(token) is not int or token < 0 for token in self.tokens
        ):
            raise ValueError("Invalid transcript segment tokens")
        for value in (self.avg_logprob, self.compression_ratio, self.temperature):
            if type(value) not in (int, float) or not math.isfinite(value):
                raise ValueError("Invalid transcript segment score")
        if (
            type(self.no_speech_prob) not in (int, float)
            or not math.isfinite(self.no_speech_prob)
            or not 0 <= self.no_speech_prob <= 1
        ):
            raise ValueError("Invalid transcript segment speech probability")
        if type(self.seek) is not int or self.seek < 0:
            raise ValueError("Invalid transcript segment seek")


@dataclass(frozen=True, slots=True)
class TranscriptWord:
    """One real word boundary reported by the engine, already bounded."""

    word: str
    start_ms: int
    end_ms: int

    def __post_init__(self) -> None:
        if type(self.word) is not str or "\x00" in self.word:
            raise ValueError("Invalid transcript word")
        if (
            type(self.start_ms) is not int
            or type(self.end_ms) is not int
            or not 0 <= self.start_ms < self.end_ms
        ):
            raise ValueError("Invalid transcript word boundary")


@dataclass(frozen=True, slots=True)
class TranscriptionResult:
    """One complete transcript; nothing here may be estimated or truncated."""

    text: str
    language: str
    duration_ms: int
    segments: tuple[TranscriptSegment, ...] = ()
    words: tuple[TranscriptWord, ...] = ()

    def __post_init__(self) -> None:
        if type(self.text) is not str or "\x00" in self.text:
            raise ValueError("Invalid transcript text")
        if (
            type(self.language) is not str
            or not 1 <= len(self.language) <= MAX_TRANSCRIPTION_LANGUAGE_CHARS
            or _LANGUAGE.fullmatch(self.language) is None
        ):
            raise ValueError("Invalid transcript language")
        if (
            type(self.duration_ms) is not int
            or not 0 < self.duration_ms <= MAX_TRANSCRIPTION_AUDIO_MS
        ):
            raise ValueError("Invalid transcript duration")
        if type(self.segments) is not tuple or any(
            not isinstance(segment, TranscriptSegment) for segment in self.segments
        ):
            raise ValueError("Invalid transcript segments")
        if type(self.words) is not tuple or any(
            not isinstance(word, TranscriptWord) for word in self.words
        ):
            raise ValueError("Invalid transcript words")


def identifier(value: str) -> str:
    if type(value) is not str or _IDENTIFIER.fullmatch(value) is None:
        raise ValueError("Invalid identifier")
    return value


def revision_number(value: int) -> int:
    if type(value) is not int or not 1 <= value <= 9_999_999_999_999_999_999:
        raise ValueError("Invalid revision")
    return value


__all__ = [
    "MAX_TRANSCRIPTION_AUDIO_MS",
    "MAX_TRANSCRIPTION_CLIENT_REFERENCE_CHARS",
    "MAX_TRANSCRIPTION_LANGUAGE_CHARS",
    "MAX_TRANSCRIPTION_PROMPT_CHARS",
    "MAX_TRANSCRIPTION_UPLOAD_BYTES",
    "MIN_TRANSCRIPTION_AUDIO_MS",
    "MIN_TRANSCRIPTION_UPLOAD_BYTES",
    "TIMESTAMP_GRANULARITIES",
    "AudioFacts",
    "TranscriptFormat",
    "TranscriptSegment",
    "TranscriptWord",
    "TranscriptionCommand",
    "TranscriptionPlan",
    "TranscriptionResult",
    "identifier",
    "revision_number",
]
