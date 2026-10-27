"""Bounded Job dispatch decisions; gateway transport and storage stay outside."""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

DispatchState = Literal["pending", "running", "succeeded", "failed"]
GatewayOutcome = Literal["status", "not_found", "unknown", "rejected"]
ExecutionState = Literal[
    "submitting", "executing", "stopping", "succeeded", "failed", "aborted", "unknown_quarantined"
]
JobOutcome = Literal[
    "succeeded",
    "failed",
    "unknown",
    "idle",
    "claimed",
    "skipped",
    "exhausted",
    "timed_out",
    "deferred",
    "claim_lost",
]

_IDENTIFIER = re.compile(r"[a-z0-9][a-z0-9._:-]{1,126}[a-z0-9]\Z")
_ERROR_CODE = re.compile(r"[a-z][a-z0-9_]{1,62}\Z")
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
_TIMESTAMP = 253_402_300_799_999_999
_EXECUTION_STATES = frozenset(
    {
        "submitting",
        "executing",
        "stopping",
        "succeeded",
        "failed",
        "aborted",
        "unknown_quarantined",
    }
)


class DispatchUnavailableError(RuntimeError):
    """The dispatcher cannot read or persist authoritative facts safely."""


class DispatchConflictError(RuntimeError):
    """A public Job transition lost its status-version or state guard."""


class DispatchClaimLostError(DispatchConflictError):
    """The claimed lease was taken over or expired before a terminal write."""


class DispatchProtocolError(RuntimeError):
    """A gateway or stored document cannot be interpreted as the fixed contract."""


class DispatchRejectedError(DispatchProtocolError):
    """The gateway explicitly rejected the request with a stable error code."""

    def __init__(self, error_code: str) -> None:
        if not isinstance(error_code, str) or not _ERROR_CODE.fullmatch(error_code):
            raise DispatchProtocolError("Gateway returned an invalid error code") from None
        self.error_code = error_code
        super().__init__("Gateway rejected the execution request")


class DispatchRequestUnavailableError(RuntimeError):
    """The original canonical request is absent or has been redacted."""


def identifier(value: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise ValueError("Invalid dispatch identity")
    return value


def digest(value: str) -> str:
    if not isinstance(value, str) or not _DIGEST.fullmatch(value):
        raise ValueError("Invalid dispatch digest")
    return value


def submit_intent_identity(job_id: str) -> str:
    """One public Job maps to one stable gateway execution identity."""
    identifier(job_id)
    return "submit-" + hashlib.sha256(("public-job:" + job_id).encode()).hexdigest()[:48]


def result_artifact_identity(job_id: str, kind: Literal["asr", "tts"]) -> str:
    """One Job and kind publish one stable Artifact identity across replays."""
    identifier(job_id)
    if kind not in ("asr", "tts"):
        raise ValueError("Invalid result Job kind")
    return (
        "artifact-" + hashlib.sha256(f"public-job-result:{job_id}:{kind}".encode()).hexdigest()[:48]
    )


def result_receipt_identity(job_id: str) -> str:
    """A repeat publication returns the same public receipt reference."""
    identifier(job_id)
    return "receipt-" + hashlib.sha256(f"public-job-receipt:{job_id}".encode()).hexdigest()[:48]


def result_object_key(workspace_id: str, artifact_id: str) -> str:
    """A workspace-scoped, stable object key; never derived from client bytes."""
    identifier(workspace_id)
    identifier(artifact_id)
    return f"private/artifacts/{workspace_id}/{artifact_id}"


@dataclass(frozen=True, slots=True)
class DispatchLimits:
    """Every bound is explicit; no caller may extend one request indefinitely."""

    batch_size: int = 8
    lease_ms: int = 30_000
    poll_seconds: float = 1.0
    submit_timeout_seconds: float = 5.0
    job_timeout_seconds: float = 600.0
    max_attempts: int = 5

    def __post_init__(self) -> None:
        for value, lower, ceiling in (
            (self.batch_size, 1, 32),
            (self.lease_ms, 1_000, 60_000),
            (self.max_attempts, 1, 20),
        ):
            if type(value) is not int or not lower <= value <= ceiling:
                raise ValueError("Invalid dispatch integer bound")
        for seconds, limit in (
            (self.poll_seconds, 30.0),
            (self.submit_timeout_seconds, 60.0),
            (self.job_timeout_seconds, 3_600.0),
        ):
            if (
                type(seconds) not in (int, float)
                or not math.isfinite(seconds)
                or not 0.01 <= seconds <= limit
            ):
                raise ValueError("Invalid dispatch time bound")


@dataclass(frozen=True, slots=True)
class DispatchJob:
    job_id: str
    workspace_id: str
    kind: Literal["asr", "tts"]
    state: DispatchState
    status_version: int
    created_at_us: int
    request_digest: str
    request_document: str = field(repr=False)

    def __post_init__(self) -> None:
        identifier(self.job_id)
        identifier(self.workspace_id)
        if self.kind not in ("asr", "tts"):
            raise ValueError("Invalid dispatch Job kind")
        if self.state not in ("pending", "running", "succeeded", "failed"):
            raise ValueError("Invalid dispatch Job state")
        if type(self.status_version) is not int or self.status_version < 1:
            raise ValueError("Invalid dispatch status version")
        if type(self.created_at_us) is not int or not 0 <= self.created_at_us <= _TIMESTAMP:
            raise ValueError("Invalid dispatch creation time")
        digest(self.request_digest)
        if not isinstance(self.request_document, str):
            raise ValueError("Invalid dispatch request document")

    @property
    def deadline_us(self) -> int:
        return self.created_at_us + 3_600_000_000


@dataclass(frozen=True, slots=True)
class ClaimedJob:
    job: DispatchJob
    worker_id: str
    claim_version: int
    lease_expires_at_us: int

    def __post_init__(self) -> None:
        identifier(self.worker_id)
        if type(self.claim_version) is not int or self.claim_version < 1:
            raise ValueError("Invalid dispatch claim version")
        if type(self.lease_expires_at_us) is not int or self.lease_expires_at_us < 1:
            raise ValueError("Invalid dispatch lease expiry")


@dataclass(frozen=True, slots=True)
class ExecutionSubmission:
    submit_intent_id: str
    body: Mapping[str, Any] = field(repr=False)

    def __post_init__(self) -> None:
        identifier(self.submit_intent_id)
        if not isinstance(self.body, Mapping) or not self.body:
            raise ValueError("Invalid execution submission")
        if self.body.get("submit_intent_id") != self.submit_intent_id:
            raise ValueError("Execution submission identity disagrees with its body")


@dataclass(frozen=True, slots=True)
class GatewayReply:
    outcome: GatewayOutcome
    status: Mapping[str, Any] | None = None
    error_code: str | None = None

    def __post_init__(self) -> None:
        if self.outcome == "status":
            if self.status is None or self.error_code is not None:
                raise ValueError("Gateway status requires exactly one status document")
            state = self.status.get("execution_state")
            if state not in _EXECUTION_STATES:
                raise DispatchProtocolError("Gateway returned an unknown execution state")
        elif self.status is not None:
            raise ValueError("Non-status gateway reply cannot carry a status")
        if self.error_code is not None and not _ERROR_CODE.fullmatch(self.error_code):
            raise DispatchProtocolError("Gateway returned an invalid error code")


@dataclass(frozen=True, slots=True)
class ResultReceipt:
    """A staging reference reply; it carries no execution state."""

    outcome: GatewayOutcome
    document: Mapping[str, Any] | None = None
    error_code: str | None = None

    def __post_init__(self) -> None:
        if self.outcome == "status":
            if self.document is None or self.error_code is not None:
                raise ValueError("Receipt status requires exactly one document")
        elif self.document is not None:
            raise ValueError("Non-status receipt cannot carry a document")
        if self.error_code is not None and not _ERROR_CODE.fullmatch(self.error_code):
            raise DispatchProtocolError("Gateway returned an invalid error code")


@dataclass(frozen=True, slots=True)
class PublishedResult:
    artifact_id: str
    receipt_id: str

    def __post_init__(self) -> None:
        identifier(self.artifact_id)
        identifier(self.receipt_id)


@dataclass(frozen=True, slots=True)
class DispatchResult:
    job_id: str
    outcome: JobOutcome
    error_code: str | None = None


@dataclass(frozen=True, slots=True)
class DispatchBatch:
    claimed: int = 0
    succeeded: int = 0
    failed: int = 0
    unknown: int = 0
    skipped: int = 0
    deferred: int = 0
    lease_reclaimed: int = 0
    lease_lost: int = 0

    def count(self, result: DispatchResult) -> DispatchBatch:
        increments = {
            "succeeded": "succeeded",
            "failed": "failed",
            "exhausted": "failed",
            "timed_out": "failed",
            "unknown": "unknown",
            "idle": "skipped",
            "skipped": "skipped",
            "claimed": "skipped",
            "deferred": "deferred",
            "claim_lost": "lease_lost",
        }
        name = increments[result.outcome]
        return DispatchBatch(
            claimed=self.claimed,
            succeeded=self.succeeded + (name == "succeeded"),
            failed=self.failed + (name == "failed"),
            unknown=self.unknown + (name == "unknown"),
            skipped=self.skipped + (name == "skipped"),
            deferred=self.deferred + (name == "deferred"),
            lease_reclaimed=self.lease_reclaimed,
            lease_lost=self.lease_lost + (name == "lease_lost"),
        )


def terminal_result(job: DispatchJob, artifact_id: str, receipt_id: str) -> str:
    """A public result carries only a stable artifact and receipt reference."""
    identifier(artifact_id)
    identifier(receipt_id)
    key = "audio_artifact_id" if job.kind == "tts" else "transcript_artifact_id"
    return json.dumps(
        {"kind": job.kind, key: artifact_id, "receipt_id": receipt_id},
        separators=(",", ":"),
        ensure_ascii=False,
    )


def gateway_failure_code(status: Mapping[str, Any]) -> str:
    value = status.get("error_code")
    if isinstance(value, str) and _ERROR_CODE.fullmatch(value):
        return value
    return "execution_failed"


__all__ = [
    "ClaimedJob",
    "DispatchBatch",
    "DispatchClaimLostError",
    "DispatchConflictError",
    "DispatchJob",
    "DispatchLimits",
    "DispatchProtocolError",
    "DispatchRejectedError",
    "DispatchRequestUnavailableError",
    "DispatchResult",
    "DispatchUnavailableError",
    "ExecutionSubmission",
    "GatewayReply",
    "PublishedResult",
    "ResultReceipt",
    "gateway_failure_code",
    "result_artifact_identity",
    "result_object_key",
    "result_receipt_identity",
    "submit_intent_identity",
    "terminal_result",
]
