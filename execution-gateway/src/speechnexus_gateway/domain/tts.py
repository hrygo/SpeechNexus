"""Native bounded speech synthesis facts; no vendor DTOs, addresses or stop assertions.

The gateway only models what it can verify: a frozen policy, exactly one engine
call per admitted synthesis, normalized mono PCM16, and a bounded delivery
encoding. Engine identity, engine-side retries and resource release stay outside
this module.
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol

from speechnexus_gateway.domain.canonical import command_digest

TTS_RECIPE_VERSION = "file-tts-1"
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")
_LANGUAGE = re.compile(r"[a-z]{2,3}(-[A-Za-z0-9]{2,8}){0,4}")
_TARGET_IDENTIFIER = re.compile(r"[a-z0-9][a-z0-9._:-]{1,126}[a-z0-9]")

MAX_TTS_TEXT_CHARS = 4096
MAX_TTS_TEXT_BYTES = 16384
MAX_TTS_INSTRUCTIONS_CHARS = 4096
ENGINE_SAMPLE_RATES = (8000, 24000)


class TtsFailure(StrEnum):
    CANCELLED = "tts_cancelled"
    DEADLINE_EXCEEDED = "tts_deadline_exceeded"
    TRANSPORT_UNKNOWN = "tts_transport_unknown"
    RESPONSE_INVALID = "tts_response_invalid"
    OUTPUT_TRUNCATED = "tts_output_truncated"
    OUTPUT_LIMIT_EXCEEDED = "tts_output_limit_exceeded"


class TtsCallError(RuntimeError):
    """One failed synthesis call.

    ``request_may_have_run`` never claims the engine stopped; it only states
    whether bytes may already have left the gateway, so callers keep owning the
    execution budget until independent stop evidence exists.
    """

    def __init__(
        self,
        code: TtsFailure,
        *,
        request_may_have_run: bool,
        audio_bytes: int | None = None,
    ) -> None:
        super().__init__(code.value)
        self.code = code
        self.request_may_have_run = request_may_have_run
        self.audio_bytes = audio_bytes


class TtsDeliveryFailure(StrEnum):
    CANCELLED = "tts_delivery_cancelled"
    LIMIT_EXCEEDED = "tts_delivery_limit_exceeded"
    CODEC_UNAVAILABLE = "tts_delivery_codec_unavailable"
    ENCODE_FAILED = "tts_delivery_failed"


class TtsDeliveryError(ValueError):
    """A bounded delivery encoding could not be produced; no engine work changes."""

    def __init__(self, code: TtsDeliveryFailure, *, payload_bytes: int | None = None) -> None:
        super().__init__(code.value)
        self.code = code
        self.payload_bytes = payload_bytes


class TtsEngineFormat(StrEnum):
    """Formats this gateway requests from the engine and structurally verifies."""

    WAV = "wav"
    PCM = "pcm"


class TtsTask(StrEnum):
    """Engine task that a binding's checkpoint is qualified for.

    Only ``CustomVoice`` has been exercised end to end; ``Base`` and
    ``VoiceDesign`` need their own reference-material and quality units.
    """

    CUSTOM_VOICE = "CustomVoice"


class TtsDeliveryFormat(StrEnum):
    """Formats the gateway can hand to an owning business service."""

    WAV = "wav"
    MP3 = "mp3"
    PCM = "pcm"

    @property
    def media_type(self) -> str:
        return {"wav": "audio/wav", "mp3": "audio/mpeg", "pcm": "audio/pcm"}[self.value]


@dataclass(frozen=True, slots=True)
class TtsExecutionTarget:
    """The admitted batch-TTS engine target, independent of transport and vendor types."""

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
            or self.pool_id != "tts-batch"
            or type(self.instance_generation) is not int
            or not 1 <= self.instance_generation <= 2**63 - 1
        ):
            raise ValueError("invalid configured file TTS execution target")


def pcm16_mono_bytes(duration_ms: int, sample_rate_hz: int) -> int:
    """Exact upper bound in bytes for PCM16 mono audio of one declared duration.

    The engine clock and channel count are fixed by the approved binding, so a
    request that declares a shorter duration must bound the source PCM the same
    way instead of inheriting the binding's much larger ceiling.
    """
    if type(duration_ms) is not int or duration_ms < 0 or type(sample_rate_hz) is not int:
        raise ValueError("Invalid audio duration bound")
    return -((-duration_ms * sample_rate_hz) // 1000) * 2


@dataclass(frozen=True, slots=True)
class TtsPolicy:
    """Frozen per-binding synthesis policy.

    Only ``wav``/``pcm`` engine responses and a single 24 kHz mono PCM16 shape
    were verified against the recorded Qwen3-TTS CustomVoice binding. Other
    declared values are accepted so a binding can be configured explicitly, but
    an engine response that disagrees with the declaration fails instead of
    being relabelled.
    """

    config_revision: str
    voice: str
    task: TtsTask = TtsTask.CUSTOM_VOICE
    instructions: str = field(default="", repr=False)
    speed: float = 1.0
    engine_format: TtsEngineFormat = TtsEngineFormat.WAV
    sample_rate_hz: int = 24000
    language: str | None = None
    seed: int | None = None
    max_pcm_bytes: int = 8 * 1024 * 1024
    max_audio_ms: int = 600000
    timeout_seconds: float = 120.0

    def __post_init__(self) -> None:
        if (
            type(self.config_revision) is not str
            or _IDENTIFIER.fullmatch(self.config_revision) is None
            or type(self.voice) is not str
            or _IDENTIFIER.fullmatch(self.voice) is None
            or type(self.instructions) is not str
            or len(self.instructions) > MAX_TTS_INSTRUCTIONS_CHARS
            or "\x00" in self.instructions
            or type(self.speed) not in (int, float)
            or not math.isfinite(self.speed)
            or not 0.25 <= self.speed <= 4.0
            or not isinstance(self.task, TtsTask)
            or not isinstance(self.engine_format, TtsEngineFormat)
            or type(self.sample_rate_hz) is not int
            or self.sample_rate_hz not in ENGINE_SAMPLE_RATES
            or (
                self.language is not None
                and (type(self.language) is not str or _LANGUAGE.fullmatch(self.language) is None)
            )
            or (
                self.seed is not None
                and (type(self.seed) is not int or not -(2**63) <= self.seed < 2**63)
            )
            or type(self.max_pcm_bytes) is not int
            or not 1024 <= self.max_pcm_bytes <= 64 * 1024 * 1024
            or type(self.max_audio_ms) is not int
            or not 1 <= self.max_audio_ms <= 3600000
            or type(self.timeout_seconds) not in (int, float)
            or not math.isfinite(self.timeout_seconds)
            or not 0 < self.timeout_seconds <= 600
        ):
            raise ValueError("invalid frozen TTS policy")
        try:
            self.instructions.encode("utf-8")
        except UnicodeError:
            raise ValueError("invalid frozen TTS instructions encoding") from None

    @property
    def digest(self) -> str:
        return command_digest(
            {
                "recipe": TTS_RECIPE_VERSION,
                "config_revision": self.config_revision,
                "voice": self.voice,
                "task": self.task.value,
                "instructions": self.instructions,
                "speed": self.speed,
                "engine_format": self.engine_format.value,
                "sample_rate_hz": self.sample_rate_hz,
                "language": self.language,
                "seed": self.seed,
                "max_pcm_bytes": self.max_pcm_bytes,
                "max_audio_ms": self.max_audio_ms,
                "timeout_seconds": self.timeout_seconds,
            }
        )


@dataclass(frozen=True, slots=True)
class TtsRequest:
    """One frozen text input; longer text is an upstream chunking decision."""

    request_id: str
    text: str = field(repr=False)

    def __post_init__(self) -> None:
        if (
            type(self.request_id) is not str
            or _IDENTIFIER.fullmatch(self.request_id) is None
            or type(self.text) is not str
            or "\x00" in self.text
            or not 1 <= len(self.text) <= MAX_TTS_TEXT_CHARS
        ):
            raise ValueError("TTS requires an identity and bounded non-empty text")
        try:
            encoded = self.text.encode("utf-8")
        except UnicodeError:
            raise ValueError("invalid TTS text encoding") from None
        if len(encoded) > MAX_TTS_TEXT_BYTES:
            raise ValueError("TTS text exceeds the byte budget")


@dataclass(frozen=True, slots=True)
class TtsAudio:
    """Normalized engine output: mono PCM16LE plus the declared sample clock."""

    request_id: str
    engine_media_type: str
    pcm16: bytes = field(repr=False)
    sample_rate_hz: int
    channels: int
    policy_digest: str

    def __post_init__(self) -> None:
        if (
            type(self.request_id) is not str
            or _IDENTIFIER.fullmatch(self.request_id) is None
            or self.engine_media_type not in ("audio/wav", "audio/pcm")
            or type(self.pcm16) is not bytes
            or not self.pcm16
            or len(self.pcm16) % 2
            or type(self.sample_rate_hz) is not int
            or self.sample_rate_hz not in ENGINE_SAMPLE_RATES
            or self.channels != 1
            or type(self.policy_digest) is not str
            or not self.policy_digest
        ):
            raise ValueError("invalid normalized TTS audio")

    @property
    def duration_ms(self) -> int:
        return len(self.pcm16) // 2 * 1000 // self.sample_rate_hz


@dataclass(frozen=True, slots=True)
class TtsDelivery:
    """Bounded delivery payload derived from exactly one synthesized result."""

    format: TtsDeliveryFormat
    media_type: str
    payload: bytes = field(repr=False)
    sample_rate_hz: int
    channels: int
    duration_ms: int
    source_pcm_digest: str

    def __post_init__(self) -> None:
        if (
            not isinstance(self.format, TtsDeliveryFormat)
            or type(self.media_type) is not str
            or not self.media_type
            or type(self.payload) is not bytes
            or not self.payload
            or type(self.sample_rate_hz) is not int
            or self.sample_rate_hz not in ENGINE_SAMPLE_RATES
            or self.channels != 1
            or type(self.duration_ms) is not int
            or self.duration_ms < 1
            or type(self.source_pcm_digest) is not str
            or not self.source_pcm_digest
        ):
            raise ValueError("invalid TTS delivery payload")


class TtsSynthesizer(Protocol):
    """One engine call per admitted synthesis; no retry or resource-release authority."""

    @property
    def binding_revision_id(self) -> str: ...

    def validate_execution_target(self, target: TtsExecutionTarget) -> None:
        """Reject an unmatched node, generation or binding before any engine call."""
        ...

    async def synthesize(
        self,
        request: TtsRequest,
        policy: TtsPolicy,
        *,
        cancelled: Callable[[], bool],
    ) -> TtsAudio: ...


class TtsDeliveryEncoder(Protocol):
    """Bounded local encoding; never calls the engine or asserts release."""

    def encode(
        self,
        audio: TtsAudio,
        *,
        target: TtsDeliveryFormat,
        max_payload_bytes: int,
        cancelled: Callable[[], bool],
    ) -> TtsDelivery: ...
