"""Source approval validates immutable operator input before database access."""

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from speechnexus_backend.builtin_voice_source_approval import (
    BuiltinSourceApprovalError,
    approve_builtin_voice_source,
)


class NoDatabase:
    def _run(self, *args: Any, **kwargs: Any) -> None:
        pytest.fail("invalid input must not access the database")


def valid() -> dict[str, Any]:
    return {
        "database": "speech_test",
        "schema": "speech_test",
        "installation_id": "12345678-1234-4234-8234-123456789abc",
        "source_approval_id": "source-test",
        "voice_id": "voice-test",
        "source_revision": "source-revision-1",
        "source_digest": "sha256:" + "a" * 64,
        "native_speaker_id": "Vivian",
        "expires_at": datetime.now(UTC) + timedelta(hours=1),
    }


@pytest.mark.parametrize(
    "field,value",
    [
        ("source_digest", "passed"),
        ("source_digest", "sha256:" + "A" * 64),
        ("native_speaker_id", "Other"),
        ("source_revision", ""),
        ("source_revision", "x" * 513),
        ("source_revision", "https://user:secret@example.test/model"),
        ("source_approval_id", "invalid space"),
        ("installation_id", "not-a-uuid"),
        ("expires_at", datetime(2026, 1, 1)),
    ],
)
def test_invalid_input_rejected_before_database(field: str, value: Any) -> None:
    args = valid() | {field: value}
    with pytest.raises(BuiltinSourceApprovalError, match="invalid_request"):
        approve_builtin_voice_source(NoDatabase(), **args)  # type: ignore[arg-type]


def test_cli_rejects_submicrosecond_input_without_connecting_or_leaking(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from speechnexus_backend.approve_builtin_voice_source import main

    monkeypatch.setenv(
        "SPEECHNEXUS_SOURCE_APPROVAL_WRITER_DATABASE_URL", "postgresql://private:secret@invalid/db"
    )
    values = {
        "database": "speech_test",
        "schema": "speech_test",
        "installation-id": "12345678-1234-4234-8234-123456789abc",
        "source-approval-id": "source-test",
        "voice-id": "voice-test",
        "source-revision": "revision-1",
        "source-digest": "sha256:" + "a" * 64,
        "native-speaker-id": "Vivian",
        "expires-at": "2026-11-03T00:00:00.1234567Z",
    }
    args = [item for key, value in values.items() for item in ("--" + key, value)]
    with pytest.raises(SystemExit) as result:
        main(args)
    assert result.value.code == 2
    captured = capsys.readouterr()
    assert "secret" not in captured.err and "invalid" not in captured.out


def test_cli_rejects_missing_writer_configuration(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from speechnexus_backend.approve_builtin_voice_source import main

    monkeypatch.delenv("SPEECHNEXUS_SOURCE_APPROVAL_WRITER_DATABASE_URL", raising=False)
    values = {
        "database": "speech_test",
        "schema": "speech_test",
        "installation-id": "12345678-1234-4234-8234-123456789abc",
        "source-approval-id": "source-test",
        "voice-id": "voice-test",
        "source-revision": "revision-1",
        "source-digest": "sha256:" + "a" * 64,
        "native-speaker-id": "Vivian",
        "expires-at": "2026-11-03T00:00:00Z",
    }
    args = [item for key, value in values.items() for item in ("--" + key, value)]
    with pytest.raises(SystemExit) as result:
        main(args)
    assert result.value.code == 2
    assert "invalid source approval configuration" in capsys.readouterr().err


def _cli_args() -> list[str]:
    values = {
        "database": "speech_test",
        "schema": "speech_test",
        "installation-id": "12345678-1234-4234-8234-123456789abc",
        "source-approval-id": "source-test",
        "voice-id": "voice-test",
        "source-revision": "revision-1",
        "source-digest": "sha256:" + "a" * 64,
        "native-speaker-id": "Vivian",
        "expires-at": "2030-11-03T00:00:00Z",
    }
    return [item for key, value in values.items() for item in ("--" + key, value)]


def test_malformed_conninfo_never_reaches_pool_or_leaks(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    from speechnexus_backend.approve_builtin_voice_source import main

    marker = "synthetic_private_marker"
    monkeypatch.setenv(
        "SPEECHNEXUS_SOURCE_APPROVAL_WRITER_DATABASE_URL",
        f"postgresql://user:{marker}%ZZ@localhost/db",
    )
    monkeypatch.setattr(
        "psycopg.Connection.connect", lambda *a, **kw: pytest.fail("network reached")
    )
    with pytest.raises(SystemExit) as result:
        main(_cli_args())
    assert result.value.code == 2
    captured = capsys.readouterr()
    assert marker not in captured.out + captured.err + caplog.text


@pytest.mark.parametrize("failure_stage", ["open", "close"])
def test_pool_diagnostics_and_cleanup_do_not_leak(
    failure_stage: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    import logging

    from speechnexus_backend.approve_builtin_voice_source import main

    marker = "synthetic_private_marker"
    monkeypatch.setenv(
        "SPEECHNEXUS_SOURCE_APPROVAL_WRITER_DATABASE_URL",
        f"postgresql://user:{marker}@localhost/db",
    )

    class Pool:
        def __init__(self, name: str) -> None:
            self.name = name

        def open(self) -> None:
            if failure_stage == "open":
                logging.getLogger("psycopg.pool").warning("%s failed %s", self.name, marker)
                raise ValueError(marker)

        def close(self) -> None:
            if failure_stage == "close":
                logging.getLogger("psycopg.pool").warning("%s failed %s", self.name, marker)
                raise ValueError(marker)

    monkeypatch.setattr(
        "speechnexus_backend.approve_builtin_voice_source.speech_pool",
        lambda *a, **kw: Pool(kw["name"]),
    )
    monkeypatch.setattr(
        "speechnexus_backend.approve_builtin_voice_source.approve_builtin_voice_source",
        lambda *a, **kw: {"outcome": "recorded", "source_approval_id": "source-test"},
    )
    with pytest.raises(SystemExit) as result:
        main(_cli_args())
    assert result.value.code == 5
    captured = capsys.readouterr()
    assert marker not in captured.out + captured.err + caplog.text
    assert "Source approval database unavailable" in caplog.text
