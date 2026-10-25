"""Service-neutral, bounded text generation; no speech queue or transport DTOs."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Protocol

type LlmErrorCode = Literal[
    "disabled",
    "invalid_input",
    "profile_mismatch",
    "unavailable",
    "invalid_response",
    "source_unavailable",
    "source_changed",
    "source_not_sealed",
]


class LlmError(Exception):
    """Private stable failures; never carry provider text, URLs or credentials."""

    def __init__(self, code: LlmErrorCode) -> None:
        self.code = code
        super().__init__(f"workbench text LLM {code}")


def valid_text(value: str, maximum: int) -> bool:
    """Preserve accepted Unicode exactly; reject empty or unstorable content."""
    if not isinstance(value, str) or not 1 <= len(value) <= maximum or not value.strip():
        return False
    if "\x00" in value:
        return False
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


@dataclass(frozen=True, slots=True)
class LlmProfile:
    """Server-selected immutable model/template and generation budgets."""

    model: str
    template_revision: int
    system_prompt: str = field(repr=False)
    max_input_characters: int = 100_000
    max_output_characters: int = 20_000
    max_output_tokens: int = 2048

    def __post_init__(self) -> None:
        if (
            not valid_text(self.model, 128)
            or any(ord(character) < 32 or ord(character) == 127 for character in self.model)
            or not valid_text(self.system_prompt, 20_000)
            or type(self.template_revision) is not int
            or not 1 <= self.template_revision < 10**19
            or any(
                type(value) is not int or not 1 <= value <= 1_000_000
                for value in (
                    self.max_input_characters,
                    self.max_output_characters,
                )
            )
            or type(self.max_output_tokens) is not int
            or not 1 <= self.max_output_tokens <= 32_768
        ):
            raise ValueError("invalid text LLM profile")


@dataclass(frozen=True, slots=True)
class LlmRequest:
    profile: LlmProfile
    text: str = field(repr=False)

    def __post_init__(self) -> None:
        if not valid_text(self.text, self.profile.max_input_characters):
            raise LlmError("invalid_input")


@dataclass(frozen=True, slots=True)
class LlmUsage:
    """Provider-reported usage; absence is unknown, never a fabricated zero."""

    input_tokens: int
    output_tokens: int

    def __post_init__(self) -> None:
        if any(
            type(value) is not int or not 0 <= value <= 1_000_000_000
            for value in (
                self.input_tokens,
                self.output_tokens,
            )
        ):
            raise LlmError("invalid_response")


@dataclass(frozen=True, slots=True)
class LlmCompletion:
    """Untrusted plain text candidate, never a tool invocation or executable value."""

    profile: LlmProfile
    text: str = field(repr=False)
    usage: LlmUsage | None = None

    def __post_init__(self) -> None:
        if not valid_text(self.text, self.profile.max_output_characters):
            raise LlmError("invalid_response")


class TextLlm(Protocol):
    @property
    def binding_id(self) -> str | None:
        """Opaque server binding fingerprint; pin retries to the same service/profile."""
        ...

    @property
    def profile(self) -> LlmProfile | None:
        """None means disabled; a configured binding is not deployment qualification."""
        ...

    async def generate(self, request: LlmRequest) -> LlmCompletion:
        """One bounded attempt; propagate cancellation, never switch service or model."""
        ...


class DisabledTextLlm:
    """Default optional dependency, independent of basic workbench and speech."""

    @property
    def binding_id(self) -> None:
        return None

    @property
    def profile(self) -> None:
        return None

    async def generate(self, request: LlmRequest) -> LlmCompletion:
        raise LlmError("disabled")
