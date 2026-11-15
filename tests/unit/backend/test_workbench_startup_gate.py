"""The workbench container gate must name what is missing and never start early.

``deploy/workbench-startup-gate.py`` is the ``workbench-api`` container's
entrypoint. Its output is the only explanation an operator gets before the ASGI
factory would have started, so these tests drive ``main()`` with injected
collaborators and prove the four properties that make it a gate: every blank
input is named, a refused database exits 1 with the refusal text, a failure never
``exec``s, and only a clean run prints its ``OK:`` lines and starts the server.

The gate module lives outside the importable source roots; it is loaded by path,
like ``tests/unit/test_job_startup_gate.py``.
"""

from __future__ import annotations

import asyncio
import importlib.util
import io
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest

from speechnexus_backend.workbench.runtime import RuntimeStartupError

ROOT = Path(__file__).resolve().parents[3]
GATE_SPEC = importlib.util.spec_from_file_location(
    "workbench_startup_gate", ROOT / "deploy/workbench-startup-gate.py"
)
assert GATE_SPEC is not None and GATE_SPEC.loader is not None
GATE: Any = importlib.util.module_from_spec(GATE_SPEC)
GATE_SPEC.loader.exec_module(GATE)

BASE_ENV = {
    "SPEECHNEXUS_WORKBENCH_DATABASE_URL": "postgresql://sn_workbench_runtime@postgres:5432/db",
    "SPEECHNEXUS_WORKBENCH_CLEANUP_DATABASE_URL": "postgresql://sn_workbench_cleanup@postgres/db",
    "SPEECHNEXUS_WORKBENCH_DATABASE_SCHEMA": "workbench",
    "SPEECHNEXUS_WORKBENCH_WORKSPACE_ID": "workspace-default",
    "SPEECHNEXUS_WORKBENCH_DEPLOYMENT_ID": "deployment-reference",
    "SPEECHNEXUS_WORKBENCH_CURSOR_KEY": "synthetic-32-byte-cursor-key-test-only",
}


class Recorder:
    """Capture the collaborators the gate calls, in order."""

    def __init__(self, *, error: BaseException | None = None) -> None:
        self.error = error
        self.calls: list[str] = []
        self.checked: Any = None
        self.program: str | None = None
        self.command: Sequence[str] | None = None

    def load(self, env: Mapping[str, str]) -> Any:
        self.calls.append("load")
        return object()

    async def check(self, settings: Any) -> None:
        self.calls.append("check")
        self.checked = settings
        if self.error is not None:
            raise self.error

    def exec(self, program: str, argv: Sequence[str]) -> Any:
        # `os.execvp` never returns; the fake records the program and argv and
        # returns so the assertion can check the exit code too. The signature is
        # deliberately two-argument: calling a one-argument fake here once shipped
        # a container that reached `exec` and died on "missing 1 required argument".
        self.calls.append("exec")
        self.program = program
        self.command = tuple(argv)
        return None


def run_gate(
    env: Mapping[str, str],
    *,
    recorder: Recorder,
    load: Callable[[Mapping[str, str]], Any] | None = None,
) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    code = GATE.main(
        env,
        out=out,
        err=err,
        load_settings=recorder.load if load is None else load,
        check=recorder.check,
        exec_command=recorder.exec,
    )
    return code, out.getvalue(), err.getvalue()


@pytest.mark.parametrize(
    ("key", "detail"),
    [
        (
            "SPEECHNEXUS_WORKBENCH_DATABASE_URL",
            "the serving identity has no database role",
        ),
        (
            "SPEECHNEXUS_WORKBENCH_CLEANUP_DATABASE_URL",
            "the deletion routes and the ledger replay stay unavailable",
        ),
        ("SPEECHNEXUS_WORKBENCH_WORKSPACE_ID", "the default workspace"),
        ("SPEECHNEXUS_WORKBENCH_DEPLOYMENT_ID", "the deployment identity"),
        ("SPEECHNEXUS_WORKBENCH_CURSOR_KEY", "list cursors cannot be signed"),
    ],
)
def test_a_blank_input_is_named_and_never_starts(key: str, detail: str) -> None:
    env = {name: value for name, value in BASE_ENV.items() if name != key}
    recorder = Recorder()
    code, out, err = run_gate(env, recorder=recorder)
    assert code == 1
    assert recorder.calls == [], "a blank input must fail before settings or exec"
    assert f"FAIL: {key} is blank" in err
    assert detail in err
    assert "OK:" not in out


def test_the_speech_schema_is_refused_by_name() -> None:
    recorder = Recorder()
    code, _, err = run_gate(
        {**BASE_ENV, "SPEECHNEXUS_WORKBENCH_DATABASE_SCHEMA": "speech"},
        recorder=recorder,
    )
    assert code == 1
    assert recorder.calls == []
    assert "SPEECHNEXUS_WORKBENCH_DATABASE_SCHEMA is `speech`" in err


def test_a_refused_runtime_identity_exits_before_the_server_starts() -> None:
    refusal = RuntimeStartupError(
        "runtime role must not hold privileges on speech.speech_jobs; "
        "the workbench identity is scoped to its own schema"
    )
    recorder = Recorder(error=refusal)
    code, out, err = run_gate(BASE_ENV, recorder=recorder)
    assert code == 1
    assert recorder.calls == ["load", "check"]
    assert recorder.command is None, "a refused database must not exec the server"
    assert "FAIL: the workbench runtime database was refused at startup: " in err
    assert "speech.speech_jobs" in err
    assert "OK:" not in out


def test_an_unexpected_failure_reports_its_type_only() -> None:
    recorder = Recorder(error=RuntimeError("postgresql://user:secret@db/leak"))
    code, _, err = run_gate(BASE_ENV, recorder=recorder)
    assert code == 1
    assert recorder.command is None
    assert "FAIL: the workbench runtime database check failed (RuntimeError)" in err
    assert "the two workbench DSNs" in err
    assert "secret" not in err and "postgresql://" not in err


def test_a_refused_configuration_never_reaches_the_database() -> None:
    recorder = Recorder()

    def refuse(env: Mapping[str, str]) -> Any:
        recorder.calls.append("load")
        raise ValueError("SPEECHNEXUS_WORKBENCH_CURSOR_KEY: value is too short")

    code, _, err = run_gate(BASE_ENV, recorder=recorder, load=refuse)
    assert code == 1
    assert recorder.calls == ["load"]
    assert "refused at load (ValueError)" in err
    assert "SPEECHNEXUS_WORKBENCH_" in err
    assert recorder.command is None


def test_a_clean_run_prints_each_ok_and_starts_the_factory() -> None:
    recorder = Recorder()
    code, out, err = run_gate(BASE_ENV, recorder=recorder)
    assert code == 0
    assert recorder.calls == ["load", "check", "exec"]
    assert recorder.program == GATE.API_COMMAND[0]
    assert recorder.command == tuple(GATE.API_COMMAND)
    assert recorder.command[0] == "python", "argv must repeat the program"
    assert err == ""
    assert out.splitlines() == [
        "OK: runtime and cleanup identities hold exactly their declared surface",
        "OK: workbench schema is at the revision this build expects",
        "OK: starting the workbench ASGI factory",
    ]


def test_the_exec_command_is_the_documented_factory_and_port() -> None:
    # The runbook and deploy/README.md print the same command; a gate that execs
    # something else would start a different composition root than documented.
    assert "speechnexus_backend.workbench.runtime:create_app_from_env" in GATE.API_COMMAND
    assert "--factory" in GATE.API_COMMAND
    assert GATE.API_COMMAND[GATE.API_COMMAND.index("--port") + 1] == "8000"
    assert "uvicorn" in GATE.API_COMMAND


def test_the_real_check_runs_both_production_judgments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The gate must call the same two checks the lifespan calls, not a copy."""
    from speechnexus_backend.workbench import runtime as runtime_module

    assert GATE.check_database is runtime_module.check_database
    assert GATE.check_cleanup_identity is runtime_module.check_cleanup_identity
    calls: list[str] = []

    async def database(settings: Any) -> None:
        calls.append("database")

    async def cleanup(settings: Any) -> None:
        calls.append("cleanup")

    monkeypatch.setattr(GATE, "check_database", database)
    monkeypatch.setattr(GATE, "check_cleanup_identity", cleanup)
    asyncio.run(GATE._checks(object()))
    assert calls == ["database", "cleanup"]
