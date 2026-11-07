"""Submit and read the public Job surface from published facts only."""

from __future__ import annotations

import base64
import binascii
import json
import re
import uuid
from collections.abc import Callable, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
from functools import lru_cache
from importlib.resources import files
from typing import Any, Literal, Protocol, runtime_checkable

import rfc8785
from jsonschema import Draft202012Validator, FormatChecker

from speechnexus_backend.speech.application.effective_configuration import (
    effective_configuration,
)
from speechnexus_backend.speech.domain.public_jobs import (
    JobArtifactFact,
    JobCancellationReceipt,
    JobCancellationResult,
    JobCreateResult,
    JobEventFact,
    JobIntent,
    JobKind,
    JobState,
    JobSubmission,
    NewPublicJob,
    PublicJob,
    PublicJobNotFoundError,
    PublicJobUnavailableError,
    RevisionCreateResult,
    canonical_digest,
    check_transition,
    idempotency_digest,
    identifier,
    retryable,
)
from speechnexus_backend.speech.domain.publications import Publication, PublicationKind

DEFAULT_WORKSPACE = "workspace-default"
_REVISION = re.compile(r"[1-9][0-9]{0,18}\Z")


@lru_cache(maxsize=1)
def _validation_result_validator() -> Draft202012Validator:
    """The generated file contract, including its nested VoiceValidation schema."""
    bundle = json.loads(files("speechnexus_contracts").joinpath("file.schema.json").read_text())
    return Draft202012Validator(
        {"$ref": "#/$defs/VoiceValidationJobResult", "$defs": bundle["$defs"]},
        format_checker=FormatChecker(),
    )


_REQUIRED_TASK: dict[JobKind, str] = {
    "asr": "asr_file",
    "tts": "tts_file",
    "voice_design": "voice_design",
    "voice_clone": "voice_clone",
    "voice_validation": "voice_validation",
}


def _encode_list_cursor(created_at_us: int, job_id: str, scope: str) -> str:
    document = json.dumps([1, created_at_us, job_id, scope], separators=(",", ":"))
    return base64.urlsafe_b64encode(document.encode("ascii")).decode("ascii").rstrip("=")


def _list_cursor(cursor: str, scope: str) -> tuple[int, str]:
    try:
        if not 1 <= len(cursor) <= 512:
            raise ValueError
        data = json.loads(
            base64.b64decode(cursor + "=" * (-len(cursor) % 4), altchars=b"-_", validate=True)
        )
        if (
            not isinstance(data, list)
            or len(data) != 4
            or type(data[0]) is not int
            or data[0] != 1
            or type(data[1]) is not int
            or not 0 <= data[1] <= 253402300799999999
            or not isinstance(data[2], str)
            or data[3] != scope
        ):
            raise ValueError
        identifier(data[2])
        if _encode_list_cursor(data[1], data[2], scope) != cursor:
            raise ValueError
        return data[1], data[2]
    except ValueError, TypeError, binascii.Error, UnicodeError, RecursionError:
        raise ValueError("Invalid Job list cursor") from None


class UnsupportedJobKindError(ValueError):
    """The frozen contract declares more kinds than this execution unit accepts."""


class UnknownWorkspaceError(LookupError):
    pass


class DisabledWorkspaceError(ValueError):
    pass


class ModelNotPublishedError(LookupError):
    pass


class ModelBindingError(ValueError):
    """A stable, non-retryable rejection of an explicit binding selection."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class VoiceDraftStaleRevisionError(RuntimeError):
    """The requested revision is no longer the draft head."""


class VoiceDraftNotCandidateError(RuntimeError):
    """The head revision has no succeeded generation Job to validate."""


class VoiceDraftNotGeneratableError(RuntimeError):
    """The draft lifecycle no longer accepts a generation or validation Job."""


class VoiceNotPublishedError(LookupError):
    pass


class ProfileNotPublishedError(LookupError):
    pass


class PronunciationNotFoundError(LookupError):
    pass


class AssetNotReadyError(LookupError):
    pass


class VoiceInputInvalidError(ValueError):
    """The creation input does not satisfy the language field's request contract."""


class IdempotencyConflictError(RuntimeError):
    pass


class JobOutcomeUnknownError(RuntimeError):
    pass


JobRejectionReason = Literal[
    "facts_changed",
    "identity_conflict",
    "revision_exhausted",
    "storage_rejected",
    "contract",
]


class JobRejectedError(RuntimeError):
    """A prepared Job was refused; ``code`` names the class without storage detail.

    ``facts_changed`` and ``revision_exhausted`` are conflicts with current
    state. ``identity_conflict`` is the same 409 class but is not a normal
    reproducible path: an unkeyed submission stores a NULL key digest and a
    random uuid4 Job identity, so the unique constraint cannot collide that
    way; only an extreme UUID Job identity collision can reach it.
    ``storage_rejected`` and ``contract`` are deterministic defects (a database
    refusal or a missing result payload), not client conflicts. A connection
    failure in the submission window is not a refusal at all: it cannot prove
    the transaction did not commit, so it is reported as outcome unknown.
    """

    def __init__(self, code: JobRejectionReason) -> None:
        self.code = code
        super().__init__(code)


class JobCleanupPendingError(RuntimeError):
    """A terminal Job still holds resources; cancellation cannot claim it is clean."""


class JobRetryNotAllowedError(RuntimeError):
    """The Job state cannot be replaced by a retry."""


class JobRetryUnavailableError(RuntimeError):
    """The original canonical intent is no longer available to re-send."""


@dataclass(frozen=True, slots=True)
class CancellationView:
    """One durably accepted cancellation and its authoritative current Job."""

    job: PublicJob
    receipt: JobCancellationReceipt
    accepted_now: bool


@dataclass(frozen=True, slots=True)
class WorkspaceFact:
    policy_revision: int
    active: bool


@dataclass(frozen=True, slots=True)
class BindingFact:
    publication: Publication
    definition: Mapping[str, Any]
    current: bool = True
    retired: bool = False


@dataclass(frozen=True, slots=True)
class VoiceDraftRevisionFact:
    """The immutable revision facts a validation Job is allowed to target."""

    workspace_id: str
    kind: str
    head_revision: str
    generation_job_id: str
    state: JobState
    result_json: str | None
    model_id: str
    binding: tuple[str, int]


def require_pinned_binding(
    binding: BindingFact | None,
    reference: tuple[str, int],
    model: str,
    *,
    task: str = "tts_file",
) -> BindingFact:
    """Validate exact published identity; reuse after the catalogue object lock."""
    if binding is None:
        raise ModelBindingError("model_binding_not_published")
    publication, definition = binding.publication, binding.definition
    if (
        publication.kind != "binding"
        or (publication.object_id, publication.revision) != reference
        or ("binding_id" in definition and definition["binding_id"] != publication.object_id)
        or ("revision" in definition and definition["revision"] != str(publication.revision))
        or ("revision_id" in definition and definition["revision_id"] != publication.revision_id)
        or not isinstance(definition.get("logical_model"), str)
        or not isinstance(definition.get("tasks"), list)
    ):
        raise PublicJobUnavailableError
    # Draft storage keeps identity in columns, not necessarily in definition_json.
    # The adapter's exact three-column join owns that identity; any identity
    # also embedded in the document must agree. Lifecycle comes from publication
    # and retirement rows, never the original draft's lifecycle projection.
    if binding.retired:
        raise ModelBindingError("model_binding_retired")
    if not binding.current:
        raise ModelBindingError("model_binding_not_current")
    if definition["logical_model"] != model:
        raise ModelBindingError("model_binding_mismatch")
    if task not in definition["tasks"]:
        raise ModelBindingError("model_binding_task_not_supported")
    return binding


class JobFacts(Protocol):
    """Read-only authoritative inputs; every absence is explicit."""

    def workspace(self, workspace_id: str) -> WorkspaceFact | None: ...

    def latest_binding(self, model_id: str) -> BindingFact | None: ...

    def binding_revision(self, binding_id: str, revision: int) -> BindingFact | None: ...

    def publication(
        self, kind: PublicationKind, object_id: str, revision: int
    ) -> Publication | None: ...

    def pronunciation_exists(self, object_id: str, revision: int) -> bool: ...

    def asset(self, workspace_id: str, asset_id: str) -> tuple[str, str] | None: ...

    def voice_draft_revision(
        self, draft_id: str, revision: str
    ) -> VoiceDraftRevisionFact | None: ...

    def catalog_revision(self) -> int: ...

    def now_us(self) -> int: ...


class PublicJobStore(Protocol):
    def find_by_key(
        self,
        workspace_id: str,
        key_digest: str,
        request_digest: str,
        retry_of_job_id: str | None,
    ) -> JobCreateResult | None:
        """Serialize with keyed create; settle an in-flight writer before absence."""
        ...

    def create(self, job: NewPublicJob) -> JobCreateResult: ...

    def create_revision(
        self, job: NewPublicJob, *, draft_id: str, expected_revision: str
    ) -> RevisionCreateResult: ...

    def revision_for_job(self, draft_id: str, job_id: str) -> str | None: ...

    def get(self, job_id: str) -> PublicJob | None: ...

    def list_jobs(
        self,
        *,
        workspace_id: str,
        state: str | None,
        kind: str | None,
        after: tuple[int, str] | None,
        limit: int,
    ) -> tuple[PublicJob, ...]:
        """Read strictly after (created_at_us, job_id), in ascending key order."""
        ...

    def intent_document(self, job_id: str) -> JobIntent | None:
        """The immutable canonical intent of an accepted Job, or None when redacted."""
        ...

    def events(self, job_id: str, *, after_seq: int, limit: int) -> tuple[JobEventFact, ...]: ...

    def artifact(self, workspace_id: str, artifact_id: str) -> JobArtifactFact | None: ...

    def cancel(
        self,
        job_id: str,
        *,
        workspace_id: str | None,
        request_id: str,
        key_digest: str | None,
        request_digest: str,
        now_us: int,
    ) -> JobCancellationResult: ...

    def settle_cancelled(
        self, job_id: str, *, expected_version: int, now_us: int
    ) -> PublicJob | None: ...

    def advance(
        self,
        current: PublicJob,
        target: JobState,
        *,
        result_json: str | None,
        error_code: str | None,
        now_us: int,
    ) -> PublicJob: ...


@runtime_checkable
class PublicJobArtifactBatch(Protocol):
    """Page-level Artifact read: one transaction for every referenced id.

    Kept separate from ``PublicJobStore`` so a store that cannot batch still
    serves single reads; the production adapter implements both.
    """

    def artifacts_batch(
        self, workspace_id: str, artifact_ids: Sequence[str]
    ) -> Mapping[str, JobArtifactFact]: ...


def _candidate_artifact_ids(job: PublicJob) -> tuple[str, ...]:
    """Ids worth prefetching; the strict rules stay in ``PublicJobs._artifacts``."""
    if job.state != "succeeded" or job.result_json is None:
        return ()
    try:
        result = json.loads(job.result_json)
    except ValueError, TypeError, RecursionError:
        return ()
    if not isinstance(result, dict):
        return ()
    candidates: list[object] = [
        result.get("transcript_artifact_id"),
        result.get("audio_artifact_id"),
        result.get("preview_artifact_id"),
        result.get("validation_report_artifact_id"),
    ]
    previews = result.get("preview_artifact_ids")
    if isinstance(previews, list):
        candidates.extend(previews)
    wanted = []
    for candidate in candidates:
        if not isinstance(candidate, str):
            continue
        try:
            wanted.append(identifier(candidate))
        except ValueError:
            continue
    return tuple(wanted)


def resolve_voice_previews(
    *,
    workspace_id: str,
    kind: str,
    state: str,
    result_json: str | None,
    artifact: Callable[[str, str], JobArtifactFact | None],
) -> tuple[JobArtifactFact, ...]:
    """Resolve the preview facts a succeeded voice result references.

    One rule serves the public GET projection and the metadata transaction, so a
    candidate cannot be accepted by one path while rejected by the other.
    """
    if state != "succeeded":
        return ()
    if result_json is None:
        raise PublicJobUnavailableError
    try:
        result = json.loads(result_json)
    except ValueError, TypeError, RecursionError:
        raise PublicJobUnavailableError from None
    if not isinstance(result, dict) or result.get("kind") != kind:
        raise PublicJobUnavailableError
    artifact_ids = result.get("preview_artifact_ids")
    receipt_id = result.get("receipt_id")
    if (
        not isinstance(artifact_ids, list)
        or not artifact_ids
        or not isinstance(receipt_id, str)
        or not all(isinstance(item, str) for item in artifact_ids)
        or len(set(artifact_ids)) != len(artifact_ids)
    ):
        raise PublicJobUnavailableError
    return tuple(
        _ready_artifact(
            workspace_id=workspace_id,
            artifact_id=artifact_id,
            receipt_id=receipt_id,
            kind="preview",
            artifact=artifact,
        )
        for artifact_id in artifact_ids
    )


def resolve_voice_preview(
    *,
    workspace_id: str,
    kind: str,
    state: str,
    result_json: str | None,
    artifact: Callable[[str, str], JobArtifactFact | None],
) -> tuple[JobArtifactFact, ...]:
    """Resolve the single ready preview a succeeded ``voice_preview`` Job names.

    The preview Job result is singular by contract (``preview_artifact_id``),
    unlike a generation result's ``preview_artifact_ids``. One rule serves the
    single read and the page batch: the referenced Artifact must be committed,
    ready, carry the ``preview`` kind and the same receipt.
    """
    if state != "succeeded":
        return ()
    if kind != "voice_preview" or result_json is None:
        raise PublicJobUnavailableError
    try:
        result = json.loads(result_json)
    except ValueError, TypeError, RecursionError:
        raise PublicJobUnavailableError from None
    if not isinstance(result, dict) or result.get("kind") != kind:
        raise PublicJobUnavailableError
    artifact_id = result.get("preview_artifact_id")
    receipt_id = result.get("receipt_id")
    if not isinstance(artifact_id, str) or not isinstance(receipt_id, str):
        raise PublicJobUnavailableError
    return (
        _ready_artifact(
            workspace_id=workspace_id,
            artifact_id=artifact_id,
            receipt_id=receipt_id,
            kind="preview",
            artifact=artifact,
        ),
    )


def _ready_artifact(
    *,
    workspace_id: str,
    artifact_id: str,
    receipt_id: str,
    kind: str,
    artifact: Callable[[str, str], JobArtifactFact | None],
) -> JobArtifactFact:
    """One committed, ready Artifact with the exact declared kind and receipt."""
    try:
        identifier(artifact_id)
    except ValueError:
        raise PublicJobUnavailableError from None
    fact = artifact(workspace_id, artifact_id)
    if (
        fact is None
        or fact.artifact_id != artifact_id
        or fact.kind != kind
        or fact.receipt_id != receipt_id
    ):
        raise PublicJobUnavailableError
    return fact


def resolve_voice_validation_report(
    *,
    workspace_id: str,
    kind: str,
    state: str,
    result_json: str | None,
    artifact: Callable[[str, str], JobArtifactFact | None],
) -> tuple[JobArtifactFact, ...]:
    """Resolve the single ready validation report a succeeded result references.

    The result is never trusted on its own: the referenced Artifact row must be
    committed, ready, carry the validation_report kind and the same receipt.
    """
    if state != "succeeded":
        return ()
    if kind != "voice_validation" or result_json is None:
        raise PublicJobUnavailableError
    try:
        result = json.loads(result_json)
    except ValueError, TypeError, RecursionError:
        raise PublicJobUnavailableError from None
    if not isinstance(result, dict) or result.get("kind") != kind:
        raise PublicJobUnavailableError
    if not _validation_result_validator().is_valid(result):
        raise PublicJobUnavailableError
    artifact_id = result.get("validation_report_artifact_id")
    receipt_id = result.get("receipt_id")
    if not isinstance(artifact_id, str) or not isinstance(receipt_id, str):
        raise PublicJobUnavailableError
    return (
        _ready_artifact(
            workspace_id=workspace_id,
            artifact_id=artifact_id,
            receipt_id=receipt_id,
            kind="validation_report",
            artifact=artifact,
        ),
    )


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
        self._artifact_batch: PublicJobArtifactBatch | None = (
            store if isinstance(store, PublicJobArtifactBatch) else None
        )
        self._now_us = now_us or facts.now_us

    def submit(
        self,
        payload: Mapping[str, Any],
        *,
        request_id: str,
        idempotency_key: str | None,
    ) -> PublicJob:
        kind = payload.get("kind")
        if kind not in (
            "asr",
            "tts",
            "voice_design",
            "voice_clone",
            "voice_preview",
            "voice_validation",
        ):
            raise UnsupportedJobKindError
        typed_kind: JobKind = kind
        workspace_id = payload.get("workspace_id", DEFAULT_WORKSPACE)
        if not isinstance(workspace_id, str):
            raise UnknownWorkspaceError
        identifier(workspace_id)
        if typed_kind == "voice_validation":
            payload = self._validation_intent(payload, workspace_id)
        elif typed_kind == "voice_preview":
            payload = self._preview_intent(payload, workspace_id)
        intent = _canonical_intent(payload, typed_kind)
        request_digest = canonical_digest(intent)
        key_digest = None if idempotency_key is None else idempotency_digest(idempotency_key)
        if key_digest is not None:
            original = self._find_original(
                workspace_id, key_digest, request_digest, retry_of_job_id=None
            )
            if original is not None:
                return _submission_result(original)
        try:
            return self._submit_new(
                payload,
                typed_kind=typed_kind,
                workspace_id=workspace_id,
                intent=intent,
                request_digest=request_digest,
                key_digest=key_digest,
                request_id=request_id,
                retry_of_job_id=None,
            )
        except (
            UnknownWorkspaceError,
            DisabledWorkspaceError,
            ModelNotPublishedError,
            ModelBindingError,
            VoiceNotPublishedError,
            ProfileNotPublishedError,
            PronunciationNotFoundError,
            AssetNotReadyError,
            VoiceDraftStaleRevisionError,
            VoiceDraftNotCandidateError,
        ):
            # The early lookup can miss a concurrent commit, and _submit_new may
            # already have taken and released the binding/draft row locks before
            # failing. Before rejecting changed facts, settle the same-key writer
            # and recover its result. If coordination fails, propagate
            # unavailable rather than a 422.
            if key_digest is not None:
                original = self._find_original(
                    workspace_id, key_digest, request_digest, retry_of_job_id=None
                )
                if original is not None:
                    return _submission_result(original)
            raise

    def submit_revision(
        self,
        payload: Mapping[str, Any],
        *,
        draft_id: str,
        expected_revision: str,
        request_id: str,
        idempotency_key: str | None,
    ) -> tuple[PublicJob, str]:
        """Append one revision to an existing draft; replay precedes every lock."""
        identifier(draft_id)
        if _REVISION.fullmatch(expected_revision) is None:
            raise ValueError("Invalid draft revision")
        kind = payload.get("kind")
        if kind not in ("voice_design", "voice_clone"):
            raise UnsupportedJobKindError
        typed_kind: JobKind = kind
        workspace_id = payload.get("workspace_id")
        if not isinstance(workspace_id, str):
            raise UnknownWorkspaceError
        identifier(workspace_id)
        target = payload.get("draft_target")
        if (
            not isinstance(target, dict)
            or set(target) != {"id", "expected_revision"}
            or target.get("id") != draft_id
            or target.get("expected_revision") != expected_revision
        ):
            raise ValueError("Draft target disagrees with its revision append")
        intent = _canonical_intent(payload, typed_kind)
        request_digest = canonical_digest(intent)
        key_digest = None if idempotency_key is None else idempotency_digest(idempotency_key)
        if key_digest is not None:
            original = self._find_original(
                workspace_id, key_digest, request_digest, retry_of_job_id=None
            )
            if original is not None:
                return _replayed_revision(original, draft_id, self._store)
        try:
            request = self._prepare_job(
                payload,
                typed_kind=typed_kind,
                workspace_id=workspace_id,
                intent=intent,
                request_digest=request_digest,
                key_digest=key_digest,
                request_id=request_id,
                retry_of_job_id=None,
            )
            result = self._store.create_revision(
                request, draft_id=draft_id, expected_revision=expected_revision
            )
            return _revision_result(result)
        except (
            UnknownWorkspaceError,
            DisabledWorkspaceError,
            ModelNotPublishedError,
            ModelBindingError,
            VoiceNotPublishedError,
            ProfileNotPublishedError,
            PronunciationNotFoundError,
            AssetNotReadyError,
        ):
            if key_digest is not None:
                original = self._find_original(
                    workspace_id, key_digest, request_digest, retry_of_job_id=None
                )
                if original is not None:
                    return _replayed_revision(original, draft_id, self._store)
            raise

    def _find_original(
        self,
        workspace_id: str,
        key_digest: str,
        request_digest: str,
        *,
        retry_of_job_id: str | None,
    ) -> JobCreateResult | None:
        try:
            original = self._store.find_by_key(
                workspace_id, key_digest, request_digest, retry_of_job_id
            )
        except PublicJobUnavailableError:
            raise JobOutcomeUnknownError from None
        if original is not None and (
            original.outcome not in ("replayed", "conflict")
            or (original.outcome == "replayed" and original.job is None)
        ):
            raise JobOutcomeUnknownError
        return original

    def _prepare_job(
        self,
        payload: Mapping[str, Any],
        *,
        typed_kind: JobKind,
        workspace_id: str,
        intent: Mapping[str, Any],
        request_digest: str,
        key_digest: str | None,
        request_id: str,
        retry_of_job_id: str | None,
    ) -> NewPublicJob:
        workspace = self._facts.workspace(workspace_id)
        if workspace is None:
            raise UnknownWorkspaceError
        if not workspace.active:
            raise DisabledWorkspaceError

        raw_model = payload.get("model")
        model_id: str
        pin: tuple[str, int] | None
        required_task = _REQUIRED_TASK.get(typed_kind)
        if typed_kind == "voice_validation":
            pin, derived_model = self._validation_binding(payload, workspace_id)
            if raw_model is None:
                model_id = derived_model
            elif isinstance(raw_model, str) and raw_model == derived_model:
                model_id = raw_model
            else:
                raise ModelBindingError("model_binding_mismatch")
        elif typed_kind == "voice_preview":
            # The preview's task is the target draft kind, not a fixed kind:
            # resolve both from the pinned revision before the Job is built.
            pin, derived_model, required_task = self._preview_binding(payload, workspace_id)
            if raw_model is None:
                model_id = derived_model
            elif isinstance(raw_model, str) and raw_model == derived_model:
                model_id = raw_model
            else:
                raise ModelBindingError("model_binding_mismatch")
        else:
            if not isinstance(raw_model, str):
                raise ModelNotPublishedError
            model_id = raw_model
            pin = self._binding_reference(payload.get("model_binding"))
            if "model_binding" in payload and (pin is None or typed_kind != "tts"):
                raise ValueError("Invalid model binding")
        binding: BindingFact
        if pin is not None:
            if required_task is None:
                raise PublicJobUnavailableError
            binding = require_pinned_binding(
                self._facts.binding_revision(*pin),
                pin,
                model_id,
                task=required_task,
            )
        else:
            latest = self._facts.latest_binding(model_id)
            if latest is None:
                raise ModelNotPublishedError
            binding = latest
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
                "pronunciation", pronunciation[0], pronunciation[1], "pronunciation-revision", 0
            )

        self._require_inputs(typed_kind, payload, workspace_id)

        catalog_revision = self._facts.catalog_revision()
        if catalog_revision < 1:
            raise ModelNotPublishedError
        configuration = effective_configuration(
            models={
                "voice" if typed_kind.startswith("voice_") else typed_kind: binding.publication
            },
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
            # Voice creation counts produced items. ASR/TTS use the durable
            # generated-audio watermark, never request text length as progress.
            progress_unit="items" if typed_kind.startswith("voice_") else "audio_ms",
            progress_total=None,
            submitted_at_us=self._now_us(),
        )
        job_id = "job-" + uuid.uuid4().hex
        return NewPublicJob(
            job_id=job_id,
            workspace_id=workspace_id,
            kind=typed_kind,
            model_id=model_id,
            request_digest=request_digest,
            request_document=rfc8785.dumps(intent).decode("utf-8"),
            initial_event_digest=canonical_digest(
                {
                    "job_id": job_id,
                    "state": "pending",
                    "request_digest": request_digest,
                }
            ),
            idempotency_key_digest=key_digest,
            submission=submission,
            expected_binding=binding.publication if pin is not None else None,
            retry_of_job_id=retry_of_job_id,
        )

    def _validation_intent(self, payload: Mapping[str, Any], workspace_id: str) -> dict[str, Any]:
        """Freeze the derived model and binding before key replay and locking.

        Head/state/preview and binding-publication eligibility stay in
        ``_validation_binding`` so a same-key replay still succeeds after the
        draft head advances or the binding is retired.
        """
        request_input = payload.get("input")
        if not isinstance(request_input, dict):
            raise VoiceInputInvalidError
        draft_id = request_input.get("voice_draft_id")
        revision = request_input.get("draft_revision")
        if not isinstance(draft_id, str) or not isinstance(revision, str):
            raise VoiceInputInvalidError
        identifier(draft_id)
        if _REVISION.fullmatch(revision) is None:
            raise VoiceInputInvalidError
        fact = self._facts.voice_draft_revision(draft_id, revision)
        if fact is None or fact.workspace_id != workspace_id:
            raise PublicJobNotFoundError
        explicit = self._binding_reference(payload.get("model_binding"))
        if explicit is not None and explicit != fact.binding:
            raise ModelBindingError("model_binding_mismatch")
        logical = fact.model_id
        raw_model = payload.get("model")
        if raw_model is not None and (not isinstance(raw_model, str) or raw_model != logical):
            raise ModelBindingError("model_binding_mismatch")
        return {
            **payload,
            "model": logical,
            "model_binding": {"id": fact.binding[0], "revision": str(fact.binding[1])},
        }

    def _validation_binding(
        self, payload: Mapping[str, Any], workspace_id: str
    ) -> tuple[tuple[str, int], str]:
        """Resolve the exact candidate revision a validation Job may target."""
        request_input = payload.get("input")
        if not isinstance(request_input, dict):
            raise VoiceInputInvalidError
        draft_id = request_input.get("voice_draft_id")
        revision = request_input.get("draft_revision")
        if not isinstance(draft_id, str) or not isinstance(revision, str):
            raise VoiceInputInvalidError
        identifier(draft_id)
        fact = self._facts.voice_draft_revision(draft_id, revision)
        if fact is None or fact.workspace_id != workspace_id:
            raise PublicJobNotFoundError
        explicit = self._binding_reference(payload.get("model_binding"))
        if explicit is not None and explicit != fact.binding:
            raise ModelBindingError("model_binding_mismatch")
        if fact.head_revision != revision:
            raise VoiceDraftStaleRevisionError
        if fact.state != "succeeded":
            raise VoiceDraftNotCandidateError
        if fact.kind not in ("design", "clone"):
            raise PublicJobUnavailableError
        # A succeeded generation is not enough: the preview receipt and every
        # ready artifact must agree, or the validation target is not authoritative.
        resolve_voice_previews(
            workspace_id=fact.workspace_id,
            kind="voice_" + fact.kind,
            state=fact.state,
            result_json=fact.result_json,
            artifact=self._store.artifact,
        )
        binding = self._facts.binding_revision(*fact.binding)
        if binding is None:
            raise ModelBindingError("model_binding_not_published")
        logical = binding.definition.get("logical_model")
        if not isinstance(logical, str):
            raise ModelBindingError("model_binding_not_published")
        return fact.binding, logical

    def _preview_intent(self, payload: Mapping[str, Any], workspace_id: str) -> dict[str, Any]:
        """Freeze the derived model and binding before the early key replay.

        Head equality, candidate readiness and binding publication stay in
        ``_preview_binding``. The early same-key lookup therefore runs before
        them; only a request that misses that lookup evaluates them, and the
        fallback lookup in ``submit`` re-checks the key after they fail.
        """
        request_input = payload.get("input")
        if not isinstance(request_input, dict):
            raise VoiceInputInvalidError
        draft_id = request_input.get("voice_draft_id")
        revision = request_input.get("draft_revision")
        text = request_input.get("text")
        if not isinstance(draft_id, str) or not isinstance(revision, str):
            raise VoiceInputInvalidError
        identifier(draft_id)
        if _REVISION.fullmatch(revision) is None:
            raise VoiceInputInvalidError
        if not isinstance(text, str) or not 1 <= len(text) <= 2000:
            raise VoiceInputInvalidError
        fact = self._facts.voice_draft_revision(draft_id, revision)
        if fact is None or fact.workspace_id != workspace_id:
            raise PublicJobNotFoundError
        explicit = self._binding_reference(payload.get("model_binding"))
        if explicit is not None and explicit != fact.binding:
            raise ModelBindingError("model_binding_mismatch")
        logical = fact.model_id
        raw_model = payload.get("model")
        if raw_model is not None and (not isinstance(raw_model, str) or raw_model != logical):
            raise ModelBindingError("model_binding_mismatch")
        return {
            **payload,
            "model": logical,
            "model_binding": {"id": fact.binding[0], "revision": str(fact.binding[1])},
        }

    def _preview_binding(
        self, payload: Mapping[str, Any], workspace_id: str
    ) -> tuple[tuple[str, int], str, str]:
        """Resolve the exact head candidate a preview Job may target."""
        request_input = payload.get("input")
        if not isinstance(request_input, dict):
            raise VoiceInputInvalidError
        draft_id = request_input.get("voice_draft_id")
        revision = request_input.get("draft_revision")
        if not isinstance(draft_id, str) or not isinstance(revision, str):
            raise VoiceInputInvalidError
        identifier(draft_id)
        fact = self._facts.voice_draft_revision(draft_id, revision)
        if fact is None or fact.workspace_id != workspace_id:
            raise PublicJobNotFoundError
        explicit = self._binding_reference(payload.get("model_binding"))
        if explicit is not None and explicit != fact.binding:
            raise ModelBindingError("model_binding_mismatch")
        if fact.kind not in ("design", "clone"):
            raise VoiceInputInvalidError
        if fact.head_revision != revision:
            raise VoiceDraftStaleRevisionError
        if fact.state != "succeeded":
            raise VoiceDraftNotCandidateError
        # A preview synthesizes text for a candidate voice: the generation Job
        # must already have committed its receipt and every ready preview.
        resolve_voice_previews(
            workspace_id=fact.workspace_id,
            kind="voice_" + fact.kind,
            state=fact.state,
            result_json=fact.result_json,
            artifact=self._store.artifact,
        )
        binding = self._facts.binding_revision(*fact.binding)
        if binding is None:
            raise ModelBindingError("model_binding_not_published")
        logical = binding.definition.get("logical_model")
        if not isinstance(logical, str):
            raise ModelBindingError("model_binding_not_published")
        return fact.binding, logical, "voice_" + fact.kind

    def _submit_new(
        self,
        payload: Mapping[str, Any],
        *,
        typed_kind: JobKind,
        workspace_id: str,
        intent: Mapping[str, Any],
        request_digest: str,
        key_digest: str | None,
        request_id: str,
        retry_of_job_id: str | None,
    ) -> PublicJob:
        request = self._prepare_job(
            payload,
            typed_kind=typed_kind,
            workspace_id=workspace_id,
            intent=intent,
            request_digest=request_digest,
            key_digest=key_digest,
            request_id=request_id,
            retry_of_job_id=retry_of_job_id,
        )
        return _submission_result(self._store.create(request))

    def get(self, job_id: str) -> PublicJob:
        identifier(job_id)
        job = self._store.get(job_id)
        if job is None or job.job_id != job_id:
            raise PublicJobNotFoundError
        return job

    def list_jobs(
        self,
        *,
        workspace_id: str = DEFAULT_WORKSPACE,
        state: str | None = None,
        kind: str | None = None,
        cursor: str | None = None,
        limit: int = 50,
    ) -> tuple[tuple[PublicJob, ...], str | None]:
        """Live keyset pages; the cursor freezes the ordering key and filter scope."""
        identifier(workspace_id)
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("Invalid Job page size")
        scope = canonical_digest([workspace_id, state, kind])
        after = None if cursor is None else _list_cursor(cursor, scope)
        rows = self._store.list_jobs(
            workspace_id=workspace_id, state=state, kind=kind, after=after, limit=limit + 1
        )
        page = rows[:limit]
        next_cursor = None
        if len(rows) > limit:
            last = page[-1]
            next_cursor = _encode_list_cursor(last.created_at_us, last.job_id, scope)
        return page, next_cursor

    def retry(
        self,
        job_id: str,
        *,
        request_id: str,
        idempotency_key: str | None,
    ) -> PublicJob:
        """Resend the frozen canonical intent as a new Job that names its parent.

        The parent stays terminal: nothing here updates its state, result or
        history. A retry request can only be observed after the parent froze, so
        the state read below cannot be overtaken by a later transition.
        """
        identifier(job_id)
        parent = self.get(job_id)
        if parent.state == "failed" and parent.error_code == "cancellation_unconfirmed":
            raise JobCleanupPendingError
        if not retryable(parent):
            raise JobRetryNotAllowedError
        intent = self._store.intent_document(job_id)
        if intent is None:
            # Content deletion removed the parameters; a replacement cannot be
            # rebuilt from mutable defaults or from memory.
            raise JobRetryUnavailableError
        try:
            payload = json.loads(intent.document)
        except ValueError, TypeError, RecursionError:
            raise PublicJobUnavailableError from None
        # Deliberate second layer behind ``JobIntent``: the stored document was
        # validated against its digest on read, and the parsed payload is checked
        # again here before it can become a replacement Job. Both layers are
        # required; neither may be removed as "redundant".
        if not isinstance(payload, dict) or canonical_digest(payload) != intent.request_digest:
            raise PublicJobUnavailableError
        if payload.get("kind") != parent.kind or (
            payload.get("model", parent.model_id) != parent.model_id
        ):
            raise PublicJobUnavailableError
        if payload.get("draft_target") is not None:
            # A revision append is not a fresh draft; replay the original key or
            # start a new generation explicitly.
            raise JobRetryUnavailableError
        key_digest = None if idempotency_key is None else idempotency_digest(idempotency_key)
        if key_digest is not None:
            original = self._find_original(
                parent.workspace_id,
                key_digest,
                intent.request_digest,
                retry_of_job_id=job_id,
            )
            if original is not None:
                return _retransmission_result(original, job_id)
        try:
            return self._submit_new(
                payload,
                typed_kind=parent.kind,
                workspace_id=parent.workspace_id,
                intent=payload,
                request_digest=intent.request_digest,
                key_digest=key_digest,
                request_id=request_id,
                retry_of_job_id=parent.job_id,
            )
        except (
            UnknownWorkspaceError,
            DisabledWorkspaceError,
            ModelNotPublishedError,
            ModelBindingError,
            VoiceNotPublishedError,
            ProfileNotPublishedError,
            PronunciationNotFoundError,
            AssetNotReadyError,
        ):
            if key_digest is not None:
                original = self._find_original(
                    parent.workspace_id,
                    key_digest,
                    intent.request_digest,
                    retry_of_job_id=job_id,
                )
                if original is not None:
                    return _retransmission_result(original, job_id)
            raise

    def events(self, job_id: str, *, after_seq: int, limit: int) -> tuple[JobEventFact, ...]:
        """Read persisted event rows strictly after one sequence."""
        identifier(job_id)
        if type(after_seq) is not int or not -1 <= after_seq <= 9_223_372_036_854_775_806:
            raise ValueError("Invalid public Job event cursor")
        if type(limit) is not int or not 1 <= limit <= 500:
            raise ValueError("Invalid public Job event page")
        return self._store.events(job_id, after_seq=after_seq, limit=limit)

    def artifacts(self, job: PublicJob) -> tuple[JobArtifactFact, ...]:
        """Resolve the committed Artifact a succeeded result references, or nothing."""
        return self._artifacts(job, self._store.artifact)

    def artifacts_page(self, jobs: Sequence[PublicJob]) -> tuple[tuple[JobArtifactFact, ...], ...]:
        """Resolve a whole page with one Artifact read per workspace.

        Each Job is still verified by the same rule as the single read above;
        the batch only replaces how the referenced rows are fetched.
        """
        wanted: dict[str, set[str]] = {}
        for job in jobs:
            wanted.setdefault(job.workspace_id, set()).update(_candidate_artifact_ids(job))
        if self._artifact_batch is None or not wanted:
            return tuple(self.artifacts(job) for job in jobs)
        fetched: dict[tuple[str, str], JobArtifactFact] = {}
        for workspace_id, artifact_ids in wanted.items():
            for artifact_id, fact in self._artifact_batch.artifacts_batch(
                workspace_id, sorted(artifact_ids)
            ).items():
                fetched[(workspace_id, artifact_id)] = fact

        def lookup(workspace_id: str, artifact_id: str) -> JobArtifactFact | None:
            # A batch miss is absence, never an invitation to read again.
            return fetched.get((workspace_id, artifact_id))

        return tuple(self._artifacts(job, lookup) for job in jobs)

    def _artifacts(
        self,
        job: PublicJob,
        artifact: Callable[[str, str], JobArtifactFact | None],
    ) -> tuple[JobArtifactFact, ...]:
        """One verification rule for the single and batched reads."""
        if job.state != "succeeded":
            return ()
        if job.result_json is None:
            raise PublicJobUnavailableError
        try:
            result = json.loads(job.result_json)
        except ValueError, TypeError, RecursionError:
            raise PublicJobUnavailableError from None
        if not isinstance(result, dict) or result.get("kind") != job.kind:
            raise PublicJobUnavailableError
        if job.kind in ("voice_design", "voice_clone"):
            return resolve_voice_previews(
                workspace_id=job.workspace_id,
                kind=job.kind,
                state=job.state,
                result_json=job.result_json,
                artifact=artifact,
            )
        if job.kind == "voice_preview":
            return resolve_voice_preview(
                workspace_id=job.workspace_id,
                kind=job.kind,
                state=job.state,
                result_json=job.result_json,
                artifact=artifact,
            )
        if job.kind == "voice_validation":
            return resolve_voice_validation_report(
                workspace_id=job.workspace_id,
                kind=job.kind,
                state=job.state,
                result_json=job.result_json,
                artifact=artifact,
            )
        name, kind = (
            ("transcript_artifact_id", "transcript")
            if job.kind == "asr"
            else ("audio_artifact_id", "audio")
        )
        artifact_id, receipt_id = result.get(name), result.get("receipt_id")
        if not isinstance(artifact_id, str) or not isinstance(receipt_id, str):
            raise PublicJobUnavailableError
        try:
            identifier(artifact_id)
            identifier(receipt_id)
        except ValueError:
            raise PublicJobUnavailableError from None
        fact = artifact(job.workspace_id, artifact_id)
        if (
            fact is None
            or fact.artifact_id != artifact_id
            or fact.kind != kind
            or fact.receipt_id != receipt_id
        ):
            raise PublicJobUnavailableError
        return (fact,)

    def cancel(
        self,
        job_id: str,
        *,
        request_id: str,
        idempotency_key: str | None,
        workspace_id: str | None = None,
    ) -> CancellationView:
        """Persist one cancellation intent; never stop an execution or release capacity."""
        identifier(job_id)
        if workspace_id is not None:
            identifier(workspace_id)
        key_digest = None if idempotency_key is None else idempotency_digest(idempotency_key)
        request_digest = canonical_digest({"operation": "cancel_job", "job_id": job_id})
        result = self._store.cancel(
            job_id,
            workspace_id=workspace_id,
            request_id=request_id,
            key_digest=key_digest,
            request_digest=request_digest,
            now_us=self._now_us(),
        )
        if result.outcome == "not_found":
            raise PublicJobNotFoundError
        if result.outcome == "conflict":
            raise IdempotencyConflictError
        if result.outcome == "cleanup_pending":
            raise JobCleanupPendingError
        if result.outcome == "unknown":
            raise JobOutcomeUnknownError
        if result.job is None or result.receipt is None:
            raise JobOutcomeUnknownError
        if (
            result.job.job_id != job_id
            or result.receipt.job_id != job_id
            or (workspace_id is not None and result.job.workspace_id != workspace_id)
        ):
            raise JobOutcomeUnknownError
        return CancellationView(result.job, result.receipt, result.outcome == "accepted")

    def settle_cancelled(self, job_id: str, *, expected_version: int) -> PublicJob:
        """Close the accepted cancellation only after a verified external stop."""
        identifier(job_id)
        current = self._store.settle_cancelled(
            job_id, expected_version=expected_version, now_us=self._now_us()
        )
        if current is None:
            current = self._store.get(job_id)
            if current is None:
                raise PublicJobUnavailableError
            if current.state == "cancelled":
                return current
            raise IdempotencyConflictError
        return current

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
    def _binding_reference(value: Any) -> tuple[str, int] | None:
        reference = PublicJobs._reference(value)
        if reference is not None and (
            set(value) != {"id", "revision"}
            or re.fullmatch(r"[1-9][0-9]{0,18}", value["revision"]) is None
        ):
            raise ValueError("Invalid model binding reference")
        return reference

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
        if kind == "voice_validation":
            for name in ("voice_draft_id", "draft_revision", "validation_set_revision"):
                if not isinstance(request_input.get(name), str):
                    raise VoiceInputInvalidError
            identifier(request_input["voice_draft_id"])
            for name in ("draft_revision", "validation_set_revision"):
                if _REVISION.fullmatch(request_input[name]) is None:
                    raise VoiceInputInvalidError
            return
        if kind == "voice_preview":
            for name in ("voice_draft_id", "draft_revision", "text"):
                if not isinstance(request_input.get(name), str):
                    raise VoiceInputInvalidError
            identifier(request_input["voice_draft_id"])
            if _REVISION.fullmatch(request_input["draft_revision"]) is None:
                raise VoiceInputInvalidError
            if not 1 <= len(request_input["text"]) <= 2000:
                raise VoiceInputInvalidError
            return
        if kind in ("voice_design", "voice_clone"):
            language = request_input.get("language")
            if language is not None and (
                not isinstance(language, str) or not language.strip() or len(language) > 35
            ):
                raise VoiceInputInvalidError
        if kind == "voice_clone":
            asset_id = request_input.get("reference_audio_asset_id")
            if (
                request_input.get("source_use_confirmed") is not True
                or not isinstance(asset_id, str)
                or self._facts.asset(workspace_id, asset_id) != ("ready", "voice_reference")
            ):
                raise AssetNotReadyError
            return
        if kind == "voice_design":
            return
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


def _retransmission_result(result: JobCreateResult, parent_job_id: str) -> PublicJob:
    """A repeated retry key replays only the replacement it created for this parent."""
    job = _submission_result(result)
    if job.retry_of_job_id != parent_job_id:
        raise IdempotencyConflictError
    return job


def _replayed_revision(
    result: JobCreateResult, draft_id: str, store: PublicJobStore
) -> tuple[PublicJob, str]:
    if result.outcome == "conflict":
        raise IdempotencyConflictError
    if result.job is None:
        raise JobOutcomeUnknownError
    revision = store.revision_for_job(draft_id, result.job.job_id)
    if revision is None:
        raise JobOutcomeUnknownError
    return result.job, revision


def _rejection_reason(error_code: str | None) -> JobRejectionReason:
    """Classify one store refusal; an unknown code is not a client conflict."""
    if error_code == "job_rejected":
        return "facts_changed"
    if error_code == "revision_exhausted":
        return "revision_exhausted"
    if error_code == "job_identity_conflict":
        return "identity_conflict"
    if error_code == "storage_rejected":
        return "storage_rejected"
    return "contract"


def _revision_result(result: RevisionCreateResult) -> tuple[PublicJob, str]:
    if result.outcome == "stale":
        raise VoiceDraftStaleRevisionError
    if result.outcome == "not_found":
        raise PublicJobNotFoundError
    if result.outcome == "conflict":
        raise IdempotencyConflictError
    if result.outcome == "unknown":
        raise JobOutcomeUnknownError
    if result.outcome == "rejected":
        raise JobRejectedError(_rejection_reason(result.error_code))
    if result.job is None or result.revision is None:
        raise JobRejectedError("contract")
    return result.job, result.revision


def _submission_result(result: JobCreateResult) -> PublicJob:
    if result.outcome == "conflict":
        raise IdempotencyConflictError
    if result.outcome == "unknown":
        raise JobOutcomeUnknownError
    if result.outcome == "rejected":
        if result.error_code == "stale":
            raise VoiceDraftStaleRevisionError
        if result.error_code == "not_candidate":
            raise VoiceDraftNotCandidateError
        if result.error_code == "draft_not_generatable":
            raise VoiceDraftNotGeneratableError
        if result.error_code == "not_found":
            raise PublicJobNotFoundError
        raise JobRejectedError(_rejection_reason(result.error_code))
    if result.job is None:
        raise JobRejectedError("contract")
    return result.job


def _canonical_intent(payload: Mapping[str, Any], kind: JobKind) -> dict[str, Any]:
    """Apply only contract defaults so omitted and explicit defaults share one key."""
    intent = deepcopy(dict(payload))
    intent.setdefault("workspace_id", DEFAULT_WORKSPACE)
    if kind in ("asr", "tts"):
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
    "CancellationView",
    "DisabledWorkspaceError",
    "IdempotencyConflictError",
    "JobCleanupPendingError",
    "JobFacts",
    "JobOutcomeUnknownError",
    "JobRejectedError",
    "JobRetryNotAllowedError",
    "JobRetryUnavailableError",
    "ModelBindingError",
    "ModelNotPublishedError",
    "ProfileNotPublishedError",
    "PronunciationNotFoundError",
    "PublicJobStore",
    "PublicJobs",
    "UnknownWorkspaceError",
    "UnsupportedJobKindError",
    "VoiceDraftNotCandidateError",
    "VoiceDraftRevisionFact",
    "VoiceDraftStaleRevisionError",
    "VoiceInputInvalidError",
    "VoiceNotPublishedError",
    "require_pinned_binding",
]
