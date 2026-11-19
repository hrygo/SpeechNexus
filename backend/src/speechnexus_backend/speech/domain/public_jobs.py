"""Public Job lifecycle and immutable submission facts.

The coordination tables carry dispatch identity; this module only models the
public read model. A state may advance, but a terminal state never reopens and a
success cannot exist without a stored result.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Literal, get_args

import rfc8785

from speechnexus_backend.speech.domain.publications import Publication

JobState = Literal[
    "pending", "running", "cancelling", "succeeded", "failed", "cancelled", "expired"
]
JobKind = Literal["asr", "tts", "voice_design", "voice_clone", "voice_preview", "voice_validation"]
ProgressUnit = Literal["audio_ms", "text_codepoints", "items"]

# The closed public Job vocabulary, in contract declaration order. Every kind
# view in this module is derived from this tuple or from ``JOB_KIND_PROFILES``
# below, so a kind cannot be added to one reader here only. The dispatch side is
# a separate vocabulary with a different axis: ``domain/job_dispatch.py`` still
# owns ``DispatchJobKind`` and its ``DISPATCHABLE_JOB_KINDS`` /
# ``VOICE_GENERATION_JOB_KINDS`` / ``VOICE_VALIDATION_JOB_KINDS`` tuples. Those
# are not projections of this table and are not derived from it here (#1040
# follow-up).
JOB_KINDS: tuple[JobKind, ...] = get_args(JobKind)


@dataclass(frozen=True, slots=True)
class JobKindProfile:
    """One Job kind's public-Job derivations; the readers in this module read it.

    ``task`` is the binding task the kind itself declares. It is ``None`` for
    ``voice_preview`` on purpose: a preview runs on its target draft's task, and
    that layer is ``VOICE_DRAFT_JOB_KINDS`` rather than a second copy of this
    column. ``model_slot`` is the key under ``effective_configuration.models``;
    the voice family names ``voice`` explicitly instead of stripping a prefix,
    so a future ``voice_*`` kind must declare its slot here. ``progress_unit``
    is ``items`` for the voice family (one produced item per unit of progress)
    and ``audio_ms`` otherwise. ``command_kind`` is the stored command kind
    admission freezes; a kind without one keeps the legacy path and is therefore
    outside the admission universe. ``pinned`` says the create may carry an
    expected binding: ``tts`` accepts an explicit reference, while the voice
    kinds derive it from the target draft.
    """

    task: str | None
    model_slot: str
    progress_unit: ProgressUnit
    command_kind: str | None
    pinned: bool


JOB_KIND_PROFILES: dict[JobKind, JobKindProfile] = {
    "asr": JobKindProfile(
        task="asr_file",
        model_slot="asr",
        progress_unit="audio_ms",
        command_kind="asr",
        pinned=False,
    ),
    "tts": JobKindProfile(
        task="tts_file",
        model_slot="tts",
        progress_unit="audio_ms",
        command_kind="tts",
        pinned=True,
    ),
    "voice_design": JobKindProfile(
        task="voice_design",
        model_slot="voice",
        progress_unit="items",
        command_kind=None,
        pinned=False,
    ),
    "voice_clone": JobKindProfile(
        task="voice_clone",
        model_slot="voice",
        progress_unit="items",
        command_kind=None,
        pinned=False,
    ),
    "voice_preview": JobKindProfile(
        task=None,
        model_slot="voice",
        progress_unit="items",
        command_kind=None,
        pinned=True,
    ),
    "voice_validation": JobKindProfile(
        task="voice_validation",
        model_slot="voice",
        progress_unit="items",
        command_kind="voiceclone",
        pinned=True,
    ),
}

# Draft kind (``speech_voice_drafts.kind``) -> the Job kind that drafts it. A
# preview and a validation resolve their task through this mapping, and a
# revision append resolves the inverse; both directions are one relation.
VOICE_DRAFT_JOB_KINDS: dict[str, JobKind] = {
    "design": "voice_design",
    "clone": "voice_clone",
}
VOICE_JOB_DRAFT_KINDS: dict[JobKind, str] = {
    kind: draft_kind for draft_kind, kind in VOICE_DRAFT_JOB_KINDS.items()
}

# The task a binding must declare for each Job kind that has one of its own.
# ``require_pinned_binding`` and the unpinned ``latest_binding`` resolution both
# check it, and the readiness report's admissible-binding filter reads this same
# map instead of a second copy. ``voice_preview`` is absent because its task is
# the draft's (see ``VOICE_DRAFT_JOB_KINDS``).
JOB_KIND_TASK: dict[JobKind, str] = {
    kind: profile.task for kind, profile in JOB_KIND_PROFILES.items() if profile.task is not None
}

# The kinds ``build_public_job_admission`` turns into a complete command, i.e.
# exactly the kinds with a stored command kind. The other Job kinds keep the
# legacy path and are outside the readiness verdict's universe: no admission
# transaction reads them.
ADMITTED_JOB_KINDS: tuple[JobKind, ...] = tuple(
    kind for kind, profile in JOB_KIND_PROFILES.items() if profile.command_kind is not None
)

# The kinds whose create may carry an expected binding.
PINNED_JOB_KINDS: frozenset[JobKind] = frozenset(
    kind for kind, profile in JOB_KIND_PROFILES.items() if profile.pinned
)

CleanupState = Literal["not_required", "pending", "complete", "quarantined"]

JOB_STATES: tuple[JobState, ...] = (
    "pending",
    "running",
    "cancelling",
    "succeeded",
    "failed",
    "cancelled",
    "expired",
)
TERMINAL_STATES = frozenset({"succeeded", "failed", "cancelled", "expired"})
# A retry only replaces an execution that is already frozen. These states never
# transition again, so an observed retryable state cannot change underneath the
# replacement.
RETRYABLE_STATES = frozenset({"failed", "cancelled", "expired"})
ARTIFACT_KINDS = (
    "transcript",
    "audio",
    "preview",
    "validation_report",
    "export_manifest",
)
_ALLOWED_TRANSITIONS: dict[JobState, frozenset[JobState]] = {
    "pending": frozenset({"running", "cancelling", "failed", "cancelled", "expired"}),
    "running": frozenset({"succeeded", "failed", "cancelling", "cancelled"}),
    "cancelling": frozenset({"cancelled", "failed"}),
    "succeeded": frozenset(),
    "failed": frozenset(),
    "cancelled": frozenset(),
    "expired": frozenset(),
}
CANCELLATION_UNCONFIRMED = "cancellation_unconfirmed"
_IDENTIFIER = re.compile(r"[a-z0-9][a-z0-9._:-]{1,126}[a-z0-9]\Z")
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
_TIMESTAMP = 253_402_300_799_999_999


class PublicJobUnavailableError(RuntimeError):
    """Persisted public Job facts could not be read or written safely.

    ``detail`` names the deployment input that makes the surface unusable when
    this build knows it -- a blank Job DSN, for example. It names the key only;
    a value never reaches the message (#877).
    """

    def __init__(self, detail: str | None = None) -> None:
        self.detail = detail
        super().__init__(detail or "")


class PublicJobNotFoundError(LookupError):
    """The requested public Job identity has no authoritative row."""


class PublicJobConflictError(RuntimeError):
    """A transition lost its status-version guard or tried to revive a terminal state."""


def identifier(value: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise ValueError("Invalid public Job identity")
    return value


def counter(value: int, *, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise ValueError("Invalid public Job counter")
    return value


def canonical_digest(value: Any) -> str:
    """One RFC 8785 digest; the caller supplies the complete canonical intent."""
    try:
        encoded = rfc8785.dumps(value)
    except ValueError, TypeError, RecursionError, OverflowError:
        raise ValueError("Invalid public Job document") from None
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def idempotency_digest(key: str) -> str:
    if not isinstance(key, str) or not 8 <= len(key) <= 128:
        raise ValueError("Invalid idempotency key")
    return "sha256:" + hashlib.sha256(key.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class JobSubmission:
    """Immutable request facts resolved once when the Job is accepted."""

    request_id: str
    client_reference: str | None
    effective_configuration_json: str
    execution_revision: int
    progress_unit: ProgressUnit
    progress_total: int | None
    submitted_at_us: int

    def __post_init__(self) -> None:
        if not isinstance(self.request_id, str) or not 1 <= len(self.request_id) <= 128:
            raise ValueError("Invalid public Job request id")
        if self.client_reference is not None and (
            not isinstance(self.client_reference, str) or len(self.client_reference) > 256
        ):
            raise ValueError("Invalid public Job client reference")
        if not isinstance(self.effective_configuration_json, str) or not (
            2 <= len(self.effective_configuration_json.encode("utf-8")) <= 65536
        ):
            raise ValueError("Invalid effective configuration")
        counter(self.execution_revision, minimum=1)
        if self.progress_unit not in ("audio_ms", "text_codepoints", "items"):
            raise ValueError("Invalid progress unit")
        if self.progress_total is not None:
            counter(self.progress_total)
        if type(self.submitted_at_us) is not int or not 0 <= self.submitted_at_us <= _TIMESTAMP:
            raise ValueError("Invalid submission timestamp")


@dataclass(frozen=True, slots=True)
class JobProgressFact:
    """The latest committed progress fact; ordered by the event sequence."""

    unit: ProgressUnit
    completed: int
    total: int | None

    def __post_init__(self) -> None:
        if self.unit not in ("audio_ms", "text_codepoints", "items"):
            raise ValueError("Invalid public Job progress unit")
        counter(self.completed)
        if self.total is not None:
            counter(self.total)
            if self.completed > self.total:
                raise ValueError("Public Job progress exceeds its total")


@dataclass(frozen=True, slots=True)
class PublicJob:
    job_id: str
    workspace_id: str
    kind: JobKind
    model_id: str
    state: JobState
    status_version: int
    result_json: str | None
    error_code: str | None
    created_at_us: int
    updated_at_us: int
    submission: JobSubmission
    retry_of_job_id: str | None = None
    # None means no progress fact has been committed; a Job projection must then
    # report the declared unit and zero completed rather than invent a value.
    progress: JobProgressFact | None = None

    def __post_init__(self) -> None:
        identifier(self.job_id)
        identifier(self.workspace_id)
        if self.kind not in JOB_KINDS:
            raise ValueError("Invalid public Job kind")
        if not isinstance(self.model_id, str) or not 1 <= len(self.model_id) <= 128:
            raise ValueError("Invalid public Job model")
        if self.state not in JOB_STATES:
            raise ValueError("Invalid public Job state")
        counter(self.status_version, minimum=1)
        if type(self.created_at_us) is not int or not 0 <= self.created_at_us <= _TIMESTAMP:
            raise ValueError("Invalid public Job creation time")
        if (
            type(self.updated_at_us) is not int
            or not self.created_at_us <= self.updated_at_us <= _TIMESTAMP
        ):
            raise ValueError("Public Job time moved backwards")
        if (self.state == "succeeded") != (self.result_json is not None):
            raise ValueError("Only a succeeded Job carries a result")
        if (self.state == "failed") != (self.error_code is not None):
            raise ValueError("Only a failed Job carries an error code")
        if self.retry_of_job_id is not None:
            identifier(self.retry_of_job_id)
            if self.retry_of_job_id == self.job_id:
                raise ValueError("A public Job cannot retry itself")
        if self.progress is not None and self.progress.unit != self.submission.progress_unit:
            raise ValueError("Public Job progress disagrees with its declared unit")


@dataclass(frozen=True, slots=True)
class NewPublicJob:
    job_id: str
    workspace_id: str
    kind: JobKind
    model_id: str
    request_digest: str
    request_document: str
    initial_event_digest: str
    idempotency_key_digest: str | None
    submission: JobSubmission
    expected_binding: Publication | None = None
    # Only a retry replacement names its frozen parent; every other Job is its
    # own origin.
    retry_of_job_id: str | None = None

    def __post_init__(self) -> None:
        identifier(self.job_id)
        identifier(self.workspace_id)
        if self.kind not in JOB_KINDS:
            raise ValueError("Invalid public Job kind")
        if not isinstance(self.model_id, str) or not 1 <= len(self.model_id) <= 128:
            raise ValueError("Invalid public Job model")
        if self.retry_of_job_id is not None:
            identifier(self.retry_of_job_id)
            if self.retry_of_job_id == self.job_id:
                raise ValueError("A public Job cannot retry itself")
        for digest in (
            self.request_digest,
            self.initial_event_digest,
            self.idempotency_key_digest,
        ):
            if digest is not None and not _DIGEST.fullmatch(digest):
                raise ValueError("Invalid public Job digest")
        if not isinstance(self.request_document, str) or not (
            2 <= len(self.request_document.encode("utf-8")) <= 262144
        ):
            raise ValueError("Invalid public Job request document")
        try:
            decoded = json.loads(self.request_document)
        except ValueError, TypeError:
            raise ValueError("Invalid public Job request document") from None
        if not isinstance(decoded, dict):
            raise ValueError("Invalid public Job request document")
        if canonical_digest(decoded) != self.request_digest:
            raise ValueError("Public Job request document disagrees with its digest")
        if ("model_binding" in decoded or self.expected_binding is not None) and (
            self.expected_binding is None
            or self.expected_binding.kind != "binding"
            or self.kind not in PINNED_JOB_KINDS
            or decoded.get("kind") != self.kind
            or decoded.get("model", self.model_id) != self.model_id
            or (
                decoded.get("model_binding") != self.expected_binding.reference
                and not (self.kind == "voice_validation" and "model_binding" not in decoded)
            )
        ):
            raise ValueError("Public Job binding disagrees with its original intent")


def check_transition(
    current: JobState,
    target: JobState,
    *,
    result_json: str | None,
    error_code: str | None,
) -> None:
    """Application-side half of the invariant; PostgreSQL checks the other half."""
    if target not in _ALLOWED_TRANSITIONS[current]:
        raise PublicJobConflictError("Public Job transition is not allowed")
    if (target == "succeeded") != (result_json is not None):
        raise PublicJobConflictError("Success requires and only success carries a result")
    if (target == "failed") != (error_code is not None):
        raise PublicJobConflictError("Failure requires and only failure carries an error code")
    if result_json is not None:
        try:
            decoded = json.loads(result_json)
        except ValueError, TypeError:
            raise PublicJobConflictError("Stored result is not JSON") from None
        if not isinstance(decoded, dict):
            raise PublicJobConflictError("Stored result must be an object")


def cleanup_state(job: PublicJob) -> CleanupState:
    """Report resource cleanup separately from the single result state."""
    if job.state == "cancelling":
        return "pending"
    if job.state == "cancelled" or job.state == "succeeded":
        return "complete"
    if job.state == "failed" and job.error_code == CANCELLATION_UNCONFIRMED:
        return "quarantined"
    return "not_required"


@dataclass(frozen=True, slots=True)
class JobCancellationReceipt:
    """The durable acceptance of one public Job cancellation."""

    job_id: str
    workspace_id: str
    operation_id: str
    attempt_id: str
    request_id: str
    request_digest: str
    idempotency_key_digest: str | None
    accepted: bool
    requested_at_us: int

    def __post_init__(self) -> None:
        identifier(self.job_id)
        identifier(self.workspace_id)
        identifier(self.operation_id)
        identifier(self.attempt_id)
        if not isinstance(self.request_id, str) or not 1 <= len(self.request_id) <= 128:
            raise ValueError("Invalid cancellation request id")
        if not _DIGEST.fullmatch(self.request_digest):
            raise ValueError("Invalid cancellation request digest")
        if self.idempotency_key_digest is not None and not _DIGEST.fullmatch(
            self.idempotency_key_digest
        ):
            raise ValueError("Invalid cancellation idempotency digest")
        if type(self.accepted) is not bool:
            raise ValueError("Invalid cancellation acceptance")
        if type(self.requested_at_us) is not int or not 0 <= self.requested_at_us <= _TIMESTAMP:
            raise ValueError("Invalid cancellation timestamp")


@dataclass(frozen=True, slots=True)
class JobCancellationResult:
    """One durable cancellation read/write outcome, never stop evidence."""

    outcome: Literal[
        "accepted", "replayed", "terminal", "cleanup_pending", "conflict", "not_found", "unknown"
    ]
    job: PublicJob | None = None
    receipt: JobCancellationReceipt | None = None


@dataclass(frozen=True, slots=True)
class JobCreateResult:
    outcome: Literal["created", "replayed", "conflict", "unknown", "rejected"]
    job: PublicJob | None = None
    error_code: str | None = None
    # The concrete missing item when the refusal class alone cannot name it (a
    # blank admission DSN, no registered engine instance). It is never a DSN value
    # and never changes the code (#877).
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class RevisionCreateResult:
    """One durable revision-append outcome, never a second revision for one key."""

    outcome: Literal["created", "replayed", "conflict", "stale", "not_found", "unknown", "rejected"]
    job: PublicJob | None = None
    revision: str | None = None
    error_code: str | None = None
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class JobIntent:
    """The immutable canonical intent kept beside one accepted public Job."""

    request_digest: str
    document: str

    def __post_init__(self) -> None:
        if not isinstance(self.request_digest, str) or not _DIGEST.fullmatch(self.request_digest):
            raise ValueError("Invalid public Job request digest")
        if not isinstance(self.document, str) or not (
            2 <= len(self.document.encode("utf-8")) <= 262144
        ):
            raise ValueError("Invalid public Job intent document")
        try:
            decoded = json.loads(self.document)
        except ValueError, TypeError:
            raise ValueError("Invalid public Job intent document") from None
        # Defense in depth, not duplication: the application retry path checks
        # the parsed payload against this digest too. Both layers are required;
        # neither may be removed as "redundant".
        if not isinstance(decoded, dict) or canonical_digest(decoded) != self.request_digest:
            raise ValueError("Public Job intent disagrees with its digest")


@dataclass(frozen=True, slots=True)
class JobEventFact:
    """One persisted event; ordering and identity come from its sequence.

    A progress row carries the observed unit and value. It is never a lifecycle
    transition, so it cannot exist for a terminal state and its value must
    strictly advance the previous progress fact of the same Job.
    """

    event_seq: int
    state: JobState
    occurred_at_us: int
    kind: Literal["lifecycle", "progress"] = "lifecycle"
    progress: JobProgressFact | None = None

    def __post_init__(self) -> None:
        counter(self.event_seq)
        if self.state not in JOB_STATES:
            raise ValueError("Invalid public Job event state")
        if type(self.occurred_at_us) is not int or not 0 <= self.occurred_at_us <= _TIMESTAMP:
            raise ValueError("Invalid public Job event time")
        if self.kind == "lifecycle":
            if self.progress is not None:
                raise ValueError("A lifecycle event carries no progress fact")
            return
        if self.kind != "progress":
            raise ValueError("Invalid public Job event kind")
        if self.state != "running" or self.progress is None:
            raise ValueError("A progress event requires a running progress fact")


@dataclass(frozen=True, slots=True)
class JobArtifactFact:
    """One committed Artifact row referenced by a succeeded Job result."""

    artifact_id: str
    kind: str
    media_type: str
    bytes: int
    sha256: str
    receipt_id: str
    expires_at_us: int | None = None

    def __post_init__(self) -> None:
        identifier(self.artifact_id)
        if self.kind not in ARTIFACT_KINDS:
            raise ValueError("Invalid Artifact kind")
        if not isinstance(self.media_type, str) or not 1 <= len(self.media_type) <= 128:
            raise ValueError("Invalid Artifact media type")
        if type(self.bytes) is not int or self.bytes < 1:
            raise ValueError("Invalid Artifact size")
        if not isinstance(self.sha256, str) or not _DIGEST.fullmatch(self.sha256):
            raise ValueError("Invalid Artifact digest")
        identifier(self.receipt_id)
        if self.expires_at_us is not None and (
            type(self.expires_at_us) is not int or not 0 < self.expires_at_us <= _TIMESTAMP
        ):
            raise ValueError("Invalid Artifact expiry")


def retryable(job: PublicJob) -> bool:
    """Only a frozen terminal Job may be replaced; a quarantined failure stays held."""
    return job.state in RETRYABLE_STATES and job.error_code != CANCELLATION_UNCONFIRMED
