"""Worker shell: settings aliases, one-pass mode, and bounded interval handling."""

from __future__ import annotations

import json
from threading import Event
from typing import Literal, cast

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
from speechnexus_backend.speech.infrastructure.postgres_pool import SpeechConnection, SpeechPool
from speechnexus_backend.speech.infrastructure.voice_publication_schema import EVIDENCE_TABLE
from speechnexus_backend.voice_evidence_worker import (
    _RECORDER_TABLES,
    VoiceEvidenceWorkerSettings,
    _log_batch,
    _verify_columns,
    _verify_foreign_schema,
    _verify_recorder_worker,
    _verify_restricted_role,
    _WorkerRuntime,
    main,
    run_worker,
)


class _Pending:
    def __init__(self, jobs: list[str]) -> None:
        self.jobs = [(index + 1, job) for index, job in enumerate(jobs)]

    def pending(self, limit: int, after: tuple[int, str] | None = None) -> list[tuple[int, str]]:
        return [row for row in self.jobs if after is None or row > after][:limit]


class _FailingPending:
    def pending(self, limit: int, after: tuple[int, str] | None = None) -> list[tuple[int, str]]:
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


class _Cursor:
    def __init__(self, rows: list[dict[str, object]], one: dict[str, object] | None) -> None:
        self._rows, self._one = rows, one

    def execute(self, *args: object, **kwargs: object) -> _Cursor:
        return self

    def fetchall(self) -> list[dict[str, object]]:
        return list(self._rows)

    def fetchone(self) -> dict[str, object] | None:
        return self._one


class _Connection:
    def __init__(self, *cursors: _Cursor) -> None:
        self._cursors = list(cursors)

    def __enter__(self) -> _Connection:
        return self

    def __exit__(self, *exc: object) -> Literal[False]:
        return False

    def execute(self, *args: object, **kwargs: object) -> _Cursor:
        return self._cursors.pop(0)


def _connection(*cursors: _Cursor) -> SpeechConnection:
    return cast(SpeechConnection, _Connection(*cursors))


class _Pool:
    def __init__(self, connection: _Connection) -> None:
        self._connection = connection
        self.opened = False
        self.closed = False

    def connection(self, timeout: float | None = None) -> _Connection:
        return self._connection

    def open(self) -> None:
        self.opened = True

    def close(self) -> None:
        self.closed = True


def _column_row(table: str, **flags: bool) -> dict[str, object]:
    row: dict[str, object] = {
        "table_name": table,
        "column_name": "column",
        "can_read": True,
        "can_insert": table == EVIDENCE_TABLE,
        "can_update": False,
        "can_reference": False,
        "any_column": True,
        "destructive": False,
    }
    row.update(flags)
    return row


def _all_rows() -> list[dict[str, object]]:
    return [_column_row(name) for name in _RECORDER_TABLES]


def _settings(monkeypatch: pytest.MonkeyPatch) -> VoiceEvidenceWorkerSettings:
    monkeypatch.setenv("SPEECHNEXUS_VOICE_EVIDENCE_DATABASE_URL", "postgresql://worker@example/db")
    monkeypatch.setenv("SPEECHNEXUS_DATABASE_SCHEMA", "speech")
    monkeypatch.setenv("SPEECHNEXUS_OBJECT_STORAGE_ENDPOINT", "http://127.0.0.1:9000")
    monkeypatch.setenv("SPEECHNEXUS_OBJECT_STORAGE_BUCKET", "synthetic-artifacts")
    monkeypatch.setenv("SPEECHNEXUS_OBJECT_STORAGE_ACCESS_KEY", "access")
    monkeypatch.setenv("SPEECHNEXUS_OBJECT_STORAGE_SECRET_KEY", "secret")
    return VoiceEvidenceWorkerSettings()


def test_worker_verifies_every_read_table_and_the_evidence_writer() -> None:
    _verify_columns(_connection(_Cursor(_all_rows(), None)), "recorder_role", "speech")
    # A missing table means the role is not the documented shape.
    rows = _all_rows()
    rows.pop()
    with pytest.raises(ValueError):
        _verify_columns(_connection(_Cursor(rows, None)), "recorder_role", "speech")
    # Each widening is detected on its own.
    for table, flags in (
        ("speech_jobs", {"can_update": True}),
        ("speech_jobs", {"can_reference": True}),
        ("speech_jobs", {"can_read": False}),
        ("speech_jobs", {"can_insert": True}),
        (EVIDENCE_TABLE, {"can_insert": False}),
        (EVIDENCE_TABLE, {"destructive": True}),
    ):
        rows = _all_rows()
        next(row for row in rows if row["table_name"] == table).update(flags)
        with pytest.raises(ValueError):
            _verify_columns(_connection(_Cursor(rows, None)), "recorder_role", "speech")
    # An unrelated table must not even be readable.
    rows = _all_rows()
    rows.append(_column_row("speech_publications", can_read=True, can_insert=False))
    with pytest.raises(ValueError):
        _verify_columns(_connection(_Cursor(rows, None)), "recorder_role", "speech")
    unrelated = _column_row("speech_publications", can_read=False, can_insert=False)
    unrelated["any_column"] = False
    rows = _all_rows()
    rows.append(unrelated)
    _verify_columns(_connection(_Cursor(rows, None)), "recorder_role", "speech")


def test_worker_refuses_privileges_outside_its_schema() -> None:
    _verify_foreign_schema(_connection(_Cursor([], None)), "recorder_role", "speech")
    with pytest.raises(ValueError):
        _verify_foreign_schema(
            _connection(_Cursor([], {"foreign_privilege": 1})), "recorder_role", "speech"
        )


def test_worker_rejects_owner_identities() -> None:
    connection = _Connection(_Cursor([], {"forbidden": False}))
    _verify_restricted_role(cast(SpeechConnection, connection), "recorder_role", "speech")
    for forbidden in (True, None):
        connection = _Connection(_Cursor([], None if forbidden is None else {"forbidden": True}))
        with pytest.raises(ValueError):
            _verify_restricted_role(cast(SpeechConnection, connection), "recorder_role", "speech")


def test_worker_checks_login_and_effective_identity_once_each(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        "speechnexus_backend.voice_evidence_worker._verify_no_role_switch",
        lambda connection, label: None,
    )
    monkeypatch.setattr(
        "speechnexus_backend.voice_evidence_worker._verify_restricted_role",
        lambda connection, role, schema: calls.append(("restricted", role)),
    )
    monkeypatch.setattr(
        "speechnexus_backend.voice_evidence_worker._verify_columns",
        lambda connection, role, schema: calls.append(("columns", role)),
    )
    monkeypatch.setattr(
        "speechnexus_backend.voice_evidence_worker._verify_foreign_schema",
        lambda connection, role, schema: calls.append(("foreign", role)),
    )
    pool = _Pool(
        _Connection(
            _Cursor([], None),
            _Cursor([], {"login": "recorder_role", "effective": "recorder_role"}),
        )
    )
    _verify_recorder_worker(cast(SpeechPool, pool), "speech")
    assert calls == [
        ("restricted", "recorder_role"),
        ("columns", "recorder_role"),
        ("foreign", "recorder_role"),
    ]


def test_runtime_opens_and_closes_owned_resources(monkeypatch: pytest.MonkeyPatch) -> None:
    pool = _Pool(_Connection())
    monkeypatch.setattr(
        "speechnexus_backend.voice_evidence_worker.speech_pool",
        lambda *args, **kwargs: pool,
    )
    monkeypatch.setattr(
        "speechnexus_backend.voice_evidence_worker.MinioArtifactObjects",
        lambda *args: type("Objects", (), {"close": lambda self: None})(),
    )
    monkeypatch.setattr(
        "speechnexus_backend.voice_evidence_worker._verify_recorder_worker",
        lambda pool, schema: None,
    )
    runtime = _WorkerRuntime(_settings(monkeypatch))
    runtime.open()
    assert runtime.worker is not None and pool.opened
    with pytest.raises(ValueError):
        runtime.open()
    runtime.close()
    assert pool.closed and runtime.worker is None


def test_runtime_turns_a_rejected_startup_into_one_stable_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool = _Pool(_Connection())
    monkeypatch.setattr(
        "speechnexus_backend.voice_evidence_worker.speech_pool",
        lambda *args, **kwargs: pool,
    )
    monkeypatch.setattr(
        "speechnexus_backend.voice_evidence_worker.MinioArtifactObjects",
        lambda *args: type("Objects", (), {"close": lambda self: None})(),
    )

    def reject(pool: object, schema: str) -> None:
        raise RuntimeError("privileges do not match")

    monkeypatch.setattr("speechnexus_backend.voice_evidence_worker._verify_recorder_worker", reject)
    with pytest.raises(ValueError, match="startup rejected"):
        _WorkerRuntime(_settings(monkeypatch)).open()
    assert pool.closed


def test_run_worker_returns_immediately_when_stopped() -> None:
    stop = Event()
    stop.set()
    batches: list[EvidenceRecordingBatch] = []
    run_worker(_worker(), stop, interval_seconds=1, on_batch=batches.append)
    assert batches == []
