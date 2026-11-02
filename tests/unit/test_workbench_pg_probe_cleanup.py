"""Disposable PG data must not accumulate after completed probe runs."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "workbench_pg_cleanup_driver", ROOT / "scripts/dev-env/workbench-pg-probe.py"
)
assert SPEC is not None and SPEC.loader is not None
DRIVER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(DRIVER)


@pytest.fixture
def workspace(monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    # Match the driver's private Unix-socket path limit even under long pytest roots.
    with tempfile.TemporaryDirectory(prefix="nx-pg-", dir="/tmp") as directory:
        root = Path(directory).resolve()
        binaries = root / "bin"
        binaries.mkdir()
        for name in ("initdb", "pg_ctl", "createdb", "postgres"):
            (binaries / name).touch()
        monkeypatch.setattr(DRIVER, "ROOT", root)
        monkeypatch.setattr(
            DRIVER.sys,
            "argv",
            ["workbench-pg-probe.py", "--pg-bin", str(binaries)],
        )
        yield root


def fake_processes(
    monkeypatch: pytest.MonkeyPatch,
    root: Path,
    *,
    test_code: int = 0,
    stop_mode: str = "ok",
    init_failure: bool = False,
) -> None:
    """Fake only process execution; exercise the real CLI, lifecycle and filesystem."""

    def execute(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        executable = Path(command[0]).name
        if executable == "postgres":
            return subprocess.CompletedProcess(command, 0, "PostgreSQL synthetic", "")
        if executable == "initdb":
            data = Path(command[command.index("-D") + 1])
            data.mkdir()
            (data / "postgresql.conf").write_text("")
            (data / "synthetic-table").write_text("synthetic content")
            if init_failure:
                raise subprocess.CalledProcessError(1, command)
        elif executable == "pg_ctl":
            data = Path(command[command.index("-D") + 1])
            pid = data / "postmaster.pid"
            if "start" in command:
                pid.write_text("12345")
                (data.parent / "postgres.log").write_text("synthetic database log")
            elif "stop" in command:
                if stop_mode == "timeout":
                    raise subprocess.TimeoutExpired(command, 25)
                if stop_mode == "refused":
                    return subprocess.CompletedProcess(command, 1, "", "stop refused")
                pid.unlink()
            elif "status" in command:
                if stop_mode == "status-error":
                    pid.unlink(missing_ok=True)
                    return subprocess.CompletedProcess(command, 4, "", "status failed")
                return subprocess.CompletedProcess(command, 0 if pid.exists() else 3, "", "")
        elif executable != "createdb":
            cluster = next((root / ".runtime/team-c").glob("pg-*"))
            (cluster / "coverage.json").write_text("{}")
            return subprocess.CompletedProcess(command, test_code, "synthetic test output", "")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(DRIVER.subprocess, "run", execute)


@pytest.mark.parametrize("test_code", [0, 1])
def test_completed_probe_removes_data_and_preserves_evidence(
    workspace: Path, monkeypatch: pytest.MonkeyPatch, test_code: int
) -> None:
    fake_processes(monkeypatch, workspace, test_code=test_code)
    assert DRIVER.main() == test_code
    result = json.loads((workspace / ".runtime/team-c/c00-pg-probe.json").read_text())
    cluster = workspace / result["cluster"]
    assert not (cluster / "data").exists()
    assert (cluster / "postgres.log").read_text() == "synthetic database log"
    assert (cluster / "pytest.log").read_text() == "synthetic test output"
    assert (cluster / "coverage.json").read_text() == "{}"
    assert result["stopped"] is True and result["data_removed"] is True
    assert result["tests_passed"] is (test_code == 0)


def test_keep_data_is_explicit_and_still_stops_the_database(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_processes(monkeypatch, workspace)
    monkeypatch.setattr(DRIVER.sys, "argv", [*DRIVER.sys.argv, "--keep-data"])
    assert DRIVER.main() == 0
    result = json.loads((workspace / ".runtime/team-c/c00-pg-probe.json").read_text())
    data = workspace / result["cluster"] / "data"
    assert (data / "synthetic-table").exists() and not (data / "postmaster.pid").exists()
    assert result["stopped"] is True and result["data_removed"] is False
    assert result["data_retained_reason"] == "requested"


@pytest.mark.parametrize("stop_mode", ["refused", "timeout", "status-error"])
def test_unconfirmed_shutdown_retains_data_and_records_failure(
    workspace: Path, monkeypatch: pytest.MonkeyPatch, stop_mode: str
) -> None:
    fake_processes(monkeypatch, workspace, stop_mode=stop_mode)
    assert DRIVER.main() == 1
    result = json.loads((workspace / ".runtime/team-c/c00-pg-probe.json").read_text())
    assert (workspace / result["cluster"] / "data/synthetic-table").exists()
    assert result["stopped"] is False and result["data_removed"] is False
    assert result["data_retained_reason"] == "shutdown-unconfirmed"


def test_failed_initialization_removes_partial_data(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_processes(monkeypatch, workspace, init_failure=True)
    assert DRIVER.main() == 1
    result = json.loads((workspace / ".runtime/team-c/c00-pg-probe.json").read_text())
    assert not (workspace / result["cluster"] / "data").exists()
    assert result["failure"] == "CalledProcessError" and result["data_removed"] is True


def test_cleanup_failure_is_reported_and_fails_the_probe(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_processes(monkeypatch, workspace)

    def refuse_removal(_path: Path) -> None:
        raise PermissionError("synthetic removal refusal")

    with monkeypatch.context() as patch:
        patch.setattr(DRIVER.shutil, "rmtree", refuse_removal)
        assert DRIVER.main() == 1
    result = json.loads((workspace / ".runtime/team-c/c00-pg-probe.json").read_text())
    assert result["tests_passed"] is True and result["stopped"] is True
    assert result["data_removed"] is False
    assert result["data_retained_reason"] == "cleanup-failed"
    assert result["cleanup_failure"] == "PermissionError"
