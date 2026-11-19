"""The admission watch logs a change, stays quiet on a repeat and wakes once."""

from __future__ import annotations

import logging
from typing import Any

import pytest
from pydantic import ValidationError

from speechnexus_backend.admission_watch_runtime import (
    AdmissionWatchRuntime,
    AdmissionWatchSettings,
    build_admission_watch,
)
from speechnexus_backend.admission_watch_worker import (
    EXIT_ADMITTING,
    EXIT_DEGRADED,
    EXIT_UNAVAILABLE,
    exit_code,
    main,
)
from speechnexus_backend.speech.application.admission_watch import (
    DEPLOYMENT_CAUSES,
    EVENT_ALERTED,
    EVENT_CHANGED,
    EVENT_CLEARED,
    EVENT_STARTED,
    WORKSPACE_SCOPED_CAUSES,
    AdmissionWatcher,
    AdmissionWatchObservation,
)
from speechnexus_backend.speech.domain.job_admission_readiness import (
    AdmissionReadiness,
    AdmissionReadinessCause,
    admitted_readiness,
    refused_readiness,
)
from speechnexus_backend.speech.domain.public_jobs import PublicJobUnavailableError

WORKSPACE = "workspace-watch"
LOGGER = "speechnexus_backend.speech.application.admission_watch"


def _report(cause: AdmissionReadinessCause | None, *, now_us: int = 1_000) -> AdmissionReadiness:
    if cause is None:
        return admitted_readiness(workspace_id=WORKSPACE, now_us=now_us, instances=(), quota=None)
    return refused_readiness(cause, workspace_id=WORKSPACE, now_us=now_us)


class _ScriptedService:
    """Answer each poll from a script; the last entry repeats forever."""

    def __init__(self, script: list[AdmissionReadiness | BaseException]) -> None:
        self.script = script
        self.calls = 0

    def admission_readiness(self, workspace_id: str) -> AdmissionReadiness:
        assert workspace_id == WORKSPACE
        item = self.script[min(self.calls, len(self.script) - 1)]
        self.calls += 1
        if isinstance(item, BaseException):
            raise item
        return item


def _watcher(
    script: list[AdmissionReadiness | BaseException], *, alert_after: int = 3
) -> AdmissionWatcher:
    return AdmissionWatcher(
        _ScriptedService(script),
        workspaces=(WORKSPACE,),
        interval_seconds=30,
        alert_after=alert_after,
    )


def _records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [record for record in caplog.records if record.name == LOGGER]


def _events(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [str(getattr(record, "event", "")) for record in _records(caplog)]


def _extra(record: logging.LogRecord, name: str) -> Any:
    return getattr(record, name)


def test_one_line_per_change_and_none_for_a_repeated_verdict(
    caplog: pytest.LogCaptureFixture,
) -> None:
    watcher = _watcher(
        [
            _report(None),
            _report(None),
            _report(AdmissionReadinessCause.CAPACITY_EXHAUSTED),
            _report(AdmissionReadinessCause.CAPACITY_EXHAUSTED),
            _report(None),
        ],
        alert_after=3,
    )
    caplog.set_level(logging.INFO, logger=LOGGER)
    observations = [watcher.poll()[0] for _ in range(5)]
    assert _events(caplog) == [EVENT_STARTED, EVENT_CHANGED, EVENT_CHANGED]
    assert [observation.changed for observation in observations] == [
        False,
        False,
        True,
        False,
        True,
    ]
    first, second = _records(caplog)[1:]
    assert _extra(first, "workspace_id") == WORKSPACE
    assert _extra(first, "from") == {"status": "admitting", "cause": None}
    assert _extra(first, "to") == {"status": "degraded", "cause": "capacity_exhausted"}
    assert isinstance(_extra(first, "detail"), str) and _extra(first, "detail")
    assert _extra(second, "from") == {"status": "degraded", "cause": "capacity_exhausted"}
    assert _extra(second, "to") == {"status": "admitting", "cause": None}


def test_a_change_of_cause_is_a_change(caplog: pytest.LogCaptureFixture) -> None:
    watcher = _watcher(
        [
            _report(AdmissionReadinessCause.ENGINE_RECEIPT_EXPIRED),
            _report(AdmissionReadinessCause.CAPACITY_EXHAUSTED),
        ]
    )
    caplog.set_level(logging.INFO, logger=LOGGER)
    watcher.poll()
    watcher.poll()
    assert _events(caplog) == [EVENT_STARTED, EVENT_CHANGED]
    change = _records(caplog)[-1]
    assert _extra(change, "from") == {
        "status": "degraded",
        "cause": "engine_receipt_expired",
    }
    assert _extra(change, "to") == {"status": "degraded", "cause": "capacity_exhausted"}


def test_the_alert_fires_once_per_streak_and_clears(caplog: pytest.LogCaptureFixture) -> None:
    watcher = _watcher(
        [
            *(_report(AdmissionReadinessCause.ENGINE_RECEIPT_EXPIRED) for _ in range(5)),
            _report(None),
        ],
        alert_after=3,
    )
    caplog.set_level(logging.INFO, logger=LOGGER)
    observations = [watcher.poll()[0] for _ in range(6)]
    assert [observation.consecutive_degraded for observation in observations] == [1, 2, 3, 4, 5, 0]
    assert _events(caplog) == [EVENT_STARTED, EVENT_ALERTED, EVENT_CHANGED, EVENT_CLEARED]
    alert = _records(caplog)[1]
    assert alert.levelno == logging.WARNING
    assert _extra(alert, "workspace_id") == WORKSPACE
    assert _extra(alert, "cause") == "engine_receipt_expired"
    assert _extra(alert, "consecutive_degraded") == 3
    assert _extra(alert, "alert_after") == 3
    assert _extra(alert, "interval_seconds") == 30
    assert _extra(alert, "since_us") == 1_000
    cleared = _records(caplog)[-1]
    assert _extra(cleared, "status") == "admitting"
    assert _extra(cleared, "cause") is None


def test_a_workspace_scoped_refusal_never_wakes_anyone(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The criterion's cause filter, pinned: a workspace refusal is not a page."""
    watcher = _watcher(
        [_report(AdmissionReadinessCause.WORKSPACE_DISABLED) for _ in range(6)],
        alert_after=2,
    )
    caplog.set_level(logging.INFO, logger=LOGGER)
    observations = [watcher.poll()[0] for _ in range(6)]
    assert _events(caplog) == [EVENT_STARTED]
    assert [observation.alerted for observation in observations] == [False] * 6
    assert [observation.consecutive_degraded for observation in observations] == [0] * 6
    # The same shape does alert on a deployment refusal, so the absence above is the
    # classification rather than a criterion that can never fire.
    caplog.clear()
    engine = _watcher(
        [_report(AdmissionReadinessCause.ENGINE_RECEIPT_EXPIRED) for _ in range(2)],
        alert_after=2,
    )
    engine.poll()
    engine.poll()
    assert _events(caplog) == [EVENT_STARTED, EVENT_ALERTED]
    assert _extra(_records(caplog)[-1], "workspace_id") == WORKSPACE


def test_an_unreadable_report_is_the_declared_storage_cause(
    caplog: pytest.LogCaptureFixture,
) -> None:
    watcher = _watcher([_report(None), PublicJobUnavailableError()], alert_after=2)
    caplog.set_level(logging.INFO, logger=LOGGER)
    watcher.poll()
    watcher.poll()
    watcher.poll()
    assert _events(caplog) == [EVENT_STARTED, EVENT_CHANGED, EVENT_ALERTED]
    change = _records(caplog)[1]
    assert _extra(change, "to") == {"status": "degraded", "cause": "storage_unavailable"}
    assert _extra(_records(caplog)[-1], "cause") == "storage_unavailable"


def test_every_cause_is_classified_as_workspace_or_deployment() -> None:
    assert WORKSPACE_SCOPED_CAUSES.isdisjoint(DEPLOYMENT_CAUSES)
    assert set(AdmissionReadinessCause) == WORKSPACE_SCOPED_CAUSES | DEPLOYMENT_CAUSES
    assert {cause.value for cause in WORKSPACE_SCOPED_CAUSES} == {
        "workspace_not_found",
        "workspace_disabled",
        "workspace_not_configured",
    }
    assert {cause.value for cause in DEPLOYMENT_CAUSES} == {
        "engine_receipt_expired",
        "engine_state_mismatch",
        "engine_resource_disabled",
        "capacity_exhausted",
        "storage_unavailable",
    }


def _settings(**changes: Any) -> AdmissionWatchSettings:
    values: dict[str, Any] = {
        "enabled": False,
        "database_url": "postgresql://sn_job_admission:synthetic@127.0.0.1:5432/speech",
        "database_schema": "speech_test",
        "workspaces": "workspace-default",
    }
    return AdmissionWatchSettings(**dict(values, **changes))


def test_disabled_watch_creates_no_pool(monkeypatch: pytest.MonkeyPatch) -> None:
    import speechnexus_backend.admission_watch_runtime as runtime_module

    def fail(*args: object, **kwargs: object) -> object:
        raise AssertionError("a disabled watch must not open a pool")

    monkeypatch.setattr(runtime_module, "speech_pool", fail)
    assert build_admission_watch(_settings(enabled=False)) is None


def test_disabled_watch_cannot_be_constructed_directly() -> None:
    with pytest.raises(ValueError):
        AdmissionWatchRuntime(_settings(enabled=False))


def test_enabled_watch_requires_every_operating_value() -> None:
    with pytest.raises(ValidationError):
        _settings(enabled=True, database_url=None)
    with pytest.raises(ValidationError):
        _settings(enabled=True, database_schema=None)
    with pytest.raises(ValidationError):
        _settings(enabled=True, workspaces=" , ")
    with pytest.raises(ValidationError):
        _settings(enabled=True, interval_seconds=0.5)
    with pytest.raises(ValidationError):
        _settings(enabled=True, interval_seconds=3601)
    with pytest.raises(ValidationError):
        _settings(enabled=True, alert_after=0)
    with pytest.raises(ValidationError):
        _settings(enabled=True, alert_after=61)


def test_the_switch_defaults_to_off_and_the_cadence_is_bounded() -> None:
    settings = AdmissionWatchSettings(
        database_url="postgresql://sn_job_admission:synthetic@127.0.0.1:5432/speech",
        database_schema="speech_test",
        workspaces=" workspace-a , workspace-b ",
    )
    assert settings.enabled is False
    assert settings.interval_seconds == 60.0
    assert settings.alert_after == 3
    assert settings.workspace_ids() == ("workspace-a", "workspace-b")


def test_the_watcher_rejects_an_unbounded_configuration() -> None:
    service = _ScriptedService([_report(None)])
    with pytest.raises(ValueError):
        AdmissionWatcher(service, workspaces=(), interval_seconds=30, alert_after=3)
    with pytest.raises(ValueError):
        AdmissionWatcher(service, workspaces=(WORKSPACE,), interval_seconds=0, alert_after=3)
    with pytest.raises(ValueError):
        AdmissionWatcher(service, workspaces=(WORKSPACE,), interval_seconds=30, alert_after=0)


def test_the_entrypoint_does_nothing_at_all_when_disabled(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    for key in (
        "SPEECHNEXUS_ADMISSION_WATCH_ENABLED",
        "SPEECHNEXUS_ADMISSION_WATCH_INTERVAL_SECONDS",
        "SPEECHNEXUS_ADMISSION_WATCH_ALERT_AFTER",
        "SPEECHNEXUS_ADMISSION_WATCH_WORKSPACES",
        "SPEECHNEXUS_ADMISSION_WATCH_TRANSACTION_TIMEOUT_SECONDS",
        "SPEECHNEXUS_JOB_ADMISSION_DATABASE_URL",
        "SPEECHNEXUS_DATABASE_SCHEMA",
    ):
        monkeypatch.delenv(key, raising=False)
    main([])
    captured = capsys.readouterr()
    assert captured.out == "admission watch disabled\n"
    assert captured.err == ""


def test_the_entrypoint_names_the_missing_key_when_enabled(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("SPEECHNEXUS_ADMISSION_WATCH_ENABLED", "true")
    monkeypatch.delenv("SPEECHNEXUS_JOB_ADMISSION_DATABASE_URL", raising=False)
    monkeypatch.setenv("SPEECHNEXUS_DATABASE_SCHEMA", "speech")
    with pytest.raises(SystemExit) as exit_info:
        main([])
    assert exit_info.value.code == EXIT_UNAVAILABLE
    assert "SPEECHNEXUS_JOB_ADMISSION_DATABASE_URL" in capsys.readouterr().err


def test_the_one_shot_exit_code_follows_the_deployment_level_refusal() -> None:
    admitting = AdmissionWatchObservation(WORKSPACE, _report(None), False, 0, False)
    workspace_scoped = AdmissionWatchObservation(
        WORKSPACE, _report(AdmissionReadinessCause.WORKSPACE_DISABLED), True, 0, False
    )
    deployment = AdmissionWatchObservation(
        WORKSPACE, _report(AdmissionReadinessCause.ENGINE_RECEIPT_EXPIRED), True, 1, False
    )
    assert exit_code((admitting,)) == EXIT_ADMITTING
    # The same classification the alert uses: a workspace-scoped refusal is that
    # workspace's own administrative state, so a timer must not read it as "wake".
    assert exit_code((workspace_scoped,)) == EXIT_ADMITTING
    assert exit_code((admitting, workspace_scoped, deployment)) == EXIT_DEGRADED


class _PerWorkspaceService:
    """Answer each workspace from its own script; the last entry repeats forever.

    Two workspaces in one watcher are the shape most likely to leak state: a single
    counter shared by both would attribute one workspace's streak to the other, and
    a single "last verdict" would log the wrong ``workspace_id``.
    """

    def __init__(self, scripts: dict[str, list[AdmissionReadiness]]) -> None:
        self.scripts = scripts
        self.calls = dict.fromkeys(scripts, 0)

    def admission_readiness(self, workspace_id: str) -> AdmissionReadiness:
        script = self.scripts[workspace_id]
        item = script[min(self.calls[workspace_id], len(script) - 1)]
        self.calls[workspace_id] += 1
        return item


def test_two_workspaces_keep_their_own_streaks_and_their_own_ids(
    caplog: pytest.LogCaptureFixture,
) -> None:
    other = "workspace-other"
    service = _PerWorkspaceService(
        {
            WORKSPACE: [
                refused_readiness(
                    AdmissionReadinessCause.ENGINE_RECEIPT_EXPIRED,
                    workspace_id=WORKSPACE,
                    now_us=1_000,
                )
            ],
            other: [
                refused_readiness(
                    AdmissionReadinessCause.WORKSPACE_DISABLED,
                    workspace_id=other,
                    now_us=1_000,
                )
            ],
        }
    )
    watcher = AdmissionWatcher(
        service, workspaces=(WORKSPACE, other), interval_seconds=30, alert_after=2
    )
    caplog.set_level(logging.INFO, logger=LOGGER)
    polls = [watcher.poll() for _ in range(4)]

    # The deployment-level streak advances for one workspace only; the
    # workspace-scoped refusal never counts, however many times it repeats.
    assert [[item.consecutive_degraded for item in poll] for poll in polls] == [
        [1, 0],
        [2, 0],
        [3, 0],
        [4, 0],
    ]
    assert [[item.alerted for item in poll] for poll in polls] == [
        [False, False],
        [True, False],
        [True, False],
        [True, False],
    ]
    records = _records(caplog)
    assert [(_extra(record, "event"), _extra(record, "workspace_id")) for record in records] == [
        (EVENT_STARTED, WORKSPACE),
        (EVENT_STARTED, other),
        (EVENT_ALERTED, WORKSPACE),
    ]
    alerted = records[2]
    assert _extra(alerted, "consecutive_degraded") == 2
    assert _extra(alerted, "cause") == "engine_receipt_expired"
    for record in records:
        assert _extra(record, "workspace_id") in {WORKSPACE, other}
