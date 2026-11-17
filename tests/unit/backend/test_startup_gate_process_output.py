"""Start each entry point for real and assert the failure shape it promises (#928).

``#920``'s review (#926) found the API entry point documented as printing one named
``FAIL:`` line while the real process printed a traceback and exited ``3``. The test
that was supposed to guard it patched ``uvicorn.run`` and never started a process, so
nothing failed. These cases close that gap for every unit whose documents promise an
output shape: each one runs the real entry point as a subprocess with a deliberately
broken environment, then asserts on the stream an operator would read.

What is proven per case:

- the entry point *exits* (a timeout is a failure, not a skip);
- the promised shape holds exactly: one `FAIL:` line (or one line per missing item),
  or the one literal non-`FAIL` line the document quotes;
- no ``Traceback`` appears anywhere in stdout or stderr;
- no deployment input leaks: no DSN fragment, password, host, port or database name.
  Key names, bucket names, schema names and table names are the documented repair
  surface and are expected in these lines.

PostgreSQL is not required here: an unreachable DSN exercises the same refusal path a
missing database does, and the API's synchronous settlement gate (which needs a real
empty schema) lives in ``tests/integration/test_startup_gate_output_pg.py``.

Every case is named in ``tests/ci/startup_gates.py``; ``tests/ci/
test_startup_gate_output_map.py`` refuses a row whose promised shape is not backed by
a case that exists in this module.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
GATE = ROOT / "deploy/job-startup-gate.py"
WORKBENCH_GATE = ROOT / "deploy/workbench-startup-gate.py"
CURSOR_KEY = "k" * 40
UNREACHABLE_SERVING = "postgresql://sn_workbench_runtime:pw_secret@127.0.0.1:1/sn892"
UNREACHABLE_CLEANUP = "postgresql://sn_workbench_cleanup:pw_secret@127.0.0.1:1/sn892"
# Values that must never appear in a gate's output: a DSN fragment, its password, its
# host or port, and the database name. `sn_*` role names are the documented
# provisioning identities and are allowed.
FORBIDDEN_FRAGMENTS = (
    "postgresql://",
    "pw_secret",
    "127.0.0.1",
    "/sn892",
)
PASSTHROUGH = ("PATH", "HOME", "TMPDIR", "LANG", "LC_ALL", "SYSTEMROOT")


def run_entry(
    arguments: list[str], environment: dict[str, str], *, timeout: float = 120
) -> subprocess.CompletedProcess[str]:
    """Run one real entry point in a clean environment and capture both streams."""
    inherited = {name: os.environ[name] for name in PASSTHROUGH if name in os.environ}
    inherited.update(environment)
    return subprocess.run(
        [sys.executable, *arguments],
        cwd=ROOT,
        env=inherited,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def both_streams(result: subprocess.CompletedProcess[str]) -> str:
    return result.stdout + result.stderr


def fail_lines(result: subprocess.CompletedProcess[str]) -> list[str]:
    """Every `FAIL:` line, with the gate's leading indentation removed."""
    return [
        line.strip()
        for line in both_streams(result).splitlines()
        if line.strip().startswith("FAIL:")
    ]


def assert_no_traceback(result: subprocess.CompletedProcess[str]) -> None:
    assert "Traceback" not in both_streams(result), both_streams(result)[-2000:]


def assert_no_leak(result: subprocess.CompletedProcess[str]) -> None:
    text = both_streams(result)
    leaked = [fragment for fragment in FORBIDDEN_FRAGMENTS if fragment in text]
    assert leaked == [], f"the gate echoed a deployment input: {leaked}"


# --------------------------------------------------------------------------- api


def test_asset_inspector_reports_one_line_and_no_traceback() -> None:
    """`async-job-runbook.md` pins the exact line and exit code for a blank token."""
    result = run_entry(["-m", "speechnexus_asset_inspector.main"], {})

    assert result.returncode == 1
    assert_no_traceback(result)
    lines = [line for line in both_streams(result).splitlines() if line.strip()]
    assert lines[0] == (
        "asset inspector configuration error: "
        "SPEECHNEXUS_INSPECTION_TOKEN must be a non-empty bearer token"
    )
    assert fail_lines(result) == []


# --------------------------------------------------- job-dispatch-worker entry gate

BLANK_JOB_GATE = {"SPEECHNEXUS_JOB_DISPATCH_KINDS": "asr"}


def test_job_dispatch_gate_names_missing_prerequisites_without_a_traceback() -> None:
    """One named line per missing prerequisite, exit 1, and no worker is started."""
    result = run_entry([str(GATE)], BLANK_JOB_GATE)

    assert result.returncode == 1
    assert_no_traceback(result)
    assert "job dispatch startup gate: deployment prerequisites before any claim" in (result.stdout)
    lines = fail_lines(result)
    assert len(lines) >= 3
    assert len(set(lines)) == len(lines), "each missing prerequisite is named once"
    assert any("SPEECHNEXUS_JOB_DISPATCH_DATABASE_URL" in line for line in lines)
    assert "refusing to start speechnexus-job-dispatch-worker" in result.stdout
    assert "job dispatch startup gate: OK" not in result.stdout
    assert_no_leak(result)


def test_job_dispatch_gate_refuses_an_unsafe_schema_instead_of_crashing() -> None:
    """A schema that is not a safe identifier used to escape as a ValueError traceback.

    The gate promised one named line per missing prerequisite, then crashed in the
    first check that used the schema. The case pins the repair: the gate names the
    schema key and still never starts the worker.
    """
    result = run_entry(
        [str(GATE)],
        {
            **BLANK_JOB_GATE,
            "SPEECHNEXUS_DATABASE_URL": "postgresql://sn_job_dispatch:pw@127.0.0.1:1/sn892",
            "SPEECHNEXUS_DATABASE_SCHEMA": "bad-schema!",
        },
    )

    assert result.returncode == 1
    assert_no_traceback(result)
    schema_lines = [line for line in fail_lines(result) if "SPEECHNEXUS_DATABASE_SCHEMA" in line]
    assert len(schema_lines) == 1
    assert "safe lowercase SQL identifier" in schema_lines[0]
    assert "refusing to start speechnexus-job-dispatch-worker" in result.stdout
    assert_no_leak(result)


def test_job_dispatch_gate_connect_failure_names_no_server_or_login() -> None:
    """A refused connection is one named line that echoes no address, database or user."""
    result = run_entry(
        [str(GATE)],
        {
            **BLANK_JOB_GATE,
            "SPEECHNEXUS_DATABASE_URL": "postgresql://sn_job_dispatch:pw_secret@127.0.0.1:1/sn892",
            "SPEECHNEXUS_DATABASE_SCHEMA": "speech",
        },
        timeout=60,
    )

    assert result.returncode == 1
    assert_no_traceback(result)
    connect_lines = [line for line in fail_lines(result) if "cannot connect to PostgreSQL" in line]
    assert len(connect_lines) == 1
    assert "SPEECHNEXUS_JOB_DISPATCH_DATABASE_URL" in connect_lines[0]
    # psycopg reports no SQLSTATE for a connect failure, so the type name is the only
    # classification a gate can print without echoing the driver's text (#928 M2).
    assert "(OperationalError)" in connect_lines[0]
    assert (
        "the dispatch role, the database name or the database network is not ready"
        in (connect_lines[0])
    )
    # A psycopg message names the server address and, on an auth failure, the login
    # name; the gate must classify the failure instead of echoing it.
    assert_no_leak(result)


# ------------------------------------------------------- workbench-migrate entry


def test_workbench_migrate_names_missing_inputs_without_a_traceback() -> None:
    """`compose-minimal.md` §6: one `FAIL:` line per named condition, exit 1."""
    result = run_entry(["-m", "speechnexus_backend.workbench.migrate"], {})

    assert result.returncode == 1
    assert_no_traceback(result)
    lines = fail_lines(result)
    assert len(lines) == 3
    assert "SPEECHNEXUS_MIGRATION_DATABASE_URL is blank" in lines[0]
    assert result.stdout == ""


def test_workbench_migrate_names_an_unreachable_owner_without_echoing_the_dsn() -> None:
    """The driver's own text can carry the DSN, so the migration reports its type only."""
    result = run_entry(
        ["-m", "speechnexus_backend.workbench.migrate"],
        {
            "SPEECHNEXUS_MIGRATION_DATABASE_URL": "postgresql://sn_owner:pw_secret@127.0.0.1:1/sn892",
            "SPEECHNEXUS_WORKBENCH_DATABASE_URL": "postgresql://sn_wb:pw_secret@127.0.0.1:1/sn892",
            "SPEECHNEXUS_WORKBENCH_CLEANUP_DATABASE_URL": (
                "postgresql://sn_wb_cleanup:pw_secret@127.0.0.1:1/sn892"
            ),
        },
        timeout=60,
    )

    assert result.returncode == 1
    assert_no_traceback(result)
    lines = fail_lines(result)
    assert len(lines) == 1
    assert "the owner connection could not complete the workbench migration" in lines[0]
    assert "(OperationalError)" in lines[0]
    assert_no_leak(result)


# --------------------------------------------------------- workbench-api entry gate


def test_workbench_gate_names_missing_inputs_without_a_traceback() -> None:
    """A blank deployment gets one `FAIL:` line per input, exit 1, and no server."""
    result = run_entry([str(WORKBENCH_GATE)], {})

    assert result.returncode == 1
    assert_no_traceback(result)
    lines = fail_lines(result)
    assert len(lines) == 5
    assert result.stdout == ""
    assert_no_leak(result)


def test_workbench_gate_refuses_a_runtime_check_failure_in_one_named_line() -> None:
    """The runtime preflight runs before `exec`, so its refusal is still one line."""
    result = run_entry(
        [str(WORKBENCH_GATE)],
        {
            "SPEECHNEXUS_WORKBENCH_DATABASE_URL": UNREACHABLE_SERVING,
            "SPEECHNEXUS_WORKBENCH_CLEANUP_DATABASE_URL": UNREACHABLE_CLEANUP,
            "SPEECHNEXUS_WORKBENCH_WORKSPACE_ID": "workspace-default",
            "SPEECHNEXUS_WORKBENCH_DEPLOYMENT_ID": "deployment-local",
            "SPEECHNEXUS_WORKBENCH_CURSOR_KEY": CURSOR_KEY,
            "SPEECHNEXUS_WORKBENCH_DATABASE_SCHEMA": "workbench",
        },
        timeout=60,
    )

    assert result.returncode == 1
    assert_no_traceback(result)
    lines = fail_lines(result)
    assert len(lines) == 1
    assert lines[0].startswith("FAIL: the workbench runtime database was refused at startup:")
    assert result.stdout == ""
    assert_no_leak(result)


@pytest.mark.parametrize(
    "entry",
    [
        [str(GATE)],
        [str(WORKBENCH_GATE)],
        ["-m", "speechnexus_backend.workbench.migrate"],
    ],
    ids=["job-dispatch", "workbench-api", "workbench-migrate"],
)
def test_every_gate_failure_is_cheap_enough_for_a_startup_case(entry: list[str]) -> None:
    """The cases above must be runnable in CI: a gate refuses in seconds, not minutes."""
    result = run_entry(entry, {}, timeout=30)

    assert result.returncode == 1
    assert_no_traceback(result)


# ------------------------------------------------ job-dispatch-worker entry point

WORKER = "speechnexus_backend.job_dispatch_worker"
WORKER_SETTINGS = {
    "SPEECHNEXUS_PROCESS_ROLE": "job-worker",
    "SPEECHNEXUS_NODE_ID": "node-local-1",
    "SPEECHNEXUS_INSTANCE_ID": "instance-local-1",
    # A closed port: the settings validate, then the planner/publisher assembly fails
    # exactly as it would against a database that is not ready.
    "SPEECHNEXUS_DATABASE_URL": "postgresql://sn_job_dispatch:pw_secret@127.0.0.1:1/sn892",
    "SPEECHNEXUS_DATABASE_SCHEMA": "speech",
    "SPEECHNEXUS_EXECUTION_GATEWAY_BASE_URL": "http://127.0.0.1:1",
    "SPEECHNEXUS_EXECUTION_GATEWAY_COMPONENT_CREDENTIAL": "0123456789abcdef",
    "SPEECHNEXUS_EXECUTION_GATEWAY_TIMEOUT_SECONDS": "5",
    "SPEECHNEXUS_JOB_DISPATCH_BATCH_SIZE": "4",
    "SPEECHNEXUS_JOB_DISPATCH_LEASE_MS": "30000",
    "SPEECHNEXUS_JOB_DISPATCH_POLL_SECONDS": "1",
    "SPEECHNEXUS_JOB_DISPATCH_JOB_TIMEOUT_SECONDS": "60",
    "SPEECHNEXUS_JOB_DISPATCH_MAX_ATTEMPTS": "3",
    "SPEECHNEXUS_JOB_DISPATCH_INTERVAL_SECONDS": "1",
    "SPEECHNEXUS_JOB_DISPATCH_STORAGE_TIMEOUT_SECONDS": "5",
    "SPEECHNEXUS_OBJECT_STORAGE_ENDPOINT": "http://127.0.0.1:1",
    "SPEECHNEXUS_OBJECT_STORAGE_BUCKET": "speechnexus-results",
    "SPEECHNEXUS_EXECUTION_RESULT_BUCKET": "speechnexus-results",
    "SPEECHNEXUS_OBJECT_STORAGE_ACCESS_KEY": "access",
    "SPEECHNEXUS_OBJECT_STORAGE_SECRET_KEY": "secret",
}


def test_job_dispatch_worker_refuses_without_settings_in_one_line() -> None:
    """The worker's own entry point names its refusal instead of dumping a traceback."""
    result = run_entry(["-m", WORKER], {})

    assert result.returncode == 1
    assert_no_traceback(result)
    assert result.stdout == ""
    assert result.stderr == "Job dispatch unavailable; no lease was claimed.\n"
    assert_no_leak(result)


def test_job_dispatch_worker_separates_an_unassembled_planner_with_exit_two() -> None:
    """A validated configuration whose planner cannot be built is a different refusal."""
    result = run_entry(["-m", WORKER], WORKER_SETTINGS, timeout=60)

    assert result.returncode == 2
    assert_no_traceback(result)
    assert result.stderr == (
        "Job dispatch unavailable: planner and result publisher are not assembled; "
        "no lease was claimed.\n"
    )
    assert_no_leak(result)
