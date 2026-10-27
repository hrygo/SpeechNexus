"""Resolve one public speech request from published facts, then synthesize once.

Nothing here chooses a default model or voice revision: the binding and the
voice must already be published, and an absent fact is an explicit failure. The
request ceiling is applied to the policy the synthesizer receives, so a shorter
request cannot inherit the deployment's larger engine budget.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from speechnexus_backend.speech.domain.publications import Publication, PublicationKind
from speechnexus_backend.speech.domain.speech_synthesis import (
    MAX_AUDIO_MS,
    MAX_PAYLOAD_BYTES,
    MAX_SPEECH_INSTRUCTIONS_CHARS,
    MIN_AUDIO_MS,
    MIN_PAYLOAD_BYTES,
    SpeechAudio,
    SpeechCommand,
    SpeechFormat,
    SpeechPlan,
    correlation_identifier,
    identifier,
    pcm16_mono_bytes,
)

DEFAULT_WORKSPACE = "workspace-default"
REQUIRED_TASK = "tts_file"
REST_SAMPLE_RATE_HZ = 24000
REST_CHANNELS = 1
DEFAULT_PLANNER_CHARS = 400
DEFAULT_PLANNER_WAIT_MS = 500
_WAV_HEADER_BYTES = 44
# 64 kbit/s MP3 is roughly one sixth of 24 kHz PCM16; a quarter keeps the bound
# conservative without letting an encoder expand beyond the declared ceiling.
_MP3_DIVISOR = 4
_MP3_HEADROOM_BYTES = 8192


class SpeechUnavailableError(RuntimeError):
    """The deployment cannot execute a synthesis at all (no adapter or storage)."""


class SpeechModelNotFoundError(LookupError):
    """The requested logical model has no published binding."""


class SpeechUnsupportedError(ValueError):
    """The published binding cannot honour a contract-valid request value."""

    def __init__(self, message: str, *, param: str | None = None) -> None:
        super().__init__(message)
        self.param = param


class SpeechVoiceNotFoundError(LookupError):
    """The requested voice, or its latest revision, is not published."""


class SpeechProfileNotFoundError(LookupError):
    """The requested profile revision is not published."""


class SpeechPronunciationNotFoundError(LookupError):
    """The requested pronunciation revision does not exist."""


class SpeechWorkspaceNotFoundError(LookupError):
    """The requested workspace has no published policy."""


class SpeechWorkspaceDisabledError(ValueError):
    """The workspace exists but is not active."""


class SpeechEngineError(RuntimeError):
    """The engine call failed; no partial audio may be returned."""


class SpeechDeadlineExceededError(TimeoutError):
    """The request's own deadline elapsed before complete audio existed."""


class SpeechOutputLimitExceededError(RuntimeError):
    """The synthesizer returned more audio than this request allowed."""


class SpeechOutputInvalidError(RuntimeError):
    """The synthesizer returned audio that disagrees with the fixed plan."""


@dataclass(frozen=True, slots=True)
class SpeechLimits:
    """Deployment policy for one synchronous REST synthesis."""

    max_output_audio_ms: int = 120_000
    max_payload_bytes: int = 8 * 1024 * 1024
    max_instructions_chars: int = MAX_SPEECH_INSTRUCTIONS_CHARS
    timeout_seconds: float = 120.0

    def __post_init__(self) -> None:
        if (
            type(self.max_output_audio_ms) is not int
            or not MIN_AUDIO_MS <= self.max_output_audio_ms <= MAX_AUDIO_MS
        ):
            raise ValueError("Speech duration limit must be bounded")
        if (
            type(self.max_payload_bytes) is not int
            or not MIN_PAYLOAD_BYTES <= self.max_payload_bytes <= MAX_PAYLOAD_BYTES
        ):
            raise ValueError("Speech payload limit must be bounded")
        if (
            type(self.max_instructions_chars) is not int
            or not 1 <= self.max_instructions_chars <= MAX_SPEECH_INSTRUCTIONS_CHARS
        ):
            raise ValueError("Speech instruction limit must be bounded")
        if (
            type(self.timeout_seconds) not in (int, float)
            or not math.isfinite(self.timeout_seconds)
            or not 0 < self.timeout_seconds <= 600
        ):
            raise ValueError("Speech timeout must be bounded")


@dataclass(frozen=True, slots=True)
class WorkspaceFact:
    """The published workspace policy revision a request executes under."""

    policy_revision: int
    active: bool


@dataclass(frozen=True, slots=True)
class BindingFact:
    """One published binding revision plus its immutable definition document."""

    publication: Publication
    definition: Mapping[str, Any]


class SpeechFacts(Protocol):
    """Published-only inputs; every absence is explicit and never a default."""

    def workspace(self, workspace_id: str) -> WorkspaceFact | None: ...

    def latest_binding(self, model_id: str) -> BindingFact | None: ...

    def latest_publication(self, kind: PublicationKind, object_id: str) -> Publication | None: ...

    def publication(
        self, kind: PublicationKind, object_id: str, revision: int
    ) -> Publication | None: ...

    def pronunciation_exists(self, object_id: str, revision: int) -> bool: ...


class SpeechSynthesizer(Protocol):
    """One bounded engine synthesis per plan; no admission or release authority."""

    def synthesize(self, plan: SpeechPlan) -> SpeechAudio: ...


class SpeechSynthesis:
    """One explicit resolution, one synthesis, one complete or failed result."""

    def __init__(
        self,
        facts: SpeechFacts,
        synthesizer: SpeechSynthesizer,
        limits: SpeechLimits | None = None,
    ) -> None:
        self._facts = facts
        self._synthesizer = synthesizer
        self._limits = limits or SpeechLimits()

    @property
    def limits(self) -> SpeechLimits:
        return self._limits

    def synthesize(
        self,
        command: SpeechCommand,
        *,
        request_id: str,
        max_output_audio_ms: int | None = None,
    ) -> SpeechAudio:
        """Resolve, tighten and execute one request; partial audio never escapes.

        ``max_output_audio_ms`` is the request's own ceiling. The frozen
        ``SpeechRequest`` schema has no field for it yet, so the HTTP boundary
        passes the deployment ceiling; a later contract field must be folded in
        here with ``min`` and never replace the deployment ceiling.
        """
        workspace = self._facts.workspace(identifier(command.workspace_id))
        if workspace is None:
            raise SpeechWorkspaceNotFoundError
        if not workspace.active:
            raise SpeechWorkspaceDisabledError

        binding = self._facts.latest_binding(command.model)
        if binding is None:
            raise SpeechModelNotFoundError
        tasks = binding.definition.get("tasks")
        if not isinstance(tasks, list) or REQUIRED_TASK not in tasks:
            raise SpeechUnsupportedError("The model does not publish file TTS.", param="model")
        domain = _tts_domain(binding.definition)
        _require_domain(domain, command)

        voice = self._voice(command)
        if command.profile_id is not None:
            assert command.profile_revision is not None
            if (
                self._facts.publication(
                    "profile", identifier(command.profile_id), command.profile_revision
                )
                is None
            ):
                raise SpeechProfileNotFoundError
        if command.pronunciation_revision is not None:
            # The frozen REST subset carries a revision without a set identity,
            # so it cannot name a published pronunciation. Refuse instead of
            # applying the revision to an implied default set.
            raise SpeechUnsupportedError(
                "The REST subset cannot name a pronunciation set.",
                param="nexus.pronunciation_revision",
            )

        plan = self._plan(
            command,
            request_id=request_id,
            voice=voice,
            max_output_audio_ms=max_output_audio_ms,
        )
        audio = self._synthesizer.synthesize(plan)
        _conform(plan, audio)
        return audio

    def _voice(self, command: SpeechCommand) -> Publication:
        if command.voice_revision is not None:
            publication = self._facts.publication(
                "voice", _voice_identifier(command), command.voice_revision
            )
        else:
            publication = self._facts.latest_publication("voice", _voice_identifier(command))
        if publication is None:
            raise SpeechVoiceNotFoundError
        return publication

    def _plan(
        self,
        command: SpeechCommand,
        *,
        request_id: str,
        voice: Publication,
        max_output_audio_ms: int | None,
    ) -> SpeechPlan:
        ceiling = self._limits.max_output_audio_ms
        if max_output_audio_ms is not None:
            if type(max_output_audio_ms) is not int or max_output_audio_ms < MIN_AUDIO_MS:
                raise SpeechUnsupportedError(
                    "The request audio ceiling is below one frame.", param="max_output_audio_ms"
                )
            ceiling = min(ceiling, max_output_audio_ms)
        source_pcm = pcm16_mono_bytes(ceiling, REST_SAMPLE_RATE_HZ)
        payload = min(
            self._limits.max_payload_bytes,
            _delivery_bytes(command.response_format, source_pcm),
        )
        return SpeechPlan(
            request_id=correlation_identifier(request_id),
            text=command.text,
            voice_id=voice.object_id,
            voice_revision_id=voice.revision_id,
            response_format=command.response_format,
            sample_rate_hz=REST_SAMPLE_RATE_HZ,
            channels=REST_CHANNELS,
            speed=float(command.speed),
            instructions=command.instructions,
            language=command.language,
            seed=command.seed,
            planner_max_chars=command.planner_max_chars or DEFAULT_PLANNER_CHARS,
            planner_max_wait_ms=command.planner_max_wait_ms or DEFAULT_PLANNER_WAIT_MS,
            max_audio_ms=ceiling,
            max_source_pcm_bytes=source_pcm,
            max_payload_bytes=payload,
            timeout_seconds=self._timeout(command.deadline_ms),
        )

    def _timeout(self, deadline_ms: int | None) -> float:
        limit = float(self._limits.timeout_seconds)
        if deadline_ms is None:
            return limit
        return min(limit, deadline_ms / 1000)


def _delivery_bytes(response_format: SpeechFormat, source_pcm_bytes: int) -> int:
    if response_format is SpeechFormat.PCM:
        return source_pcm_bytes
    if response_format is SpeechFormat.WAV:
        return source_pcm_bytes + _WAV_HEADER_BYTES
    return source_pcm_bytes // _MP3_DIVISOR + _MP3_HEADROOM_BYTES


def _conform(plan: SpeechPlan, audio: SpeechAudio) -> None:
    if (
        audio.response_format is not plan.response_format
        or audio.sample_rate_hz != plan.sample_rate_hz
        or audio.channels != plan.channels
    ):
        raise SpeechOutputInvalidError
    if audio.duration_ms > plan.max_audio_ms or len(audio.payload) > plan.max_payload_bytes:
        raise SpeechOutputLimitExceededError


def _tts_domain(definition: Mapping[str, Any]) -> Mapping[str, Any]:
    domains = definition.get("parameter_domains")
    if not isinstance(domains, Mapping):
        raise SpeechUnsupportedError("The model declares no parameter domain.", param="model")
    domain = domains.get("tts")
    if not isinstance(domain, Mapping):
        raise SpeechUnsupportedError(
            "The model declares no speech parameter domain.", param="model"
        )
    return domain


def _require_domain(domain: Mapping[str, Any], command: SpeechCommand) -> None:
    formats = domain.get("audio_formats")
    if not isinstance(formats, list) or command.response_format.value not in formats:
        raise SpeechUnsupportedError(
            "The binding does not publish this audio format.", param="response_format"
        )
    rates = domain.get("sample_rates_hz")
    if not isinstance(rates, list) or REST_SAMPLE_RATE_HZ not in rates:
        raise SpeechUnsupportedError(
            "The binding does not publish the REST sample rate.", param="model"
        )
    modes = domain.get("text_commit_modes")
    if not isinstance(modes, list) or "explicit" not in modes:
        raise SpeechUnsupportedError(
            "The binding does not support explicit REST text.",
            param="nexus.overrides.tts.text_commit_mode",
        )
    delivery = domain.get("audio_delivery_modes")
    if not isinstance(delivery, list) or "complete" not in delivery:
        raise SpeechUnsupportedError(
            "The binding does not support complete audio delivery.",
            param="nexus.overrides.tts.audio_delivery",
        )
    limit = domain.get("max_input_codepoints")
    if type(limit) is not int or len(command.text) > limit:
        raise SpeechUnsupportedError("The input exceeds the binding text domain.", param="input")
    if command.instructions and domain.get("supports_instructions") is not True:
        raise SpeechUnsupportedError(
            "The binding does not support instructions.", param="instructions"
        )
    if float(command.speed) != 1.0:
        if domain.get("supports_speed") is not True:
            raise SpeechUnsupportedError(
                "The binding does not support speed control.", param="speed"
            )
        speed_range = domain.get("speed_range")
        if not isinstance(speed_range, Mapping):
            raise SpeechUnsupportedError("The binding publishes no speed range.", param="speed")
        minimum, maximum = speed_range.get("minimum"), speed_range.get("maximum")
        if not isinstance(minimum, (int, float)) or not isinstance(maximum, (int, float)):
            raise SpeechUnsupportedError("The binding publishes no speed range.", param="speed")
        if not float(minimum) <= float(command.speed) <= float(maximum):
            raise SpeechUnsupportedError("The speed is outside the binding range.", param="speed")
    if command.language is not None:
        languages = domain.get("languages")
        if not isinstance(languages, list) or command.language not in languages:
            raise SpeechUnsupportedError(
                "The binding does not publish this language.",
                param="nexus.overrides.tts.language",
            )
    if command.seed is not None and domain.get("supports_seed") is not True:
        raise SpeechUnsupportedError(
            "The binding does not support a seed.", param="nexus.overrides.tts.seed"
        )


def _voice_identifier(command: SpeechCommand) -> str:
    try:
        return identifier(command.voice)
    except ValueError:
        raise SpeechVoiceNotFoundError from None


__all__ = [
    "DEFAULT_WORKSPACE",
    "SpeechDeadlineExceededError",
    "SpeechEngineError",
    "SpeechFacts",
    "SpeechLimits",
    "SpeechModelNotFoundError",
    "SpeechOutputInvalidError",
    "SpeechOutputLimitExceededError",
    "SpeechPlan",
    "SpeechProfileNotFoundError",
    "SpeechPronunciationNotFoundError",
    "SpeechSynthesis",
    "SpeechSynthesizer",
    "SpeechUnavailableError",
    "SpeechUnsupportedError",
    "SpeechVoiceNotFoundError",
    "SpeechWorkspaceDisabledError",
    "SpeechWorkspaceNotFoundError",
]
