"""Automatic capability-evidence recording for committed validation reports.

The validation lane commits an Artifact, then the public result that references it,
and only later may the evidence ledger learn about the run. Because those are
separate commits, ``artifact -> result -> evidence`` always leaves a window in
which a succeeded Job has a readable report and no evidence row. This consumer
closes that window without depending on process memory: every pass re-reads the
durable Jobs that are terminal, carry a report reference and have no evidence
row, and records each through the same recorder the operator CLI uses.

Nothing here parses the report or re-implements its quality thresholds. The
committed report is handed to :class:`~speechnexus_backend.speech.application.
voice_capability_evidence.build_evidence`, which owns capability validation,
coverage checks and the mutable-byte digest gate. This module only decides what
is worth handing over and what a refusal means for an operator:

* a deterministic refusal (job not terminal, report outside the frozen shape,
  digest or receipt mismatch, silent/truncated probe) is reported once with the
  recorder's stable reason and is never re-read in this process;
* an unavailable ledger or unreadable report may be transient, so it is retried
  on later passes, but a per-Job attempt budget stops an endless loop;
* evidence writes are bounded transactions owned by the recorder. There is no
  Job write here, so a failed ledger write cannot roll the Job back and a
  recorded row always references an already-terminal Job.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal, Protocol

from speechnexus_backend.speech.application.voice_capability_evidence import (
    VoiceEvidenceConflictError,
    VoiceEvidenceNotEligibleError,
    VoiceEvidenceUnavailableError,
    VoiceReportUnavailableError,
)
from speechnexus_backend.speech.domain.artifacts import ArtifactError, ArtifactRecord
from speechnexus_backend.speech.domain.voice_capability_evidence import (
    MAX_MEASURED_BY,
    VoiceCapabilityEvidence,
)

type EvidenceOutcomeKind = Literal["recorded", "existing", "rejected", "retry", "exhausted"]

REPORT_UNAVAILABLE = "report_artifact_unavailable"
STORAGE_UNAVAILABLE = "evidence_storage_unavailable"
EVIDENCE_CONFLICT = "evidence_conflict"


@dataclass(frozen=True, slots=True)
class EvidenceRecordingOutcome:
    """What one pass did with one Job, with a stable machine-readable reason."""

    job_id: str
    outcome: EvidenceOutcomeKind
    reason: str | None = None

    def __post_init__(self) -> None:
        if self.outcome not in ("recorded", "existing", "rejected", "retry", "exhausted"):
            raise ValueError("Invalid evidence recording outcome")
        if (self.outcome in ("rejected", "retry", "exhausted")) != (self.reason is not None):
            raise ValueError("Evidence refusal requires exactly one stable reason")
        if self.reason is not None and (not self.reason or len(self.reason) > 128):
            raise ValueError("Evidence reason must be a bounded stable code")

    def as_log(self) -> dict[str, str]:
        return {
            "event": "voice_evidence_" + self.outcome,
            "job_id": self.job_id,
            **({} if self.reason is None else {"reason": self.reason}),
        }


@dataclass(frozen=True, slots=True)
class EvidenceRecordingBatch:
    """One bounded pass; the counters are the observable progress signal."""

    scanned: int = 0
    recorded: int = 0
    existing: int = 0
    rejected: int = 0
    retried: int = 0
    exhausted: int = 0
    outcomes: tuple[EvidenceRecordingOutcome, ...] = ()

    def as_log(self) -> dict[str, int | str]:
        return {
            "event": "voice_evidence_batch",
            "scanned": self.scanned,
            "recorded": self.recorded,
            "existing": self.existing,
            "rejected": self.rejected,
            "retried": self.retried,
            "exhausted": self.exhausted,
        }


class PendingEvidenceStore(Protocol):
    """Durable scan: terminal Jobs with a report and no evidence row.

    The query must not consult process memory or a dispatch lease, so a Job that
    lost its worker between the report and the evidence commit is still found.
    """

    def pending(self, limit: int, exclude: Sequence[str] = ()) -> list[str]: ...


class CommittedReportSource(Protocol):
    """Read the committed report bytes a succeeded Job references.

    Raises :class:`VoiceReportUnavailableError` when the Artifact is missing,
    not ready, belongs to another receipt or its bytes cannot be read, and
    :class:`VoiceEvidenceNotEligibleError` when the Job is not terminal.
    """

    def read(self, validation_job_id: str) -> bytes: ...


class EvidenceRecorder(Protocol):
    """The existing append-only recorder; idempotency lives in this call."""

    def record(
        self, validation_job_id: str, report: bytes, measured_by: str
    ) -> tuple[VoiceCapabilityEvidence, bool]: ...


class ReportArtifactLocation(Protocol):
    """Resolve one succeeded Job's committed report metadata from its own rows."""

    def report(self, validation_job_id: str) -> ArtifactRecord: ...


class ReportObjectReader(Protocol):
    """Read one immutable object version; the recorder still checks its digest."""

    def read(self, artifact: ArtifactRecord) -> bytes: ...


class CommittedReportArtifacts:
    """Combine the metadata row and the versioned object into the report bytes."""

    def __init__(self, locations: ReportArtifactLocation, objects: ReportObjectReader) -> None:
        self._locations, self._objects = locations, objects

    def read(self, validation_job_id: str) -> bytes:
        artifact = self._locations.report(validation_job_id)
        try:
            return self._objects.read(artifact)
        except ArtifactError:
            # Missing, expired or unreadable bytes are a refusal, never an
            # inference from partial content.
            raise VoiceReportUnavailableError from None


class VoiceEvidenceAutoRecorder:
    """One bounded pass over the durable evidence backlog.

    Composition owns the timer. The instance keeps two small per-process notes:
    a deterministic refusal is remembered so the same immutable report is not
    re-read every pass, and a retryable failure counts against a hard budget.
    Neither note is needed for coverage: a restarted process simply re-reads the
    still-unrecorded Jobs from the database.
    """

    def __init__(
        self,
        *,
        pending: PendingEvidenceStore,
        reports: CommittedReportSource,
        recorder: EvidenceRecorder,
        measured_by: str,
        max_attempts: int = 5,
        batch_size: int = 16,
    ) -> None:
        if type(max_attempts) is not int or not 1 <= max_attempts <= 32:
            raise ValueError("Evidence recording requires a bounded attempt budget")
        if type(batch_size) is not int or not 1 <= batch_size <= 128:
            raise ValueError("Evidence recording requires a bounded batch")
        if (
            not isinstance(measured_by, str)
            or not 1 <= len(measured_by) <= MAX_MEASURED_BY
            or any(character in measured_by for character in "\r\n\t")
        ):
            raise ValueError("Invalid evidence recorder label")
        self._pending, self._reports, self._recorder = pending, reports, recorder
        self._measured_by = measured_by
        self._max_attempts, self._batch_size = max_attempts, batch_size
        self._attempts: dict[str, int] = {}
        self._refusals: dict[str, str] = {}
        self._last_retry_reason: dict[str, str] = {}

    def run_once(self) -> EvidenceRecordingBatch:
        """Scan once and consume at most ``batch_size`` Jobs.

        Jobs already reported as deterministic refusals or as exhausted stay out
        of the scan in this process, so a backlog of permanently bad reports
        cannot starve a later eligible Job. A restart re-examines them once.
        """
        settled = tuple(self._refusals) + tuple(
            job_id for job_id, attempts in self._attempts.items() if attempts >= self._max_attempts
        )
        job_ids = self._pending.pending(self._batch_size, settled)
        outcomes = tuple(self.consume(job_id) for job_id in job_ids)
        return EvidenceRecordingBatch(
            scanned=len(outcomes),
            recorded=sum(item.outcome == "recorded" for item in outcomes),
            existing=sum(item.outcome == "existing" for item in outcomes),
            rejected=sum(item.outcome == "rejected" for item in outcomes),
            retried=sum(item.outcome == "retry" for item in outcomes),
            exhausted=sum(item.outcome == "exhausted" for item in outcomes),
            outcomes=outcomes,
        )

    def consume(self, validation_job_id: str) -> EvidenceRecordingOutcome:
        """Record one Job, or report why it refuses it right now."""
        refusal = self._refusals.get(validation_job_id)
        if refusal is not None:
            # The report bytes and the Job result are immutable, so the same
            # deterministic refusal cannot become eligible without a new Job.
            return EvidenceRecordingOutcome(validation_job_id, "rejected", refusal)
        attempts = self._attempts.get(validation_job_id, 0)
        if attempts >= self._max_attempts:
            return EvidenceRecordingOutcome(
                validation_job_id,
                "exhausted",
                self._last_retry_reason.get(validation_job_id, STORAGE_UNAVAILABLE),
            )
        try:
            report = self._reports.read(validation_job_id)
            _evidence, created = self._recorder.record(validation_job_id, report, self._measured_by)
        except VoiceEvidenceNotEligibleError as error:
            reason = str(error) or "validation_not_eligible"
            self._refusals[validation_job_id] = reason
            return EvidenceRecordingOutcome(validation_job_id, "rejected", reason)
        except VoiceEvidenceConflictError:
            self._refusals[validation_job_id] = EVIDENCE_CONFLICT
            return EvidenceRecordingOutcome(validation_job_id, "rejected", EVIDENCE_CONFLICT)
        except VoiceReportUnavailableError:
            return self._retry(validation_job_id, attempts, REPORT_UNAVAILABLE)
        except VoiceEvidenceUnavailableError:
            return self._retry(validation_job_id, attempts, STORAGE_UNAVAILABLE)
        self._attempts.pop(validation_job_id, None)
        self._last_retry_reason.pop(validation_job_id, None)
        return EvidenceRecordingOutcome(validation_job_id, "recorded" if created else "existing")

    def _retry(
        self, validation_job_id: str, attempts: int, reason: str
    ) -> EvidenceRecordingOutcome:
        attempts += 1
        self._attempts[validation_job_id] = attempts
        self._last_retry_reason[validation_job_id] = reason
        if attempts >= self._max_attempts:
            return EvidenceRecordingOutcome(validation_job_id, "exhausted", reason)
        return EvidenceRecordingOutcome(validation_job_id, "retry", reason)
