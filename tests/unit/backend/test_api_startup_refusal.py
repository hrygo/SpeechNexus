"""The API entry point refuses an unmigrated database instead of restarting.

#877 item 3: with the coordinated schema absent, the synchronous TTS preflight
used to raise an uncaught ``UndefinedTable`` (``relaton "admission_leases" does
not exist``), so the container restart-looped on a traceback with no instruction.
A missing migration is a deployment defect, and the entry point states that
refusal once: what is missing and the command that fixes it.
"""

from __future__ import annotations

from typing import Any

import pytest
import uvicorn

from speechnexus_backend import main as api_main
from speechnexus_backend.speech.infrastructure import tts_settlement_preflight as preflight
from speechnexus_backend.speech.infrastructure.job_schema_preflight import (
    JobSchemaNotMigratedError,
)


class _Settings:
    host = "127.0.0.1"
    port = 8000
    log_level = "warning"


def test_the_api_entry_point_names_a_missing_migration(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def refuse(*args: Any, **kwargs: Any) -> None:
        raise preflight.SettlementPreflightUnavailableError(
            "the settled-capacity preflight cannot read admission_leases; the "
            "migration profile has not been applied: run "
            "`docker compose --profile migration run --rm migrate` before the API"
        )

    monkeypatch.setattr(api_main, "Settings", lambda: _Settings())
    monkeypatch.setattr(uvicorn, "run", refuse)

    with pytest.raises(SystemExit) as exit_info:
        api_main.main()

    assert exit_info.value.code == 1
    stderr = capsys.readouterr().err
    assert "migration profile" in stderr
    assert "admission_leases" in stderr
    assert "Traceback" not in stderr


def test_the_job_schema_refusal_is_not_claimed_by_the_entry_point(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The lifespan refusal belongs to uvicorn, and the entry point must not pretend.

    ``JobRuntime.open()`` awaits inside the ASGI lifespan, so uvicorn logs the
    exception with its traceback and exits ``STARTUP_FAILURE`` (3) itself; the
    ``except`` in ``main()`` cannot see it. This pins that division: the entry
    point converts only the synchronous assembly refusal, and the startup answer
    for an unmigrated Job database is uvicorn's named exception, not a ``FAIL:``
    line. Measured on uvicorn 0.54.0: exit 3, ``Exception in 'lifespan' protocol``
    plus the traceback, and the migration sentence in the exception message
    (#920 review M1).
    """

    def refuse(*args: Any, **kwargs: Any) -> None:
        raise JobSchemaNotMigratedError(["alembic_version", "speech_jobs"])

    monkeypatch.setattr(api_main, "Settings", lambda: _Settings())
    monkeypatch.setattr(uvicorn, "run", refuse)

    with pytest.raises(JobSchemaNotMigratedError):
        api_main.main()

    stderr = capsys.readouterr().err
    assert "FAIL:" not in stderr
