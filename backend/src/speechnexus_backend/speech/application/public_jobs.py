"""Submit and read the public Job surface from published facts only."""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable, Mapping
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Protocol

from speechnexus_backend.speech.application.effective_configuration import (
    effective_configuration,
)
from speechnexus_backend.speech.domain.public_jobs import (
    JobCreateResult,
    JobKind,
    JobState,
    JobSubmission,
    NewPublicJob,
    PublicJob,
    PublicJobNotFoundError,
    canonical_digest,
    check_transition,
    idempotency_digest,
    identifier,
)
from speechnexus_backend.speech.domain.publications import Publication, PublicationKind

DEFAULT_WORKSPACE = "workspace-default"
_REQUIRED_TASK: dict[JobKind, str] = {"asr": "asr_file", "tts": "tts_file"}


class UnsupportedJobKindError(ValueError):
    """The frozen contract declares more kinds than this execution unit accepts."""


class UnknownWorkspaceError(LookupError):
    pass


class DisabledWorkspaceError(ValueError):
    pass


class ModelNotPublishedError(LookupError):
    pass


class VoiceNotPublishedError(LookupError):
    pass


class ProfileNotPublishedError(LookupError):
    pass


class PronunciationNotFoundError(LookupError):
    pass


class AssetNotReadyError(LookupError):
    pass


class IdempotencyConflictError(RuntimeError):
    pass


class JobOutcomeUnknownError(RuntimeError):
    pass


class JobRejectedError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class WorkspaceFact:
    policy_revision: int
    active: bool


@dataclass(frozen=True, slots=True)
class BindingFact:
    publication: Publication
    definition: Mapping[str, Any]


class JobFacts(Protocol):
    """Read-only authoritative inputs; every absence is explicit."""

    def workspace(self, workspace_id: str) -> WorkspaceFact | None: ...

    def latest_binding(self, model_id: str) -> BindingFact | None: ...

    def publication(
        self, kind: PublicationKind, object_id: str, revision: int
    ) -> Publication | None: ...

    def pronunciation_exists(self, object_id: str, revision: int) -> bool: ...

    def asset(self, workspace_id: str, asset_id: str) -> tuple[str, str] | None: ...

    def catalog_revision(self) -> int: ...

    def now_us(self) -> int: ...


class PublicJobStore(Protocol):
    def create(self, job: NewPublicJob) -> JobCreateResult: ...

    def get(self, job_id: str) -> PublicJob | None: ...

    def advance(
        self,
        current: PublicJob,
        target: JobState,
        *,
        result_json: str | None,
        error_code: str | None,
        now_us: int,
    ) -> PublicJob: ...


class PublicJobs:
    """One explicit resolution, one atomic write, then bounded reads."""

    def __init__(
        self,
        facts: JobFacts,
        store: PublicJobStore,
        *,
        now_us: Callable[[], int] | None = None,
    ) -> None:
        self._facts, self._store = facts, store
        self._now_us = now_us or facts.now_us

    def submit(
        self,
        payload: Mapping[str, Any],
        *,
        request_id: str,
        idempotency_key: str | None,
    ) -> PublicJob:
        kind = payload.get("kind")
        if kind not in ("asr", "tts"):
            raise UnsupportedJobKindError
        typed_kind: JobKind = kind
        workspace_id = payload.get("workspace_id", DEFAULT_WORKSPACE)
        if not isinstance(workspace_id, str):
            raise UnknownWorkspaceError
        identifier(workspace_id)
        workspace = self._facts.workspace(workspace_id)
        if workspace is None:
            raise UnknownWorkspaceError
        if not workspace.active:
            raise DisabledWorkspaceError

        model_id = payload.get("model")
        if not isinstance(model_id, str):
            raise ModelNotPublishedError
        binding = self._facts.latest_binding(model_id)
        if binding is None:
            raise ModelNotPublishedError
        tasks = binding.definition.get("tasks")
        if not isinstance(tasks, list) or _REQUIRED_TASK[typed_kind] not in tasks:
            raise ModelNotPublishedError

        voice = self._reference(payload.get("voice"))
        if typed_kind == "tts":
            if voice is None:
                raise VoiceNotPublishedError
            voice_publication = self._facts.publication("voice", voice[0], voice[1])
            if voice_publication is None:
                raise VoiceNotPublishedError
        else:
            voice_publication = None

        profile = self._reference(payload.get("profile"))
        profile_publication = None
        if profile is not None:
            profile_publication = self._facts.publication("profile", profile[0], profile[1])
            if profile_publication is None:
                raise ProfileNotPublishedError

        pronunciation = self._reference(payload.get("pronunciation"))
        pronunciation_publication = None
        if pronunciation is not None:
            if not self._facts.pronunciation_exists(*pronunciation):
                raise PronunciationNotFoundError
            pronunciation_publication = Publication(
                "voice", pronunciation[0], pronunciation[1], "pronunciation-revision", 0
            )

        self._require_inputs(typed_kind, payload, workspace_id)

        catalog_revision = self._facts.catalog_revision()
        if catalog_revision < 1:
            raise ModelNotPublishedError
        configuration = effective_configuration(
            models={typed_kind: binding.publication},
            policy_revision=workspace.policy_revision,
            catalog_revision=catalog_revision,
            profile=profile_publication,
            voice=voice_publication,
            pronunciation=pronunciation_publication,
        )
        submission = JobSubmission(
            request_id=request_id,
            client_reference=self._client_reference(payload.get("client_reference")),
            effective_configuration_json=json.dumps(
                configuration, sort_keys=True, separators=(",", ":"), ensure_ascii=False
            ),
            execution_revision=1,
            progress_unit="text_codepoints" if typed_kind == "tts" else "audio_ms",
            progress_total=_text_length(payload),
            submitted_at_us=self._now_us(),
        )
        request = NewPublicJob(
            job_id="job-" + uuid.uuid4().hex,
            workspace_id=workspace_id,
            kind=typed_kind,
            model_id=model_id,
            request_digest=canonical_digest(_canonical_intent(payload, typed_kind)),
            idempotency_key_digest=(
                None if idempotency_key is None else idempotency_digest(idempotency_key)
            ),
            submission=submission,
        )
        result = self._store.create(request)
        if result.outcome == "conflict":
            raise IdempotencyConflictError
        if result.outcome == "unknown":
            raise JobOutcomeUnknownError
        if result.outcome == "rejected" or result.job is None:
            raise JobRejectedError
        return result.job

    def get(self, job_id: str) -> PublicJob:
        identifier(job_id)
        job = self._store.get(job_id)
        if job is None or job.job_id != job_id:
            raise PublicJobNotFoundError
        return job

    def advance(
        self,
        job_id: str,
        target: JobState,
        *,
        result_json: str | None = None,
        error_code: str | None = None,
    ) -> PublicJob:
        current = self.get(job_id)
        check_transition(current.state, target, result_json=result_json, error_code=error_code)
        return self._store.advance(
            current,
            target,
            result_json=result_json,
            error_code=error_code,
            now_us=self._now_us(),
        )

    @staticmethod
    def _reference(value: Any) -> tuple[str, int] | None:
        if value is None:
            return None
        if not isinstance(value, dict):
            raise ValueError("Invalid resource reference")
        object_id, revision = value.get("id"), value.get("revision")
        if not isinstance(object_id, str) or not isinstance(revision, str):
            raise ValueError("Invalid resource reference")
        identifier(object_id)
        if not revision.isdigit() or revision.startswith("0"):
            raise ValueError("Invalid resource revision")
        return object_id, int(revision)

    @staticmethod
    def _client_reference(value: Any) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str) or len(value) > 256:
            raise ValueError("Invalid client reference")
        return value

    def _require_inputs(self, kind: JobKind, payload: Mapping[str, Any], workspace_id: str) -> None:
        request_input = payload.get("input")
        if not isinstance(request_input, dict):
            raise AssetNotReadyError
        asset_id = (
            request_input.get("audio_asset_id")
            if kind == "asr"
            else request_input.get("text_asset_id")
        )
        if asset_id is None:
            return
        if not isinstance(asset_id, str):
            raise AssetNotReadyError
        asset = self._facts.asset(workspace_id, asset_id)
        expected = "asr_input" if kind == "asr" else "text_input"
        if asset is None or asset != ("ready", expected):
            raise AssetNotReadyError


def _text_length(payload: Mapping[str, Any]) -> int | None:
    request_input = payload.get("input")
    if not isinstance(request_input, dict):
        return None
    text = request_input.get("text")
    return len(text) if isinstance(text, str) else None


def _canonical_intent(payload: Mapping[str, Any], kind: JobKind) -> dict[str, Any]:
    """Apply only contract defaults so omitted and explicit defaults share one key."""
    intent = deepcopy(dict(payload))
    intent.setdefault("workspace_id", DEFAULT_WORKSPACE)
    output_defaults = (
        {"audio_format": "mp3", "save_artifacts": True}
        if kind == "tts"
        else {"transcript_format": "json", "save_artifacts": True}
    )
    output = intent.get("output")
    if not isinstance(output, dict):
        intent["output"] = output_defaults
    else:
        for name, value in output_defaults.items():
            output.setdefault(name, value)
    constraints = intent.get("constraints")
    constraint_defaults = {"allow_degradation": False, "require_exact_parameters": False}
    if not isinstance(constraints, dict):
        intent["constraints"] = constraint_defaults
    else:
        for name, value in constraint_defaults.items():
            constraints.setdefault(name, value)
    return intent


__all__ = [
    "AssetNotReadyError",
    "DisabledWorkspaceError",
    "IdempotencyConflictError",
    "JobFacts",
    "JobOutcomeUnknownError",
    "JobRejectedError",
    "ModelNotPublishedError",
    "ProfileNotPublishedError",
    "PronunciationNotFoundError",
    "PublicJobStore",
    "PublicJobs",
    "UnknownWorkspaceError",
    "UnsupportedJobKindError",
    "VoiceNotPublishedError",
]
