"""Native bounded ASR calls; no vendor DTOs, network addresses or stop assertions."""

from __future__ import annotations

import math
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol

from speechnexus_gateway.domain.canonical import command_digest

ASR_RECIPE_VERSION = "file-asr-1"
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")
# Opaque configuration identities also admit colons; engine request IDs do not.
_CONFIG_REVISION = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}")
_TARGET_IDENTIFIER = re.compile(r"[a-z0-9][a-z0-9._:-]{1,126}[a-z0-9]")
_LANGUAGE = re.compile(r"(auto|[a-z]{2,3}(-[A-Za-z0-9]{2,8})*)")
_RESULT_LANGUAGE = re.compile(r"[a-z]{2,3}(-[A-Za-z0-9]{2,8})*")
_MAX_RESULT_LANGUAGE_LENGTH = 35


@dataclass(frozen=True, slots=True)
class AsrExecutionTarget:
    """The admitted engine target, independent of transport and vendor types."""

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
            or self.pool_id != "asr-batch"
            or type(self.instance_generation) is not int
            or not 1 <= self.instance_generation <= 2**63 - 1
        ):
            raise ValueError("invalid configured file ASR execution target")


class AsrFailure(StrEnum):
    CANCELLED = "asr_cancelled"
    DEADLINE_EXCEEDED = "asr_deadline_exceeded"
    TRANSPORT_UNKNOWN = "asr_transport_unknown"
    RESPONSE_INVALID = "asr_response_invalid"
    OUTPUT_TRUNCATED = "asr_output_truncated"
    OUTPUT_LIMIT_EXCEEDED = "asr_output_limit_exceeded"


class AsrCallError(RuntimeError):
    def __init__(
        self,
        code: AsrFailure,
        *,
        request_may_have_run: bool,
        completion_tokens: int | None = None,
    ) -> None:
        super().__init__(code.value)
        self.code = code
        self.request_may_have_run = request_may_have_run
        self.completion_tokens = completion_tokens


@dataclass(frozen=True, slots=True)
class AsrPolicy:
    config_revision: str
    language: str = "auto"
    prompt: str = field(default="", repr=False)
    temperature: float = 0.0
    max_completion_tokens: int = 1024
    timeout_seconds: float = 60.0

    def __post_init__(self) -> None:
        if (
            type(self.config_revision) is not str
            or _CONFIG_REVISION.fullmatch(self.config_revision) is None
            or type(self.language) is not str
            or len(self.language) > 35
            or _LANGUAGE.fullmatch(self.language) is None
            or type(self.prompt) is not str
            or len(self.prompt) > 10000
            or "\x00" in self.prompt
            or type(self.temperature) not in (int, float)
            or not math.isfinite(self.temperature)
            or not 0 <= self.temperature <= 1
            or type(self.max_completion_tokens) is not int
            or not 1 <= self.max_completion_tokens <= 4096
            or type(self.timeout_seconds) not in (int, float)
            or not math.isfinite(self.timeout_seconds)
            or not 0 < self.timeout_seconds <= 300
        ):
            raise ValueError("invalid frozen ASR policy")
        try:
            self.prompt.encode("utf-8")
        except UnicodeError:
            raise ValueError("invalid frozen ASR prompt encoding") from None

    @property
    def digest(self) -> str:
        return command_digest(
            {
                "recipe": ASR_RECIPE_VERSION,
                "config_revision": self.config_revision,
                "language": self.language,
                "prompt": self.prompt,
                "temperature": self.temperature,
                "max_completion_tokens": self.max_completion_tokens,
                "timeout_seconds": self.timeout_seconds,
            }
        )


@dataclass(frozen=True, slots=True)
class AsrAudio:
    request_id: str
    pcm16: bytes = field(repr=False)

    def __post_init__(self) -> None:
        if (
            type(self.request_id) is not str
            or _IDENTIFIER.fullmatch(self.request_id) is None
            or type(self.pcm16) is not bytes
            or not 0 < len(self.pcm16) <= 16000 * 30 * 2
            or len(self.pcm16) % 2
        ):
            raise ValueError("ASR requires an identity and at most 30 s of mono PCM16LE at 16 kHz")


@dataclass(frozen=True, slots=True)
class AsrSegment:
    """Optional native segment evidence; no field is synthesized when absent."""

    start_ms: int
    end_ms: int
    text: str = field(repr=False)
    id: int | None = None
    avg_logprob: float | None = None
    compression_ratio: float | None = None
    no_speech_prob: float | None = None
    seek: int | None = None
    temperature: float | None = None
    tokens: tuple[int, ...] = ()

    def __post_init__(self) -> None:
        if (
            type(self.start_ms) is not int
            or type(self.end_ms) is not int
            or not 0 <= self.start_ms < self.end_ms
            or type(self.text) is not str
            or "\x00" in self.text
            or (self.id is not None and (type(self.id) is not int or self.id < 0))
            or (self.seek is not None and (type(self.seek) is not int or self.seek < 0))
            or type(self.tokens) is not tuple
            or any(type(token) is not int or token < 0 for token in self.tokens)
        ):
            raise ValueError("invalid ASR segment evidence")
        for value in (
            self.avg_logprob,
            self.compression_ratio,
            self.temperature,
        ):
            if value is not None and (type(value) not in (int, float) or not math.isfinite(value)):
                raise ValueError("invalid ASR segment score")
        if self.no_speech_prob is not None and (
            type(self.no_speech_prob) not in (int, float)
            or not math.isfinite(self.no_speech_prob)
            or not 0 <= self.no_speech_prob <= 1
        ):
            raise ValueError("invalid ASR segment speech probability")
        try:
            self.text.encode("utf-8")
        except UnicodeError:
            raise ValueError("invalid ASR segment encoding") from None


@dataclass(frozen=True, slots=True)
class AsrWord:
    """Optional native word boundary; the adapter never estimates one."""

    word: str = field(repr=False)
    start_ms: int
    end_ms: int

    def __post_init__(self) -> None:
        if (
            type(self.word) is not str
            or "\x00" in self.word
            or type(self.start_ms) is not int
            or type(self.end_ms) is not int
            or not 0 <= self.start_ms < self.end_ms
        ):
            raise ValueError("invalid ASR word evidence")
        try:
            self.word.encode("utf-8")
        except UnicodeError:
            raise ValueError("invalid ASR word encoding") from None


@dataclass(frozen=True, slots=True)
class AsrText:
    request_id: str
    engine_request_id: str
    text: str = field(repr=False)
    completion_tokens: int
    policy_digest: str
    language: str | None = None
    duration_ms: int | None = None
    segments: tuple[AsrSegment, ...] = ()
    words: tuple[AsrWord, ...] = ()

    def __post_init__(self) -> None:
        if self.language is not None and (
            type(self.language) is not str
            or len(self.language) > _MAX_RESULT_LANGUAGE_LENGTH
            or _RESULT_LANGUAGE.fullmatch(self.language) is None
        ):
            raise ValueError("invalid ASR language evidence")
        if self.duration_ms is not None and (
            type(self.duration_ms) is not int or not 0 < self.duration_ms <= 2**63 - 1
        ):
            raise ValueError("invalid ASR duration evidence")
        if type(self.segments) is not tuple or any(
            not isinstance(segment, AsrSegment) for segment in self.segments
        ):
            raise ValueError("invalid ASR segment list")
        previous_end = 0
        for segment in self.segments:
            if segment.start_ms < previous_end or (
                self.duration_ms is not None and segment.end_ms > self.duration_ms
            ):
                raise ValueError("unordered ASR segment evidence")
            previous_end = segment.end_ms
        if type(self.words) is not tuple or any(
            not isinstance(word, AsrWord) for word in self.words
        ):
            raise ValueError("invalid ASR word list")
        previous_start = 0
        for word in self.words:
            if word.start_ms < previous_start or (
                self.duration_ms is not None and word.end_ms > self.duration_ms
            ):
                raise ValueError("unordered ASR word evidence")
            previous_start = word.start_ms


class AsrRecognizer(Protocol):
    """One call per admitted segment. No retry, idempotency or resource-release authority."""

    @property
    def binding_revision_id(self) -> str: ...

    def validate_execution_target(self, target: AsrExecutionTarget) -> None:
        """Reject an unmatched generation before object/media I/O; no external I/O."""
        ...

    async def transcribe(
        self, audio: AsrAudio, policy: AsrPolicy, *, cancelled: Callable[[], bool]
    ) -> AsrText: ...
