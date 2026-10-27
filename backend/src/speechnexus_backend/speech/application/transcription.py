"""Resolve one public transcription from published facts, then transcribe once.

Nothing here chooses a default model: the binding must already be published and
the workspace must be active, and an absent fact is an explicit failure. The
media ceilings are applied to the policy the executor receives, so a shorter
request cannot inherit the deployment's larger engine budget and an overrun
fails the whole request instead of truncating audio or text.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from speechnexus_backend.speech.application.public_jobs import BindingFact, WorkspaceFact
from speechnexus_backend.speech.domain.publications import Publication, PublicationKind
from speechnexus_backend.speech.domain.transcription import (
    MAX_TRANSCRIPTION_AUDIO_MS,
    MAX_TRANSCRIPTION_PROMPT_CHARS,
    MAX_TRANSCRIPTION_UPLOAD_BYTES,
    MIN_TRANSCRIPTION_AUDIO_MS,
    MIN_TRANSCRIPTION_UPLOAD_BYTES,
    AudioFacts,
    TranscriptionCommand,
    TranscriptionPlan,
    TranscriptionResult,
    identifier,
)

DEFAULT_WORKSPACE = "workspace-default"
REQUIRED_TASK = "asr_file"
DEFAULT_MAX_SEGMENT_MS = 30_000


class TranscriptionUnavailableError(RuntimeError):
    """The deployment cannot execute a transcription at all."""


class TranscriptionOutcomeUnknownError(TranscriptionUnavailableError):
    """Transport ambiguity after submission; the original identity must be queried."""


class TranscriptionModelNotFoundError(LookupError):
    """The requested logical model has no published binding."""


class TranscriptionUnsupportedError(ValueError):
    """The published binding cannot honour a contract-valid request value."""

    def __init__(self, message: str, *, param: str | None = None) -> None:
        super().__init__(message)
        self.param = param


class TranscriptionProfileNotFoundError(LookupError):
    """The requested profile revision is not published."""


class TranscriptionWorkspaceNotFoundError(LookupError):
    """The requested workspace has no published policy."""


class TranscriptionWorkspaceDisabledError(ValueError):
    """The workspace exists but is not active."""


class TranscriptionAudioUnsupportedError(ValueError):
    """The uploaded bytes are not a supported audio container or codec."""


class TranscriptionMediaTooLargeError(ValueError):
    """The uploaded bytes exceed the effective upload ceiling."""


class TranscriptionAudioTooLongError(ValueError):
    """The decoded audio exceeds the effective synchronous duration ceiling."""


class TranscriptionEngineError(RuntimeError):
    """The engine call failed; no partial text may be returned."""


class TranscriptionDeadlineExceededError(TimeoutError):
    """The request's own deadline elapsed before a complete transcript existed."""


class TranscriptionOutputLimitExceededError(RuntimeError):
    """The engine reported evidence beyond the request's media ceiling."""


class TranscriptionOutputInvalidError(RuntimeError):
    """The engine returned a transcript that disagrees with the frozen plan."""


@dataclass(frozen=True, slots=True)
class TranscriptionLimits:
    """Deployment policy for one synchronous REST transcription."""

    max_audio_ms: int = 120_000
    max_upload_bytes: int = 25 * 1024 * 1024
    max_prompt_chars: int = MAX_TRANSCRIPTION_PROMPT_CHARS
    timeout_seconds: float = 120.0

    def __post_init__(self) -> None:
        if (
            type(self.max_audio_ms) is not int
            or not MIN_TRANSCRIPTION_AUDIO_MS <= self.max_audio_ms <= MAX_TRANSCRIPTION_AUDIO_MS
        ):
            raise ValueError("Transcription duration limit must be bounded")
        if (
            type(self.max_upload_bytes) is not int
            or not MIN_TRANSCRIPTION_UPLOAD_BYTES
            <= self.max_upload_bytes
            <= MAX_TRANSCRIPTION_UPLOAD_BYTES
        ):
            raise ValueError("Transcription upload limit must be bounded")
        if (
            type(self.max_prompt_chars) is not int
            or not 1 <= self.max_prompt_chars <= MAX_TRANSCRIPTION_PROMPT_CHARS
        ):
            raise ValueError("Transcription prompt limit must be bounded")
        if (
            type(self.timeout_seconds) not in (int, float)
            or not math.isfinite(self.timeout_seconds)
            or not 0 < self.timeout_seconds <= 600
        ):
            raise ValueError("Transcription timeout must be bounded")


class TranscriptionFacts(Protocol):
    """Published-only inputs; every absence is explicit and never a default."""

    def workspace(self, workspace_id: str) -> WorkspaceFact | None: ...

    def latest_binding(self, model_id: str) -> BindingFact | None: ...

    def publication(
        self, kind: PublicationKind, object_id: str, revision: int
    ) -> Publication | None: ...


class AudioProbe(Protocol):
    """Bounded inspect of uploaded bytes; no engine call and no stored fact."""

    def inspect(self, audio: bytes, *, max_audio_ms: int, max_upload_bytes: int) -> AudioFacts: ...


class Transcriber(Protocol):
    """One bounded engine transcription per plan; no admission or release authority."""

    def transcribe(self, plan: TranscriptionPlan) -> TranscriptionResult: ...


@dataclass(frozen=True, slots=True)
class StagedAudio:
    """One immutable private audio object the gateway may read."""

    asset_id: str
    workspace_id: str
    bucket: str
    object_key: str
    content_sha256: str
    byte_length: int

    def __post_init__(self) -> None:
        for value in (self.asset_id, self.workspace_id):
            identifier(value)
        if type(self.bucket) is not str or not self.bucket:
            raise ValueError("Invalid staged audio bucket")
        if type(self.object_key) is not str or not self.object_key or "\x00" in self.object_key:
            raise ValueError("Invalid staged audio object key")
        if (
            type(self.content_sha256) is not str
            or len(self.content_sha256) != 71
            or not self.content_sha256.startswith("sha256:")
        ):
            raise ValueError("Invalid staged audio digest")
        if type(self.byte_length) is not int or self.byte_length < 1:
            raise ValueError("Invalid staged audio length")


@dataclass(frozen=True, slots=True)
class AdmittedExecution:
    """The complete coordination identity a gateway submit must carry."""

    submit_intent_id: str
    lease_id: str
    operation_id: str
    attempt_id: str
    stage_id: str
    workspace_id: str
    pool_id: str
    host_node_id: str
    gateway_id: str
    instance_id: str
    instance_generation: int
    binding_revision_id: str
    policy_revision_id: str
    fencing_token: int
    deadline_at_us: int

    def __post_init__(self) -> None:
        for value in (
            self.submit_intent_id,
            self.lease_id,
            self.operation_id,
            self.attempt_id,
            self.stage_id,
            self.workspace_id,
            self.pool_id,
            self.host_node_id,
            self.gateway_id,
            self.instance_id,
            self.binding_revision_id,
            self.policy_revision_id,
        ):
            identifier(value)
        for counter in (self.instance_generation, self.fencing_token):
            if type(counter) is not int or counter < 1:
                raise ValueError("Invalid admitted execution counter")
        if type(self.deadline_at_us) is not int or self.deadline_at_us < 1:
            raise ValueError("Invalid admitted execution deadline")


class TranscriptionInputObjects(Protocol):
    """Upload one immutable, versioned private input for the gateway."""

    def put(self, plan: TranscriptionPlan) -> StagedAudio: ...


class TranscriptionAdmission(Protocol):
    """Reserve the complete execution identity before any submit is sent."""

    def admit(
        self, plan: TranscriptionPlan, *, submit_intent_id: str, audio: StagedAudio
    ) -> AdmittedExecution: ...


class Transcription:
    """One explicit resolution, one transcription, one complete or failed result."""

    def __init__(
        self,
        facts: TranscriptionFacts,
        probe: AudioProbe,
        transcriber: Transcriber,
        limits: TranscriptionLimits | None = None,
    ) -> None:
        self._facts = facts
        self._probe = probe
        self._transcriber = transcriber
        self._limits = limits or TranscriptionLimits()

    @property
    def limits(self) -> TranscriptionLimits:
        return self._limits

    def close(self) -> None:
        """Release an injected long-lived transcriber, if it owns resources."""
        closer = getattr(self._transcriber, "close", None)
        if callable(closer):
            closer()

    def transcribe(
        self,
        command: TranscriptionCommand,
        *,
        request_id: str,
    ) -> TranscriptionResult:
        """Resolve, bound and execute one request; partial text never escapes."""
        workspace = self._facts.workspace(identifier(command.workspace_id))
        if workspace is None:
            raise TranscriptionWorkspaceNotFoundError
        if not workspace.active:
            raise TranscriptionWorkspaceDisabledError

        binding = self._facts.latest_binding(command.model)
        if binding is None:
            raise TranscriptionModelNotFoundError
        tasks = binding.definition.get("tasks")
        if not isinstance(tasks, list) or REQUIRED_TASK not in tasks:
            raise TranscriptionUnsupportedError(
                "The model does not publish file ASR.", param="model"
            )
        domain = _asr_domain(binding.definition)
        _require_domain(domain, command, self._limits)
        if command.profile_id is not None:
            assert command.profile_revision is not None
            if (
                self._facts.publication(
                    "profile", identifier(command.profile_id), command.profile_revision
                )
                is None
            ):
                raise TranscriptionProfileNotFoundError

        ceiling_ms, ceiling_bytes = self._ceilings(command)
        if len(command.audio) > ceiling_bytes:
            raise TranscriptionMediaTooLargeError
        facts = self._probe.inspect(
            command.audio, max_audio_ms=ceiling_ms, max_upload_bytes=ceiling_bytes
        )
        if facts.duration_ms > ceiling_ms:
            raise TranscriptionAudioTooLongError

        plan = TranscriptionPlan(
            request_id=identifier(request_id),
            workspace_id=identifier(command.workspace_id),
            model=command.model,
            binding_publication_id=binding.publication.revision_id,
            audio=command.audio,
            audio_media_type=facts.media_type,
            audio_sample_rate_hz=facts.sample_rate_hz,
            audio_channels=facts.channels,
            response_format=command.response_format,
            language=command.language or "auto",
            prompt=command.prompt,
            timestamp_granularities=command.timestamp_granularities,
            max_audio_ms=ceiling_ms,
            max_upload_bytes=ceiling_bytes,
            max_segment_ms=min(_segment_ceiling(domain), command.max_segment_ms or 30_000),
            require_segments=command.response_format.needs_segments
            or "segment" in command.timestamp_granularities,
            require_words="word" in command.timestamp_granularities,
            timeout_seconds=self._timeout(command.deadline_ms),
        )
        result = self._transcriber.transcribe(plan)
        _conform(plan, result)
        return result

    def _ceilings(self, command: TranscriptionCommand) -> tuple[int, int]:
        duration = self._limits.max_audio_ms
        if command.max_audio_ms is not None:
            duration = min(duration, command.max_audio_ms)
        upload = self._limits.max_upload_bytes
        if command.max_upload_bytes is not None:
            upload = min(upload, command.max_upload_bytes)
        return duration, upload

    def _timeout(self, deadline_ms: int | None) -> float:
        limit = float(self._limits.timeout_seconds)
        if deadline_ms is None:
            return limit
        return min(limit, deadline_ms / 1000)


def _conform(plan: TranscriptionPlan, result: TranscriptionResult) -> None:
    if result.duration_ms is not None and result.duration_ms > plan.max_audio_ms:
        raise TranscriptionOutputLimitExceededError
    if plan.require_segments and not result.segments:
        raise TranscriptionUnsupportedError(
            "The binding supplied no segment evidence.", param="response_format"
        )
    if plan.require_words and not result.words:
        raise TranscriptionUnsupportedError(
            "The binding supplied no word evidence.", param="timestamp_granularities[]"
        )
    previous = 0
    for segment in result.segments:
        if (
            segment.start_ms < previous
            or segment.end_ms > plan.max_audio_ms
            or segment.end_ms - segment.start_ms > plan.max_segment_ms
        ):
            raise TranscriptionOutputInvalidError
        previous = segment.end_ms
    for word in result.words:
        if word.end_ms > plan.max_audio_ms:
            raise TranscriptionOutputInvalidError


def _asr_domain(definition: Mapping[str, Any]) -> Mapping[str, Any]:
    domains = definition.get("parameter_domains")
    if not isinstance(domains, Mapping):
        raise TranscriptionUnsupportedError(
            "The model declares no parameter domain.", param="model"
        )
    domain = domains.get("asr")
    if not isinstance(domain, Mapping):
        raise TranscriptionUnsupportedError(
            "The model declares no ASR parameter domain.", param="model"
        )
    return domain


def _require_domain(
    domain: Mapping[str, Any], command: TranscriptionCommand, limits: TranscriptionLimits
) -> None:
    languages = domain.get("languages")
    if not isinstance(languages, list) or not all(isinstance(item, str) for item in languages):
        raise TranscriptionUnsupportedError(
            "The binding publishes no language domain.", param="model"
        )
    if command.language is not None and command.language not in languages:
        raise TranscriptionUnsupportedError(
            "The binding does not publish this language.", param="language"
        )
    if command.prompt:
        if len(command.prompt) > limits.max_prompt_chars:
            raise TranscriptionUnsupportedError(
                "The prompt exceeds the deployment prompt ceiling.", param="prompt"
            )
        if domain.get("supports_prompt") is not True:
            raise TranscriptionUnsupportedError(
                "The binding does not support a prompt.", param="prompt"
            )
    if (
        "word" in command.timestamp_granularities
        and domain.get("supports_word_timestamps") is not True
    ):
        raise TranscriptionUnsupportedError(
            "The binding does not publish word timestamps.",
            param="timestamp_granularities[]",
        )


def _segment_ceiling(domain: Mapping[str, Any]) -> int:
    value = domain.get("max_segment_ms")
    if type(value) is not int or not 1000 <= value <= 30_000:
        raise TranscriptionUnsupportedError(
            "The binding publishes no segment ceiling.", param="model"
        )
    return value


__all__ = [
    "DEFAULT_WORKSPACE",
    "AdmittedExecution",
    "AudioProbe",
    "BindingFact",
    "StagedAudio",
    "Transcriber",
    "Transcription",
    "TranscriptionAdmission",
    "TranscriptionAudioTooLongError",
    "TranscriptionAudioUnsupportedError",
    "TranscriptionDeadlineExceededError",
    "TranscriptionEngineError",
    "TranscriptionFacts",
    "TranscriptionInputObjects",
    "TranscriptionLimits",
    "TranscriptionMediaTooLargeError",
    "TranscriptionModelNotFoundError",
    "TranscriptionOutcomeUnknownError",
    "TranscriptionOutputInvalidError",
    "TranscriptionOutputLimitExceededError",
    "TranscriptionProfileNotFoundError",
    "TranscriptionUnavailableError",
    "TranscriptionUnsupportedError",
    "TranscriptionWorkspaceDisabledError",
    "TranscriptionWorkspaceNotFoundError",
    "WorkspaceFact",
]
