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
from typing import Any, Literal

import rfc8785

JobState = Literal["pending", "running", "succeeded", "failed", "cancelled", "expired"]
JobKind = Literal["asr", "tts"]
ProgressUnit = Literal["audio_ms", "text_codepoints", "items"]

JOB_STATES: tuple[JobState, ...] = (
    "pending",
    "running",
    "succeeded",
    "failed",
    "cancelled",
    "expired",
)
TERMINAL_STATES = frozenset({"succeeded", "failed", "cancelled", "expired"})
_ALLOWED_TRANSITIONS: dict[JobState, frozenset[JobState]] = {
    "pending": frozenset({"running", "failed", "cancelled", "expired"}),
    "running": frozenset({"succeeded", "failed", "cancelled"}),
    "succeeded": frozenset(),
    "failed": frozenset(),
    "cancelled": frozenset(),
    "expired": frozenset(),
}
_IDENTIFIER = re.compile(r"[a-z0-9][a-z0-9._:-]{1,126}[a-z0-9]\Z")
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
_TIMESTAMP = 253_402_300_799_999_999


class PublicJobUnavailableError(RuntimeError):
    """Persisted public Job facts could not be read or written safely."""


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

    def __post_init__(self) -> None:
        identifier(self.job_id)
        identifier(self.workspace_id)
        if self.kind not in ("asr", "tts"):
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

    def __post_init__(self) -> None:
        identifier(self.job_id)
        identifier(self.workspace_id)
        if self.kind not in ("asr", "tts"):
            raise ValueError("Invalid public Job kind")
        if not isinstance(self.model_id, str) or not 1 <= len(self.model_id) <= 128:
            raise ValueError("Invalid public Job model")
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


@dataclass(frozen=True, slots=True)
class JobCreateResult:
    outcome: Literal["created", "replayed", "conflict", "unknown", "rejected"]
    job: PublicJob | None = None
    error_code: str | None = None
