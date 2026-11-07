"""Native VoiceDesign facts; a separate stage from the CustomVoice file-TTS lane.

The engine validates a checkpoint against exactly one ``task_type``, so a
VoiceDesign instance and a CustomVoice or Base instance cannot share a process
or switch task inside one endpoint. This module therefore owns its policy,
request, execution target and failure domain, while reusing the neutral bounded
PCM16 delivery types (``TtsAudio`` and the delivery encoder) that describe the
same 24 kHz mono audio clock.
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol

from speechnexus_gateway.domain.canonical import command_digest
from speechnexus_gateway.domain.file_execution import EngineRequestRejectedError
from speechnexus_gateway.domain.languages import ENGINE_LANGUAGES
from speechnexus_gateway.domain.tts import TtsAudio, TtsEngineFormat

VOICE_DESIGN_RECIPE_VERSION = "file-voice-design-1"
VOICE_DESIGN_SAMPLE_RATE_HZ = 24000
MAX_VOICE_DESIGN_DESCRIPTION_CHARS = 2000
MAX_VOICE_DESIGN_PREVIEW_CHARS = 2000
# The approved preview ceiling is one minute of 24 kHz mono PCM16.
MAX_VOICE_DESIGN_PCM_BYTES = 60 * 24000 * 2

_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")
_LANGUAGE = re.compile(r"(auto|[a-z]{2,3}(-[A-Za-z0-9]{2,8})*)")
_MAX_LANGUAGE_CHARS = 35
_TARGET_IDENTIFIER = re.compile(r"[a-z0-9][a-z0-9._:-]{1,126}[a-z0-9]")


class VoiceDesignFailure(StrEnum):
    CANCELLED = "voice_design_cancelled"
    DEADLINE_EXCEEDED = "voice_design_deadline_exceeded"
    TRANSPORT_UNKNOWN = "voice_design_transport_unknown"
    # A client-error answer is terminal for this request and never a timeout.
    ENGINE_REJECTED = "voice_design_engine_rejected"
    RESPONSE_INVALID = "voice_design_response_invalid"
    OUTPUT_LIMIT_EXCEEDED = "voice_design_output_limit_exceeded"


class VoiceDesignCallError(RuntimeError):
    """One failed VoiceDesign call.

    ``request_may_have_run`` never claims the engine stopped. A client-error
    rejection is the only failure that positively reports ``False``; every
    timeout, transport failure and server error keeps it conservatively true.
    """

    def __init__(
        self,
        code: VoiceDesignFailure,
        *,
        request_may_have_run: bool,
    ) -> None:
        super().__init__(code.value)
        self.code = code
        self.request_may_have_run = request_may_have_run


class VoiceDesignRejectedError(VoiceDesignCallError, EngineRequestRejectedError):
    """A proven client-error refusal: terminal for this request, not a retry signal."""

    def __init__(self) -> None:
        super().__init__(VoiceDesignFailure.ENGINE_REJECTED, request_may_have_run=False)


class VoiceDesignTask(StrEnum):
    """The one engine task a VoiceDesign binding may serve."""

    VOICE_DESIGN = "VoiceDesign"


@dataclass(frozen=True, slots=True)
class VoiceDesignExecutionTarget:
    """The admitted VoiceDesign engine target, independent of transport types."""

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
            raise ValueError("invalid configured VoiceDesign execution target")


@dataclass(frozen=True, slots=True)
class VoiceDesignPolicy:
    """Frozen per-binding VoiceDesign policy; no voice or speaker exists yet."""

    config_revision: str
    task: VoiceDesignTask = VoiceDesignTask.VOICE_DESIGN
    engine_format: TtsEngineFormat = TtsEngineFormat.WAV
    sample_rate_hz: int = VOICE_DESIGN_SAMPLE_RATE_HZ
    max_pcm_bytes: int = MAX_VOICE_DESIGN_PCM_BYTES
    max_audio_ms: int = 60000
    timeout_seconds: float = 300.0
    supported_languages: tuple[str, ...] = tuple(ENGINE_LANGUAGES)

    def __post_init__(self) -> None:
        if (
            type(self.config_revision) is not str
            or _IDENTIFIER.fullmatch(self.config_revision) is None
            or self.task is not VoiceDesignTask.VOICE_DESIGN
            or self.engine_format is not TtsEngineFormat.WAV
            or self.sample_rate_hz != VOICE_DESIGN_SAMPLE_RATE_HZ
            or type(self.max_pcm_bytes) is not int
            or not 1024 <= self.max_pcm_bytes <= MAX_VOICE_DESIGN_PCM_BYTES
            or type(self.max_audio_ms) is not int
            or not 1 <= self.max_audio_ms <= 600000
            or type(self.timeout_seconds) not in (int, float)
            or not math.isfinite(self.timeout_seconds)
            or not 0 < self.timeout_seconds <= 600
            or type(self.supported_languages) is not tuple
            or not self.supported_languages
            or len(set(self.supported_languages)) != len(self.supported_languages)
            or any(tag not in ENGINE_LANGUAGES for tag in self.supported_languages)
        ):
            raise ValueError("invalid frozen VoiceDesign policy")
        if self.max_pcm_bytes > -((-self.max_audio_ms * self.sample_rate_hz) // 1000) * 2:
            raise ValueError("VoiceDesign policy PCM bound contradicts its audio ceiling")

    @property
    def digest(self) -> str:
        return command_digest(
            {
                "recipe": VOICE_DESIGN_RECIPE_VERSION,
                "config_revision": self.config_revision,
                "task": self.task.value,
                "engine_format": self.engine_format.value,
                "sample_rate_hz": self.sample_rate_hz,
                "max_pcm_bytes": self.max_pcm_bytes,
                "max_audio_ms": self.max_audio_ms,
                "timeout_seconds": self.timeout_seconds,
                "supported_languages": list(self.supported_languages),
            }
        )


@dataclass(frozen=True, slots=True)
class VoiceDesignRequest:
    """One frozen design request: the voice description plus its preview text."""

    request_id: str
    description: str = field(repr=False)
    preview_text: str = field(repr=False)
    language: str
    seed: int | None = None

    def __post_init__(self) -> None:
        if (
            type(self.request_id) is not str
            or _IDENTIFIER.fullmatch(self.request_id) is None
            or type(self.description) is not str
            or "\x00" in self.description
            or not 1 <= len(self.description) <= MAX_VOICE_DESIGN_DESCRIPTION_CHARS
            or type(self.preview_text) is not str
            or "\x00" in self.preview_text
            or not 1 <= len(self.preview_text) <= MAX_VOICE_DESIGN_PREVIEW_CHARS
            or type(self.language) is not str
            or len(self.language) > _MAX_LANGUAGE_CHARS
            or _LANGUAGE.fullmatch(self.language) is None
            or (
                self.seed is not None
                and (type(self.seed) is not int or not -(2**63) <= self.seed < 2**63)
            )
        ):
            raise ValueError("invalid frozen VoiceDesign request")
        try:
            self.description.encode("utf-8")
            self.preview_text.encode("utf-8")
        except UnicodeError:
            raise ValueError("invalid VoiceDesign request encoding") from None


class VoiceDesignSynthesizer(Protocol):
    """One engine call per admitted design; no retry and no release authority."""

    @property
    def binding_revision_id(self) -> str: ...

    def validate_execution_target(self, target: VoiceDesignExecutionTarget) -> None:
        """Reject an unmatched node, generation or binding before any engine call."""
        ...

    async def synthesize(
        self,
        request: VoiceDesignRequest,
        policy: VoiceDesignPolicy,
        *,
        cancelled: Callable[[], bool],
    ) -> TtsAudio: ...

    async def warmup(self) -> None:
        """Issue one real bounded synthesis; an HTTP 200 health answer is not readiness."""
        ...
