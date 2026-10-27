"""One frozen REST synthesis: text, published voice identity and request ceiling.

The public ``POST /v1/audio/speech`` surface returns complete audio. This module
models exactly what such a request fixes before any engine call: the text, the
published voice revision, the negotiated audio shape and the duration ceiling
that the engine policy must honour. It never selects defaults, relaxes a value
or rewrites a request; every absence is an explicit failure upstream.
"""

from __future__ import annotations

import hashlib
import math
import re
from dataclasses import dataclass, field
from enum import StrEnum

_IDENTIFIER = re.compile(r"[a-z0-9][a-z0-9._:-]{1,126}[a-z0-9]\Z")
_LANGUAGE = re.compile(r"[a-z]{2,3}(-[A-Za-z0-9]{2,8}){0,4}\Z")

MAX_SPEECH_TEXT_CHARS = 4096
MAX_SPEECH_TEXT_BYTES = 16384
MAX_SPEECH_INSTRUCTIONS_CHARS = 4096
MIN_AUDIO_MS = 100
MAX_AUDIO_MS = 3_600_000
MIN_PAYLOAD_BYTES = 1024
MAX_PAYLOAD_BYTES = 64 * 1024 * 1024
PCM_FRAME_BYTES = 2


class SpeechFormat(StrEnum):
    """Declared REST response formats; raw PCM is headless 16-bit little-endian."""

    MP3 = "mp3"
    WAV = "wav"
    PCM = "pcm"

    @property
    def media_type(self) -> str:
        return {"mp3": "audio/mpeg", "wav": "audio/wav", "pcm": "audio/pcm"}[self.value]

    @property
    def header_name(self) -> str:
        """``X-Audio-Format`` value; the contract uses the same three names."""
        return self.value


def pcm16_mono_bytes(duration_ms: int, sample_rate_hz: int) -> int:
    """Exact byte ceiling for PCM16 mono audio of one declared duration.

    The internal execution contract stores the hard ceiling as
    ``floor(max_output_audio_ms * sample_rate_hz / 1000)`` samples, so a shorter
    request must bound the source PCM the same way instead of inheriting the
    engine default. Channels are fixed at one for the REST PCM format.
    """
    if type(duration_ms) is not int or duration_ms < 0 or type(sample_rate_hz) is not int:
        raise ValueError("Invalid audio duration bound")
    return -((-duration_ms * sample_rate_hz) // 1000) * PCM_FRAME_BYTES


@dataclass(frozen=True, slots=True)
class SpeechCommand:
    """The request as the frozen public contract declares it, still unresolved."""

    model: str
    text: str = field(repr=False)
    voice: str
    response_format: SpeechFormat
    speed: float = 1.0
    instructions: str = field(default="", repr=False)
    workspace_id: str = "workspace-default"
    voice_revision: int | None = None
    profile_id: str | None = None
    profile_revision: int | None = None
    pronunciation_revision: int | None = None
    language: str | None = None
    seed: int | None = None
    planner_max_chars: int | None = None
    planner_max_wait_ms: int | None = None
    deadline_ms: int | None = None
    client_reference: str | None = None

    def __post_init__(self) -> None:
        if type(self.model) is not str or not 1 <= len(self.model) <= 128:
            raise ValueError("Invalid speech model")
        if (
            type(self.text) is not str
            or "\x00" in self.text
            or not 1 <= len(self.text) <= MAX_SPEECH_TEXT_CHARS
        ):
            raise ValueError("Invalid speech text")
        try:
            encoded = self.text.encode("utf-8")
        except UnicodeError:
            raise ValueError("Invalid speech text encoding") from None
        if len(encoded) > MAX_SPEECH_TEXT_BYTES:
            raise ValueError("Speech text exceeds the byte budget")
        if type(self.voice) is not str or not 1 <= len(self.voice) <= 128:
            raise ValueError("Invalid speech voice")
        if not isinstance(self.response_format, SpeechFormat):
            raise ValueError("Invalid speech response format")
        if (
            type(self.speed) not in (int, float)
            or not math.isfinite(self.speed)
            or not 0.25 <= self.speed <= 4.0
        ):
            raise ValueError("Invalid speech speed")
        if (
            type(self.instructions) is not str
            or "\x00" in self.instructions
            or len(self.instructions) > MAX_SPEECH_INSTRUCTIONS_CHARS
        ):
            raise ValueError("Invalid speech instructions")
        identifier(self.workspace_id)
        if self.voice_revision is not None:
            revision_number(self.voice_revision)
        if (self.profile_id is None) != (self.profile_revision is None):
            raise ValueError("A profile revision and identity are required together")
        if self.profile_id is not None:
            identifier(self.profile_id)
            assert self.profile_revision is not None
            revision_number(self.profile_revision)
        if self.pronunciation_revision is not None:
            revision_number(self.pronunciation_revision)
        if self.language is not None and (
            type(self.language) is not str or _LANGUAGE.fullmatch(self.language) is None
        ):
            raise ValueError("Invalid speech language")
        if self.seed is not None and (
            type(self.seed) is not int or not 0 <= self.seed <= 2**31 - 1
        ):
            raise ValueError("Invalid speech seed")
        if self.planner_max_chars is not None and (
            type(self.planner_max_chars) is not int or not 40 <= self.planner_max_chars <= 400
        ):
            raise ValueError("Invalid speech planner chunk size")
        if self.planner_max_wait_ms is not None and (
            type(self.planner_max_wait_ms) is not int or not 100 <= self.planner_max_wait_ms <= 3000
        ):
            raise ValueError("Invalid speech planner wait")
        if self.deadline_ms is not None and (
            type(self.deadline_ms) is not int or not 1 <= self.deadline_ms <= 600_000
        ):
            raise ValueError("Invalid speech deadline")
        if self.client_reference is not None and (
            type(self.client_reference) is not str or len(self.client_reference) > 256
        ):
            raise ValueError("Invalid speech client reference")


@dataclass(frozen=True, slots=True)
class SpeechPlan:
    """Everything one bounded synthesis may use; the ceiling is already tightened."""

    request_id: str
    text: str = field(repr=False)
    voice_id: str
    voice_revision_id: str
    response_format: SpeechFormat
    sample_rate_hz: int
    channels: int
    speed: float
    instructions: str = field(default="", repr=False)
    language: str | None = None
    seed: int | None = None
    planner_max_chars: int = 400
    planner_max_wait_ms: int = 500
    max_audio_ms: int = MAX_AUDIO_MS
    max_source_pcm_bytes: int = MAX_PAYLOAD_BYTES
    max_payload_bytes: int = MAX_PAYLOAD_BYTES
    timeout_seconds: float = 120.0

    def __post_init__(self) -> None:
        if type(self.request_id) is not str or _IDENTIFIER.fullmatch(self.request_id) is None:
            raise ValueError("Invalid speech request identity")
        if (
            type(self.text) is not str
            or "\x00" in self.text
            or not 1 <= len(self.text) <= MAX_SPEECH_TEXT_CHARS
        ):
            raise ValueError("Invalid speech text")
        if type(self.voice_id) is not str or _IDENTIFIER.fullmatch(self.voice_id) is None:
            raise ValueError("Invalid speech voice identity")
        if (
            type(self.voice_revision_id) is not str
            or _IDENTIFIER.fullmatch(self.voice_revision_id) is None
        ):
            raise ValueError("Invalid speech voice revision identity")
        if not isinstance(self.response_format, SpeechFormat):
            raise ValueError("Invalid speech response format")
        if type(self.sample_rate_hz) is not int or not 8000 <= self.sample_rate_hz <= 96000:
            raise ValueError("Invalid speech sample rate")
        if type(self.channels) is not int or self.channels != 1:
            raise ValueError("Speech plans require mono audio")
        if (
            type(self.speed) not in (int, float)
            or not math.isfinite(self.speed)
            or not 0.25 <= self.speed <= 4.0
        ):
            raise ValueError("Invalid speech speed")
        if (
            type(self.instructions) is not str
            or "\x00" in self.instructions
            or len(self.instructions) > MAX_SPEECH_INSTRUCTIONS_CHARS
        ):
            raise ValueError("Invalid speech instructions")
        if self.language is not None and (
            type(self.language) is not str or _LANGUAGE.fullmatch(self.language) is None
        ):
            raise ValueError("Invalid speech language")
        if self.seed is not None and (
            type(self.seed) is not int or not 0 <= self.seed <= 2**31 - 1
        ):
            raise ValueError("Invalid speech seed")
        if type(self.planner_max_chars) is not int or not 1 <= self.planner_max_chars <= 4096:
            raise ValueError("Invalid speech planner chunk size")
        if type(self.planner_max_wait_ms) is not int or not 100 <= self.planner_max_wait_ms <= 3000:
            raise ValueError("Invalid speech planner wait")
        if (
            type(self.max_audio_ms) is not int
            or not MIN_AUDIO_MS <= self.max_audio_ms <= MAX_AUDIO_MS
        ):
            raise ValueError("Invalid speech duration ceiling")
        if (
            type(self.max_source_pcm_bytes) is not int
            or not MIN_PAYLOAD_BYTES <= self.max_source_pcm_bytes <= MAX_PAYLOAD_BYTES
        ):
            raise ValueError("Invalid speech source PCM ceiling")
        if (
            type(self.max_payload_bytes) is not int
            or not MIN_PAYLOAD_BYTES <= self.max_payload_bytes <= MAX_PAYLOAD_BYTES
        ):
            raise ValueError("Invalid speech payload ceiling")
        if (
            type(self.timeout_seconds) not in (int, float)
            or not math.isfinite(self.timeout_seconds)
            or not 0 < self.timeout_seconds <= 600
        ):
            raise ValueError("Invalid speech timeout")


@dataclass(frozen=True, slots=True)
class SpeechAudio:
    """Complete audio for one plan; a structural mismatch is never relabelled."""

    payload: bytes = field(repr=False)
    response_format: SpeechFormat
    sample_rate_hz: int
    channels: int
    duration_ms: int

    def __post_init__(self) -> None:
        if type(self.payload) is not bytes or not self.payload:
            raise ValueError("Speech audio requires a non-empty payload")
        if not isinstance(self.response_format, SpeechFormat):
            raise ValueError("Invalid speech audio format")
        if type(self.sample_rate_hz) is not int or not 8000 <= self.sample_rate_hz <= 96000:
            raise ValueError("Invalid speech audio sample rate")
        if type(self.channels) is not int or self.channels != 1:
            raise ValueError("Speech audio must be mono")
        if type(self.duration_ms) is not int or self.duration_ms < 1:
            raise ValueError("Invalid speech audio duration")
        _check_shape(self)


def _check_shape(audio: SpeechAudio) -> None:
    """Reject a payload whose declared format, clock or frame length disagrees."""
    payload = audio.payload
    if audio.response_format is SpeechFormat.PCM:
        frame = PCM_FRAME_BYTES * audio.channels
        if len(payload) % frame:
            raise ValueError("PCM payload is not a whole number of frames")
        expected_ms = len(payload) // frame * 1000 / audio.sample_rate_hz
        if abs(expected_ms - audio.duration_ms) > 1:
            raise ValueError("PCM payload and declared duration disagree")
    elif audio.response_format is SpeechFormat.WAV:
        if len(payload) < 44 or payload[:4] != b"RIFF" or payload[8:12] != b"WAVE":
            raise ValueError("WAV payload has no RIFF/WAVE header")
        if int.from_bytes(payload[22:24], "little") != audio.channels:
            raise ValueError("WAV payload channel count disagrees")
        if int.from_bytes(payload[24:28], "little") != audio.sample_rate_hz:
            raise ValueError("WAV payload sample rate disagrees")
        if int.from_bytes(payload[34:36], "little") != PCM_FRAME_BYTES * 8:
            raise ValueError("WAV payload is not 16-bit PCM")
        data_frames = max(0, len(payload) - 44) // (PCM_FRAME_BYTES * audio.channels)
        expected_ms = data_frames * 1000 / audio.sample_rate_hz
        if abs(expected_ms - audio.duration_ms) > 1:
            raise ValueError("WAV payload and declared duration disagree")
    elif len(payload) < 4 or (
        (payload[0] != 0xFF or payload[1] & 0xE0 != 0xE0) and payload[:3] != b"ID3"
    ):
        raise ValueError("MP3 payload has no frame sync")


def identifier(value: str) -> str:
    if type(value) is not str or _IDENTIFIER.fullmatch(value) is None:
        raise ValueError("Invalid identifier")
    return value


def correlation_identifier(value: str) -> str:
    """Derive an identifier-shaped value from any platform correlation ID.

    The HTTP layer mints or accepts ``X-Request-ID`` values that are wider than
    the domain identifier grammar (uppercase hex, short trace IDs). A correlation
    ID is not a domain entity, so it must never be rejected here: accept it when
    it already fits, otherwise derive a stable identifier from its bytes.
    """
    if type(value) is str and _IDENTIFIER.fullmatch(value) is not None:
        return value
    if type(value) is not str or not value:
        raise ValueError("Invalid correlation identifier")
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:32]


def revision_number(value: int) -> int:
    if type(value) is not int or not 1 <= value <= 9_999_999_999_999_999_999:
        raise ValueError("Invalid revision")
    return value


__all__ = [
    "MAX_AUDIO_MS",
    "MAX_PAYLOAD_BYTES",
    "MAX_SPEECH_INSTRUCTIONS_CHARS",
    "MAX_SPEECH_TEXT_BYTES",
    "MAX_SPEECH_TEXT_CHARS",
    "MIN_AUDIO_MS",
    "MIN_PAYLOAD_BYTES",
    "PCM_FRAME_BYTES",
    "SpeechAudio",
    "SpeechCommand",
    "SpeechFormat",
    "SpeechPlan",
    "identifier",
    "pcm16_mono_bytes",
    "revision_number",
]
