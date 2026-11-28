"""The preflight refusal names its object, and the standalone release exists.

``#1299``: the API's startup preflight refused to start while a lease was held, named
nothing, and every settlement exit was an API route -- so the refusal locked the only
service that could clear it. Two things are guarded here:

* the refusal names ``lease_id``/``operation_id`` and a command that does not need the
  API (``speechnexus-quarantine-release``);
* that command exists as a console script, refuses to write without ``--confirm``, and
  refuses when the operation does not resolve to exactly one unsettled lease.

Mutations that must turn this red:

- dropping the identity from the refusal message (back to "reconcile first") -- the
  message test fails;
- removing the console-script entry -- the entry-point test fails;
- making ``--check`` write -- the read-only test fails (the fake service records calls);
- guessing a lease when two are unsettled -- the ambiguity test fails.
"""

from __future__ import annotations

import tomllib
import unittest
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[3]
PYPROJECT = ROOT / "backend/pyproject.toml"
PREFLIGHT = (
    ROOT / "backend/src/speechnexus_backend/speech/infrastructure/tts_settlement_preflight.py"
)


class _Cursor:
    def __init__(self, rows: list[dict[str, Any]] | None = None, row: Any = None) -> None:
        self._rows = rows or []
        self._row = row

    def fetchall(self) -> list[dict[str, Any]]:
        return self._rows

    def fetchone(self) -> Any:
        return self._row


class _PreflightConnection:
    """Answers the preflight's reads: a clock row, then one held lease."""

    def __init__(self, row: dict[str, Any]) -> None:
        self.row = row

    def execute(self, query: str, parameters: tuple[object, ...] = ()) -> _Cursor:
        if "now" in query.lower() and "clock" in query.lower():
            from datetime import UTC, datetime

            return _Cursor(row={"now": datetime(2026, 11, 28, 10, 0, tzinfo=UTC)})
        if query.strip().startswith("SELECT l.operation_id"):
            return _Cursor(row=self.row)
        return _Cursor()


class _PreflightPool:
    def __init__(self, row: dict[str, Any]) -> None:
        self.row = row

    def connection(self, timeout: float) -> Any:
        connection = _PreflightConnection(self.row)

        class _Scope:
            def __enter__(self) -> _PreflightConnection:
                return connection

            def __exit__(self, *_: object) -> None:
                return None

        return _Scope()


HELD_LEASE = {
    "operation_id": "operation-abc",
    "attempt_id": "attempt-abc",
    "lease_id": "lease-abc",
}


class PreflightRefusalTests(unittest.TestCase):
    """The refusal must name the row and a command that works without the API."""

    def test_the_refusal_names_the_lease_and_a_recovery_command(self) -> None:
        from speechnexus_backend.speech.infrastructure.tts_settlement_preflight import (
            require_complete_tts_roots,
        )

        with self.assertRaises(ValueError) as raised:
            require_complete_tts_roots(_PreflightPool(HELD_LEASE))  # type: ignore[arg-type]
        message = str(raised.exception)
        self.assertIn("operation_id=operation-abc", message)
        self.assertIn("lease_id=lease-abc", message)
        self.assertIn("attempt_id=attempt-abc", message)
        self.assertIn("speechnexus-quarantine-release --operation operation-abc --confirm", message)

    def test_a_clear_database_raises_nothing(self) -> None:
        from speechnexus_backend.speech.infrastructure.tts_settlement_preflight import (
            require_complete_tts_roots,
        )

        require_complete_tts_roots(_PreflightPool(None))  # type: ignore[arg-type]

    def test_the_recovery_command_is_the_console_script(self) -> None:
        from speechnexus_backend.speech.infrastructure import tts_settlement_preflight as module

        command = module._recovery_command("job-abc")
        self.assertIn("speechnexus-quarantine-release", command)
        self.assertIn("--operation job-abc", command)
        self.assertIn("--confirm", command)


class ConsoleScriptRegistrationTests(unittest.TestCase):
    def test_the_console_script_is_declared(self) -> None:
        project = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
        scripts = project["project"]["scripts"]
        self.assertEqual(
            scripts.get("speechnexus-quarantine-release"),
            "speechnexus_backend.quarantine_release_cli:main",
        )


class _FakeConnection:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    def execute(self, query: str, parameters: tuple[object, ...] = ()) -> Any:
        if query.startswith("SET TRANSACTION"):
            return _Result([])
        return _Result(self._rows)


class _Result:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    def fetchall(self) -> list[dict[str, Any]]:
        return self._rows

    def fetchone(self) -> dict[str, Any] | None:
        return self._rows[0] if self._rows else None


class _Connection:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._connection = _FakeConnection(rows)

    def __enter__(self) -> _FakeConnection:
        return self._connection

    def __exit__(self, *_: object) -> None:
        return None


class _FakePool:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.opened = False
        self.closed = False

    def open(self) -> None:
        self.opened = True

    def close(self) -> None:
        self.closed = True

    def connection(self, timeout: float) -> _Connection:
        return _Connection(self.rows)


LEASE = {
    "lease_id": "lease-1",
    "operation_id": "job-1",
    "attempt_id": "attempt-1",
    "lease_state": "reserved",
    "cleanup_state": "pending",
}


class CliTests(unittest.TestCase):
    """``--check`` never writes; ``--confirm`` is the only write path."""

    def setUp(self) -> None:
        from speechnexus_backend import quarantine_release_cli as cli

        self.cli = cli
        self.calls: list[object] = []

    def pool_factory(self, rows: list[dict[str, Any]]) -> Any:
        pool = _FakePool(rows)

        def factory(*_: object, **__: object) -> _FakePool:
            return pool

        module: Any = self.cli
        module.speech_pool = factory
        return pool

    def release_calls(self) -> list[object]:
        from unittest import mock

        from speechnexus_backend.speech.application.quarantine_releases import (
            QuarantineReleaseService,
        )

        def recorded(service: object, evidence: object) -> object:
            self.calls.append(evidence)
            raise AssertionError("the CLI must not write without --confirm")

        patcher = mock.patch.object(QuarantineReleaseService, "release", recorded)
        self.addCleanup(patcher.stop)
        patcher.start()
        return self.calls

    def test_check_does_not_write(self) -> None:
        self.pool_factory([LEASE])
        calls = self.release_calls()
        code = self.cli.main(
            ["--operation", "job-1", "--check", "--database-url", "postgresql://unused/test"]
        )
        self.assertEqual(code, 0)
        self.assertEqual(calls, [])

    def test_without_confirm_nothing_is_released(self) -> None:
        self.pool_factory([LEASE])
        calls = self.release_calls()
        code = self.cli.main(["--operation", "job-1", "--database-url", "postgresql://unused/test"])
        self.assertEqual(code, 0)
        self.assertEqual(calls, [])

    def test_an_unknown_operation_is_refused(self) -> None:
        self.pool_factory([])
        code = self.cli.main(
            ["--operation", "job-missing", "--check", "--database-url", "postgresql://unused/test"]
        )
        self.assertEqual(code, 1)

    def test_an_ambiguous_operation_is_refused(self) -> None:
        self.pool_factory([LEASE, {**LEASE, "lease_id": "lease-2"}])
        code = self.cli.main(
            ["--operation", "job-1", "--check", "--database-url", "postgresql://unused/test"]
        )
        self.assertEqual(code, 1)

    def test_a_missing_dsn_is_an_invalid_invocation(self) -> None:
        from unittest import mock

        with mock.patch.dict("os.environ", {"SPEECHNEXUS_DATABASE_URL": ""}):
            code = self.cli.main(["--operation", "job-1", "--check"])
        self.assertEqual(code, 2)


if __name__ == "__main__":
    unittest.main()
