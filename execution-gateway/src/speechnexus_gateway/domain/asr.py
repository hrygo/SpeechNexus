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
class AsrText:
    request_id: str
    engine_request_id: str
    text: str = field(repr=False)
    completion_tokens: int
    policy_digest: str


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
