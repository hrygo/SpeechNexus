"""Worker shell: settings aliases, one-pass mode, and bounded interval handling."""

from __future__ import annotations

import json
from collections.abc import Sequence
from threading import Event
from typing import cast

import pytest

from speechnexus_backend.speech.application.voice_capability_evidence import (
    VoiceEvidenceUnavailableError,
)
from speechnexus_backend.speech.application.voice_evidence_recording import (
    EvidenceRecordingBatch,
    EvidenceRecordingOutcome,
    VoiceEvidenceAutoRecorder,
)
from speechnexus_backend.speech.domain.voice_capability_evidence import VoiceCapabilityEvidence
from speechnexus_backend.voice_evidence_worker import (
    VoiceEvidenceWorkerSettings,
    _log_batch,
    main,
    run_worker,
)


class _Pending:
    def __init__(self, jobs: list[str]) -> None:
        self.jobs = jobs

    def pending(self, limit: int, exclude: Sequence[str] = ()) -> list[str]:
        return self.jobs[:limit]


class _FailingPending:
    def pending(self, limit: int, exclude: Sequence[str] = ()) -> list[str]:
        raise VoiceEvidenceUnavailableError


class _Reports:
    def read(self, validation_job_id: str) -> bytes:
        return b"{}"


class _Recorder:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.calls = 0

    def record(
        self, validation_job_id: str, report: bytes, measured_by: str
    ) -> tuple[VoiceCapabilityEvidence, bool]:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return cast(VoiceCapabilityEvidence, object()), True


def _worker(error: Exception | None = None) -> VoiceEvidenceAutoRecorder:
    return VoiceEvidenceAutoRecorder(
        pending=_Pending(["job-1"]),
        reports=_Reports(),
        recorder=_Recorder(error),
        measured_by="voice-evidence-recorder-worker/v1",
    )


def test_settings_use_the_documented_env_aliases(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SPEECHNEXUS_VOICE_EVIDENCE_DATABASE_URL", "postgresql://worker@example/db")
    monkeypatch.setenv("SPEECHNEXUS_DATABASE_SCHEMA", "speech")
    monkeypatch.setenv("SPEECHNEXUS_OBJECT_STORAGE_ENDPOINT", "http://127.0.0.1:9000")
    monkeypatch.setenv("SPEECHNEXUS_OBJECT_STORAGE_BUCKET", "synthetic-artifacts")
    monkeypatch.setenv("SPEECHNEXUS_OBJECT_STORAGE_ACCESS_KEY", "access")
    monkeypatch.setenv("SPEECHNEXUS_OBJECT_STORAGE_SECRET_KEY", "secret")
    monkeypatch.setenv("SPEECHNEXUS_VOICE_EVIDENCE_POLL_SECONDS", "2.5")
    monkeypatch.setenv("SPEECHNEXUS_VOICE_EVIDENCE_BATCH_SIZE", "4")
    monkeypatch.setenv("SPEECHNEXUS_VOICE_EVIDENCE_MAX_ATTEMPTS", "3")

    settings = VoiceEvidenceWorkerSettings()
    assert settings.database_schema == "speech"
    assert settings.poll_seconds == 2.5
    assert settings.batch_size == 4
    assert settings.max_attempts == 3
    assert settings.artifacts_bucket == "synthetic-artifacts"
    # Secrets stay out of the default representation.
    assert "worker@example" not in repr(settings)
    assert "secret" not in repr(settings)


def test_settings_reject_unbounded_values(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SPEECHNEXUS_VOICE_EVIDENCE_DATABASE_URL", "postgresql://worker@example/db")
    monkeypatch.setenv("SPEECHNEXUS_DATABASE_SCHEMA", "speech")
    monkeypatch.setenv("SPEECHNEXUS_OBJECT_STORAGE_ENDPOINT", "http://127.0.0.1:9000")
    monkeypatch.setenv("SPEECHNEXUS_OBJECT_STORAGE_BUCKET", "synthetic-artifacts")
    monkeypatch.setenv("SPEECHNEXUS_OBJECT_STORAGE_ACCESS_KEY", "access")
    monkeypatch.setenv("SPEECHNEXUS_OBJECT_STORAGE_SECRET_KEY", "secret")
    monkeypatch.setenv("SPEECHNEXUS_VOICE_EVIDENCE_MAX_ATTEMPTS", "99")
    with pytest.raises(ValueError):
        VoiceEvidenceWorkerSettings()


def test_main_exits_on_missing_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    for variable in (
        "SPEECHNEXUS_VOICE_EVIDENCE_DATABASE_URL",
        "SPEECHNEXUS_DATABASE_SCHEMA",
        "SPEECHNEXUS_OBJECT_STORAGE_ENDPOINT",
        "SPEECHNEXUS_OBJECT_STORAGE_BUCKET",
        "SPEECHNEXUS_OBJECT_STORAGE_ACCESS_KEY",
        "SPEECHNEXUS_OBJECT_STORAGE_SECRET_KEY",
    ):
        monkeypatch.delenv(variable, raising=False)
    with pytest.raises(SystemExit) as exit_info:
        main([])
    assert exit_info.value.code == 2


def test_run_worker_once_processes_a_single_batch() -> None:
    batches: list[EvidenceRecordingBatch] = []
    run_worker(
        _worker(),
        Event(),
        interval_seconds=1,
        once=True,
        on_batch=batches.append,
    )
    assert len(batches) == 1
    assert batches[0].recorded == 1


def test_run_worker_reports_unavailable_without_looping_forever() -> None:
    stop = Event()
    unavailable: list[str] = []

    def fail() -> None:
        unavailable.append("unavailable")
        stop.set()

    worker = VoiceEvidenceAutoRecorder(
        pending=_FailingPending(),
        reports=_Reports(),
        recorder=_Recorder(),
        measured_by="voice-evidence-recorder-worker/v1",
    )
    # The pending scan is durable, so a failed scan would be retried forever
    # without the bounded interval; the fake stops after the first failure.
    run_worker(
        worker,
        stop,
        interval_seconds=1,
        on_unavailable=fail,
        on_batch=lambda batch: pytest.fail("no batch may be reported"),
    )
    assert unavailable == ["unavailable"]


def test_run_worker_once_surfaces_an_unavailable_scan() -> None:
    worker = VoiceEvidenceAutoRecorder(
        pending=_FailingPending(),
        reports=_Reports(),
        recorder=_Recorder(),
        measured_by="voice-evidence-recorder-worker/v1",
    )
    with pytest.raises(VoiceEvidenceUnavailableError):
        run_worker(worker, Event(), interval_seconds=1, once=True)


def test_log_output_is_json_with_stable_codes(capsys: pytest.CaptureFixture[str]) -> None:
    batch = EvidenceRecordingBatch(
        scanned=1,
        rejected=1,
        outcomes=(EvidenceRecordingOutcome("job-1", "rejected", "coverage_probes_invalid"),),
    )
    _log_batch(batch)
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert lines[0]["event"] == "voice_evidence_batch"
    assert lines[1] == {
        "event": "voice_evidence_rejected",
        "job_id": "job-1",
        "reason": "coverage_probes_invalid",
    }
