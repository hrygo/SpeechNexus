"""Native Base voice-clone facts; a separate stage from VoiceDesign and CustomVoice.

The engine validates a checkpoint against exactly one ``task_type``, so a Base
clone instance cannot share a process with VoiceDesign or CustomVoice and cannot
switch task inside one endpoint. This module owns the clone lane's policy,
reference-material verification, request and failure domain, while reusing the
neutral bounded PCM16 delivery types that describe the same 24 kHz mono clock.

Reference audio is the only private content this lane accepts as an input. It is
structurally verified here (plain RIFF/WAVE, mono, 16-bit, an approved sample
rate, bounded size and duration) and never rendered into a representation,
error message or log line.
"""

from __future__ import annotations

import math
import re
import struct
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol

from speechnexus_gateway.domain.canonical import command_digest
from speechnexus_gateway.domain.file_execution import EngineRequestRejectedError
from speechnexus_gateway.domain.languages import ENGINE_LANGUAGES
from speechnexus_gateway.domain.tts import TtsAudio, TtsEngineFormat

VOICE_CLONE_RECIPE_VERSION = "file-voice-clone-1"
VOICE_CLONE_SAMPLE_RATE_HZ = 24000
MAX_VOICE_CLONE_PREVIEW_CHARS = 2000
MAX_REFERENCE_TEXT_CHARS = 10000
# A clone reference is read at ordinary speech tempo. This is a plausibility
# bound on a paired transcript, not a content comparison: the gateway cannot
# prove the text matches what the clip says, so it refuses only a pairing no
# speaker could produce and keeps semantic correspondence unverified.
MAX_REFERENCE_TEXT_CHARS_PER_SECOND = 25
REFERENCE_SAMPLE_RATES = (16000, 24000)
MIN_REFERENCE_AUDIO_MS = 1000
MAX_REFERENCE_AUDIO_MS = 60000
MAX_REFERENCE_AUDIO_BYTES = 8 * 1024 * 1024
# The approved preview ceiling is one minute of 24 kHz mono PCM16.
MAX_VOICE_CLONE_PCM_BYTES = 60 * 24000 * 2

_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")
_LANGUAGE = re.compile(r"(auto|[a-z]{2,3}(-[A-Za-z0-9]{2,8})*)")
_MAX_LANGUAGE_CHARS = 35
_TARGET_IDENTIFIER = re.compile(r"[a-z0-9][a-z0-9._:-]{1,126}[a-z0-9]")
_MAX_WAV_CHUNKS = 32


class VoiceCloneFailure(StrEnum):
    CANCELLED = "voice_clone_cancelled"
    DEADLINE_EXCEEDED = "voice_clone_deadline_exceeded"
    TRANSPORT_UNKNOWN = "voice_clone_transport_unknown"
    # A client-error answer is terminal for this request and never a timeout.
    ENGINE_REJECTED = "voice_clone_engine_rejected"
    RESPONSE_INVALID = "voice_clone_response_invalid"
    OUTPUT_LIMIT_EXCEEDED = "voice_clone_output_limit_exceeded"


class VoiceCloneCallError(RuntimeError):
    """One failed clone call.

    ``request_may_have_run`` never claims the engine stopped. A client-error
    rejection is the only failure that positively reports ``False``; every
    timeout, transport failure and server error keeps it conservatively true.
    """

    def __init__(
        self,
        code: VoiceCloneFailure,
        *,
        request_may_have_run: bool,
    ) -> None:
        super().__init__(code.value)
        self.code = code
        self.request_may_have_run = request_may_have_run


class VoiceCloneRejectedError(VoiceCloneCallError, EngineRequestRejectedError):
    """A proven client-error refusal: terminal for this request, not a retry signal."""

    def __init__(self) -> None:
        super().__init__(VoiceCloneFailure.ENGINE_REJECTED, request_may_have_run=False)


class VoiceCloneTask(StrEnum):
    """The one engine task a Base clone binding may serve."""

    BASE = "Base"


class ReferenceTextMode(StrEnum):
    """How the paired reference text is sent to the engine.

    ``ICL`` sends ``ref_text`` and lets the engine condition on both the clip and
    its transcript. ``X_VECTOR_ONLY`` omits ``ref_text`` and asks the engine for
    the speaker vector alone, which is accepted without a transcript but may
    reduce similarity. The mode is derived, never a free caller field.
    """

    ICL = "icl"
    X_VECTOR_ONLY = "x_vector_only"


@dataclass(frozen=True, slots=True, repr=False)
class ReferenceAudio:
    """Structurally verified mono 16-bit PCM plus its source sample rate."""

    pcm16: bytes = field(repr=False)
    sample_rate_hz: int

    def __post_init__(self) -> None:
        if (
            type(self.pcm16) is not bytes
            or not self.pcm16
            or len(self.pcm16) % 2
            or self.sample_rate_hz not in REFERENCE_SAMPLE_RATES
            or self.duration_ms < 1
        ):
            raise ValueError("invalid reference audio")

    @property
    def duration_ms(self) -> int:
        return len(self.pcm16) // 2 * 1000 // self.sample_rate_hz

    def wav(self) -> bytes:
        """One canonical mono 16-bit RIFF/WAVE envelope over the verified PCM."""
        rate = self.sample_rate_hz
        pcm = self.pcm16
        return (
            b"RIFF"
            + struct.pack("<I", 36 + len(pcm))
            + b"WAVE"
            + b"fmt "
            + struct.pack("<IHHIIHH", 16, 1, 1, rate, rate * 2, 2, 16)
            + b"data"
            + struct.pack("<I", len(pcm))
            + pcm
        )

    @classmethod
    def from_wav(
        cls,
        payload: bytes,
        *,
        sample_rates: Iterable[int] = REFERENCE_SAMPLE_RATES,
        min_duration_ms: int = MIN_REFERENCE_AUDIO_MS,
        max_duration_ms: int = MAX_REFERENCE_AUDIO_MS,
        max_bytes: int = MAX_REFERENCE_AUDIO_BYTES,
    ) -> ReferenceAudio:
        """Accept only the verified reference shape; no resampling or repair."""
        audio = _parse_reference_wav(payload, sample_rates=sample_rates, max_bytes=max_bytes)
        duration = audio.duration_ms
        if not min_duration_ms <= duration <= max_duration_ms:
            raise ValueError("reference audio duration is outside the approved bound")
        return audio


def _parse_reference_wav(
    payload: bytes, *, sample_rates: Iterable[int], max_bytes: int
) -> ReferenceAudio:
    allowed = tuple(sample_rates)
    if (
        type(payload) is not bytes
        or type(max_bytes) is not int
        or not 1 <= max_bytes <= MAX_REFERENCE_AUDIO_BYTES
        or len(payload) < 44
        or len(payload) > max_bytes
        or payload[:4] != b"RIFF"
        or payload[8:12] != b"WAVE"
        or not allowed
        or any(rate not in REFERENCE_SAMPLE_RATES for rate in allowed)
    ):
        raise ValueError("invalid reference audio")
    declared = struct.unpack_from("<I", payload, 4)[0]
    end_limit = declared + 8
    if end_limit != len(payload):
        raise ValueError("invalid reference audio")
    offset = 12
    chunks = 0
    format_seen = False
    data: bytes | None = None
    rate: int | None = None
    while offset < end_limit:
        if offset + 8 > end_limit:
            raise ValueError("invalid reference audio")
        chunk_id = payload[offset : offset + 4]
        size = struct.unpack_from("<I", payload, offset + 4)[0]
        body = offset + 8
        end = body + size
        if end > end_limit:
            raise ValueError("invalid reference audio")
        chunks += 1
        if chunks > _MAX_WAV_CHUNKS:
            raise ValueError("invalid reference audio")
        if chunk_id == b"fmt ":
            if format_seen or size != 16:
                raise ValueError("invalid reference audio")
            format_seen = True
            fields = struct.unpack_from("<HHIIHH", payload, body)
            audio_format, channels, rate_hz, byte_rate, block_align, bits = fields
            if (
                audio_format != 1
                or channels != 1
                or bits != 16
                or block_align != 2
                or rate_hz not in allowed
                or byte_rate != rate_hz * 2
            ):
                raise ValueError("invalid reference audio")
            rate = rate_hz
        elif chunk_id == b"data":
            if data is not None or size == 0 or size % 2:
                raise ValueError("invalid reference audio")
            data = payload[body:end]
        offset = end + (size & 1)
    if not format_seen or data is None or rate is None or offset != end_limit:
        raise ValueError("invalid reference audio")
    return ReferenceAudio(data, rate)


def reference_text_fits_duration(text: str, audio: ReferenceAudio) -> bool:
    """Reject only a transcript too long for the clip to contain it.

    Silence is legitimate reference material, so density has no lower bound: a
    six-second clip may hold a single short phrase. The upper bound is a
    deliberate over-count guard, not a claim that the words match the audio.
    """
    seconds = audio.duration_ms / 1000
    return len(text) <= max(1, int(seconds * MAX_REFERENCE_TEXT_CHARS_PER_SECOND))


@dataclass(frozen=True, slots=True)
class VoiceCloneExecutionTarget:
    """The admitted Base clone engine target, independent of transport types."""

    host_node_id: str
    gateway_id: str
    pool_id: str
    instance_id: str
    instance_generation: int
    binding_revision_id: str

    def __post_init__(self) -> None:
        if (
            any(
                type(value) is not str or _TARGET_IDENTIFIER.fullmatch(value) is None
                for value in (
                    self.host_node_id,
                    self.gateway_id,
                    self.instance_id,
                    self.binding_revision_id,
                )
            )
            or self.pool_id != "voice-creation"
            or type(self.instance_generation) is not int
            or not 1 <= self.instance_generation <= 2**63 - 1
        ):
            raise ValueError("invalid configured voice clone execution target")


@dataclass(frozen=True, slots=True)
class VoiceClonePolicy:
    """Frozen per-binding Base clone policy; reference material comes per request."""

    config_revision: str
    task: VoiceCloneTask = VoiceCloneTask.BASE
    engine_format: TtsEngineFormat = TtsEngineFormat.WAV
    sample_rate_hz: int = VOICE_CLONE_SAMPLE_RATE_HZ
    max_pcm_bytes: int = MAX_VOICE_CLONE_PCM_BYTES
    max_audio_ms: int = 60000
    timeout_seconds: float = 300.0
    supported_languages: tuple[str, ...] = tuple(ENGINE_LANGUAGES)
    reference_sample_rates: tuple[int, ...] = REFERENCE_SAMPLE_RATES
    min_reference_audio_ms: int = MIN_REFERENCE_AUDIO_MS
    max_reference_audio_ms: int = MAX_REFERENCE_AUDIO_MS
    max_reference_audio_bytes: int = MAX_REFERENCE_AUDIO_BYTES
    # ICL needs a paired transcript. X-vector-only mode is an explicit opt-in
    # because dropping the pairing is a quality decision, not a default.
    allow_x_vector_only: bool = False

    def __post_init__(self) -> None:
        if (
            type(self.config_revision) is not str
            or _IDENTIFIER.fullmatch(self.config_revision) is None
            or self.task is not VoiceCloneTask.BASE
            or self.engine_format is not TtsEngineFormat.WAV
            or self.sample_rate_hz != VOICE_CLONE_SAMPLE_RATE_HZ
            or type(self.max_pcm_bytes) is not int
            or not 1024 <= self.max_pcm_bytes <= MAX_VOICE_CLONE_PCM_BYTES
            or type(self.max_audio_ms) is not int
            or not 1 <= self.max_audio_ms <= 600000
            or type(self.timeout_seconds) not in (int, float)
            or not math.isfinite(self.timeout_seconds)
            or not 0 < self.timeout_seconds <= 600
            or type(self.supported_languages) is not tuple
            or not self.supported_languages
            or len(set(self.supported_languages)) != len(self.supported_languages)
            or any(tag not in ENGINE_LANGUAGES for tag in self.supported_languages)
            or type(self.reference_sample_rates) is not tuple
            or not self.reference_sample_rates
            or tuple(sorted(set(self.reference_sample_rates))) != self.reference_sample_rates
            or any(rate not in REFERENCE_SAMPLE_RATES for rate in self.reference_sample_rates)
            or type(self.min_reference_audio_ms) is not int
            or type(self.max_reference_audio_ms) is not int
            or not self.min_reference_audio_ms >= MIN_REFERENCE_AUDIO_MS
            or not self.min_reference_audio_ms < self.max_reference_audio_ms
            or self.max_reference_audio_ms > MAX_REFERENCE_AUDIO_MS
            or type(self.max_reference_audio_bytes) is not int
            or not 1024 <= self.max_reference_audio_bytes <= MAX_REFERENCE_AUDIO_BYTES
            or type(self.allow_x_vector_only) is not bool
        ):
            raise ValueError("invalid frozen voice clone policy")
        if self.max_pcm_bytes > -((-self.max_audio_ms * self.sample_rate_hz) // 1000) * 2:
            raise ValueError("voice clone policy PCM bound contradicts its audio ceiling")

    @property
    def digest(self) -> str:
        return command_digest(
            {
                "recipe": VOICE_CLONE_RECIPE_VERSION,
                "config_revision": self.config_revision,
                "task": self.task.value,
                "engine_format": self.engine_format.value,
                "sample_rate_hz": self.sample_rate_hz,
                "max_pcm_bytes": self.max_pcm_bytes,
                "max_audio_ms": self.max_audio_ms,
                "timeout_seconds": self.timeout_seconds,
                "supported_languages": list(self.supported_languages),
                "reference_sample_rates": list(self.reference_sample_rates),
                "min_reference_audio_ms": self.min_reference_audio_ms,
                "max_reference_audio_ms": self.max_reference_audio_ms,
                "max_reference_audio_bytes": self.max_reference_audio_bytes,
                "allow_x_vector_only": self.allow_x_vector_only,
            }
        )


@dataclass(frozen=True, slots=True)
class VoiceCloneRequest:
    """One frozen clone request: reference material plus the text to speak.

    ``reference_text is None`` selects x-vector-only mode; any string selects
    ICL mode. The policy still decides whether x-vector-only is allowed.
    """

    request_id: str
    preview_text: str = field(repr=False)
    language: str
    reference_audio: ReferenceAudio = field(repr=False)
    reference_text: str | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if (
            type(self.request_id) is not str
            or _IDENTIFIER.fullmatch(self.request_id) is None
            or type(self.preview_text) is not str
            or "\x00" in self.preview_text
            or not 1 <= len(self.preview_text) <= MAX_VOICE_CLONE_PREVIEW_CHARS
            or type(self.language) is not str
            or len(self.language) > _MAX_LANGUAGE_CHARS
            or _LANGUAGE.fullmatch(self.language) is None
            or not isinstance(self.reference_audio, ReferenceAudio)
            or (
                self.reference_text is not None
                and (
                    type(self.reference_text) is not str
                    or "\x00" in self.reference_text
                    or not self.reference_text.strip()
                    or not 1 <= len(self.reference_text) <= MAX_REFERENCE_TEXT_CHARS
                )
            )
        ):
            raise ValueError("invalid frozen voice clone request")
        try:
            self.preview_text.encode("utf-8")
            if self.reference_text is not None:
                self.reference_text.encode("utf-8")
        except UnicodeError:
            raise ValueError("invalid voice clone request encoding") from None
        if self.reference_text is not None and not reference_text_fits_duration(
            self.reference_text, self.reference_audio
        ):
            raise ValueError("the reference transcript cannot belong to that clip duration")

    @property
    def mode(self) -> ReferenceTextMode:
        return (
            ReferenceTextMode.ICL
            if self.reference_text is not None
            else ReferenceTextMode.X_VECTOR_ONLY
        )


class VoiceCloneSynthesizer(Protocol):
    """One engine call per admitted clone; no retry and no release authority."""

    @property
    def binding_revision_id(self) -> str: ...

    def validate_execution_target(self, target: VoiceCloneExecutionTarget) -> None:
        """Reject an unmatched node, generation or binding before any engine call."""
        ...

    async def synthesize(
        self,
        request: VoiceCloneRequest,
        policy: VoiceClonePolicy,
        *,
        cancelled: Callable[[], bool],
    ) -> TtsAudio: ...

    async def warmup(self) -> None:
        """Issue one real bounded clone synthesis; an HTTP 200 health answer is not readiness."""
        ...
