"""Automatic evidence consumer: bounded retries, stable refusals, no drift."""

from __future__ import annotations

from typing import cast

import pytest

from speechnexus_backend.speech.application.voice_capability_evidence import (
    VoiceEvidenceConflictError,
    VoiceEvidenceNotEligibleError,
    VoiceEvidenceUnavailableError,
    VoiceReportUnavailableError,
)
from speechnexus_backend.speech.application.voice_evidence_recording import (
    _MAX_NOTES,
    EVIDENCE_CONFLICT,
    REPORT_UNAVAILABLE,
    STORAGE_UNAVAILABLE,
    EvidenceRecordingBatch,
    EvidenceRecordingOutcome,
    ScanCursor,
    VoiceEvidenceAutoRecorder,
)
from speechnexus_backend.speech.domain.voice_capability_evidence import VoiceCapabilityEvidence

REPORT = b'{"synthesis_path":"reusable_tts"}'
MEASURED_BY = "voice-evidence-recorder-worker/v1"


class _Pending:
    def __init__(self, jobs: list[str]) -> None:
        # Synthetic positions mirror the durable (updated_at_us, job_id) order.
        self.rows: list[ScanCursor] = [(index + 1, job) for index, job in enumerate(jobs)]
        self.limits: list[tuple[int, ScanCursor | None]] = []

    def pending(self, limit: int, after: ScanCursor | None = None) -> list[ScanCursor]:
        self.limits.append((limit, after))
        return [row for row in self.rows if after is None or row > after][:limit]


class _Reports:
    def __init__(
        self, jobs: dict[str, bytes], failures: dict[str, Exception] | None = None
    ) -> None:
        self.jobs = jobs
        self.failures = failures or {}
        self.reads: list[str] = []

    def read(self, validation_job_id: str) -> bytes:
        self.reads.append(validation_job_id)
        failure = self.failures.get(validation_job_id)
        if failure is not None:
            raise failure
        return self.jobs[validation_job_id]


class _Recorder:
    def __init__(
        self,
        created: dict[str, bool] | None = None,
        failures: dict[str, Exception] | None = None,
    ) -> None:
        self.created = created or {}
        self.failures = failures or {}
        self.calls: list[tuple[str, bytes, str]] = []

    def record(
        self, validation_job_id: str, report: bytes, measured_by: str
    ) -> tuple[VoiceCapabilityEvidence, bool]:
        self.calls.append((validation_job_id, report, measured_by))
        failure = self.failures.get(validation_job_id)
        if failure is not None:
            raise failure
        evidence = cast(VoiceCapabilityEvidence, object())
        return evidence, self.created.get(validation_job_id, True)


def _worker(
    jobs: list[str],
    reports: _Reports,
    recorder: _Recorder,
    *,
    max_attempts: int = 5,
    batch_size: int = 16,
) -> tuple[VoiceEvidenceAutoRecorder, _Pending]:
    pending = _Pending(jobs)
    return (
        VoiceEvidenceAutoRecorder(
            pending=pending,
            reports=reports,
            recorder=recorder,
            measured_by=MEASURED_BY,
            max_attempts=max_attempts,
            batch_size=batch_size,
        ),
        pending,
    )


def test_run_once_records_then_reports_the_existing_row() -> None:
    reports = _Reports({"job-1": REPORT})
    recorder = _Recorder(created={"job-1": True})
    worker, pending = _worker(["job-1"], reports, recorder)

    first = worker.run_once()
    assert first == EvidenceRecordingBatch(
        scanned=1,
        recorded=1,
        outcomes=(EvidenceRecordingOutcome("job-1", "recorded"),),
    )
    assert recorder.calls == [("job-1", REPORT, MEASURED_BY)]
    assert pending.limits == [(16, None)]

    recorder.created["job-1"] = False
    second = worker.run_once()
    assert second.existing == 1 and second.recorded == 0
    assert second.outcomes == (EvidenceRecordingOutcome("job-1", "existing"),)


def test_non_terminal_job_is_rejected_with_a_stable_reason() -> None:
    reports = _Reports({}, {"job-1": VoiceEvidenceNotEligibleError("validation_job_not_succeeded")})
    recorder = _Recorder()
    worker, _ = _worker(["job-1"], reports, recorder)

    batch = worker.run_once()
    assert batch.rejected == 1
    assert batch.outcomes[0].reason == "validation_job_not_succeeded"
    assert recorder.calls == []


def test_deterministic_refusal_is_reported_once_without_rereading() -> None:
    reports = _Reports({}, {"job-1": VoiceEvidenceNotEligibleError("coverage_probes_invalid")})
    worker, pending = _worker(["job-1"], reports, _Recorder())

    first = worker.run_once()
    second = worker.run_once()
    assert first.outcomes[0].reason == "coverage_probes_invalid"
    # The next cycle re-reports the cached refusal without re-reading the
    # immutable report; the report is only read once per process.
    assert second.outcomes[0] == first.outcomes[0]
    assert pending.limits[1] == (16, None)
    assert reports.reads == ["job-1"]


def test_conflicting_evidence_is_a_refusal_not_a_retry() -> None:
    reports = _Reports({"job-1": REPORT})
    recorder = _Recorder(failures={"job-1": VoiceEvidenceConflictError()})
    worker, _ = _worker(["job-1"], reports, recorder)

    batch = worker.run_once()
    assert batch.rejected == 1
    assert batch.outcomes[0].reason == EVIDENCE_CONFLICT
    assert reports.reads == ["job-1"]


@pytest.mark.parametrize(
    ("failure", "reason"),
    [
        (VoiceReportUnavailableError(), REPORT_UNAVAILABLE),
        (VoiceEvidenceUnavailableError(), STORAGE_UNAVAILABLE),
    ],
)
def test_retryable_failures_stop_at_the_attempt_budget(failure: Exception, reason: str) -> None:
    reports = _Reports({}, {"job-1": failure})
    worker, _ = _worker(["job-1"], reports, _Recorder(), max_attempts=3)

    outcomes = [worker.run_once().outcomes[0] for _ in range(3)]
    assert [item.outcome for item in outcomes] == ["retry", "retry", "exhausted"]
    assert {item.reason for item in outcomes} == {reason}
    assert reports.reads == ["job-1", "job-1", "job-1"]
    # The exhausted Job is reported again without another read or attempt.
    assert worker.run_once().outcomes[0].outcome == "exhausted"
    assert reports.reads == ["job-1", "job-1", "job-1"]


def test_refused_job_does_not_starve_a_later_candidate() -> None:
    jobs = ["bad", "good"]
    reports = _Reports({"good": REPORT}, {"bad": VoiceEvidenceNotEligibleError("bad_report")})
    worker, _ = _worker(jobs, reports, _Recorder(), batch_size=1)

    first = worker.run_once()
    second = worker.run_once()
    assert first.outcomes[0].job_id == "bad"
    assert second.outcomes[0] == EvidenceRecordingOutcome("good", "recorded")
    assert reports.reads == ["bad", "good"]


def test_scan_cursor_pages_then_wraps_without_an_exclusion_list() -> None:
    jobs = ["job-1", "job-2", "job-3"]
    reports = _Reports(dict.fromkeys(jobs, REPORT))
    worker, pending = _worker(jobs, reports, _Recorder(), batch_size=2)

    first = worker.run_once()
    second = worker.run_once()
    third = worker.run_once()
    assert [outcome.job_id for outcome in first.outcomes] == ["job-1", "job-2"]
    assert [outcome.job_id for outcome in second.outcomes] == ["job-3"]
    # The end of the window is not a growing stateful filter: the next pass
    # starts from the beginning of the durable order.
    assert [outcome.job_id for outcome in third.outcomes] == ["job-1", "job-2"]
    assert pending.limits == [(2, None), (2, (2, "job-2")), (2, None)]


def test_refusal_notes_stay_bounded_across_a_large_bad_backlog() -> None:
    jobs = [f"job-{index}" for index in range(_MAX_NOTES + 256)]
    reports = _Reports({}, {job: VoiceEvidenceNotEligibleError("bad_report") for job in jobs})
    worker, _ = _worker(jobs, reports, _Recorder(), batch_size=128)

    while worker.run_once().scanned:
        pass
    assert len(reports.reads) == len(jobs)
    assert len(worker._refusals) <= _MAX_NOTES
    # FIFO eviction: the oldest keys disappear and the newest stays addressable.
    assert "job-0" not in worker._refusals
    assert "job-255" not in worker._refusals
    assert "job-256" in worker._refusals
    assert "job-4351" in worker._refusals


def test_batch_size_bounds_one_pass() -> None:
    jobs = ["job-1", "job-2", "job-3"]
    reports = _Reports(dict.fromkeys(jobs, REPORT))
    worker, pending = _worker(jobs, reports, _Recorder(), batch_size=2)

    batch = worker.run_once()
    assert batch.scanned == 2
    assert pending.limits == [(2, None)]


def test_scan_coverage_does_not_depend_on_process_memory() -> None:
    reports = _Reports({"job-1": REPORT})
    first = _worker(["job-1"], reports, _Recorder())[0]
    second = _worker(["job-1"], reports, _Recorder())[0]

    assert first.run_once().recorded == 1
    # A fresh process with no in-memory candidate state still finds the Job.
    assert second.run_once().recorded == 1
    assert reports.reads == ["job-1", "job-1"]


def test_empty_scan_is_a_zero_batch() -> None:
    worker, _ = _worker([], _Reports({}), _Recorder())
    assert worker.run_once() == EvidenceRecordingBatch()


def test_bounds_and_labels_are_checked() -> None:
    with pytest.raises(ValueError):
        _worker([], _Reports({}), _Recorder(), max_attempts=0)
    with pytest.raises(ValueError):
        _worker([], _Reports({}), _Recorder(), batch_size=0)
    with pytest.raises(ValueError):
        VoiceEvidenceAutoRecorder(
            pending=_Pending([]),
            reports=_Reports({}),
            recorder=_Recorder(),
            measured_by="bad\nlabel",
        )


def test_outcome_requires_exactly_one_reason_for_refusals() -> None:
    with pytest.raises(ValueError):
        EvidenceRecordingOutcome("job-1", "rejected")
    with pytest.raises(ValueError):
        EvidenceRecordingOutcome("job-1", "recorded", "unexpected")
    with pytest.raises(ValueError):
        EvidenceRecordingOutcome("job-1", "unknown", "reason")  # type: ignore[arg-type]


def test_logs_are_bounded_stable_codes() -> None:
    assert EvidenceRecordingOutcome("job-1", "rejected", "coverage_probes_invalid").as_log() == {
        "event": "voice_evidence_rejected",
        "job_id": "job-1",
        "reason": "coverage_probes_invalid",
    }
    assert EvidenceRecordingOutcome("job-1", "recorded").as_log() == {
        "event": "voice_evidence_recorded",
        "job_id": "job-1",
    }
    assert EvidenceRecordingBatch(scanned=1, rejected=1).as_log()["event"] == "voice_evidence_batch"
