"""Start the API entry point against an empty schema and read the line it prints (#928).

`async-job-runbook.md` §8 promises that an unmigrated database makes the api container
print the settled-capacity refusal as one `FAIL:` line and exit `1` instead of
restart-looping on a traceback. The unit case that guarded this called the entry
function with `uvicorn.run` patched out, so it could not see either the traceback or
the exit code (#926 found the same shape on the Job schema gate).

The same file drives the api entry point twice against a freshly created, deliberately
empty PostgreSQL schema, with a valid private speech
recipe so the synchronous preflight is the first gate that refuses. It then asserts on
the bytes an operator would read: one `FAIL:` line naming the migration profile, no
`Traceback`, exit code `1`, and no deployment input (DSN, password, host, database) in
either stream.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest
import rfc8785
from tests.support.portable_pg import connect as portable_pg_connect

ROOT = Path(__file__).resolve().parents[2]
RECIPE_EXAMPLE = ROOT / "deploy/private/speech-recipe.example.json"
PASSTHROUGH = ("PATH", "HOME", "TMPDIR", "LANG", "LC_ALL", "SYSTEMROOT")
# The entry point is `speechnexus_backend.main:main`; the console script only wraps
# this call, and `-c` keeps the case independent of the installed script names.
API_ENTRY = "from speechnexus_backend.main import main; main()"


def database_url() -> str:
    url = os.environ.get("SPEECHNEXUS_TEST_DATABASE_URL")
    if not url:
        pytest.fail("Configure an isolated PostgreSQL DSN; skip is not migration evidence")
    return url


def write_recipe(directory: Path) -> Path:
    """Copy the documented example and re-sign it, so the loader accepts it."""
    document = json.loads(RECIPE_EXAMPLE.read_text(encoding="utf-8"))
    body = {key: value for key, value in document.items() if key != "recipe_digest"}
    document["recipe_digest"] = "sha256:" + hashlib.sha256(rfc8785.dumps(body)).hexdigest()
    path = directory / "speech-recipe.json"
    path.write_text(json.dumps(document, indent=2), encoding="utf-8")
    path.chmod(0o600)
    return path


def run_api(
    environment: dict[str, str], *, timeout: float = 120
) -> subprocess.CompletedProcess[str]:
    inherited = {name: os.environ[name] for name in PASSTHROUGH if name in os.environ}
    inherited.update(environment)
    return subprocess.run(
        [sys.executable, "-c", API_ENTRY],
        cwd=ROOT,
        env=inherited,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def test_api_settlement_gate_prints_one_fail_line_and_no_traceback(tmp_path: Path) -> None:
    """The synchronous preflight refuses in one line, before uvicorn can start serving."""
    url = database_url()
    schema = "sn_gate_output_" + uuid.uuid4().hex[:12]
    recipe = write_recipe(tmp_path)
    with portable_pg_connect(url, autocommit=True) as administrator:
        administrator.execute(f'CREATE SCHEMA "{schema}"')
    try:
        result = run_api(
            {
                "SPEECHNEXUS_ENVIRONMENT": "test",
                "SPEECHNEXUS_PROCESS_ROLE": "api",
                "SPEECHNEXUS_HOST": "127.0.0.1",
                # The refusal happens in synchronous assembly, so no socket is ever
                # bound; a fixed high port keeps the case from racing a real server.
                "SPEECHNEXUS_PORT": "18099",
                "SPEECHNEXUS_DATABASE_URL": url,
                "SPEECHNEXUS_DATABASE_SCHEMA": schema,
                "SPEECHNEXUS_OBJECT_STORAGE_ENDPOINT": "http://127.0.0.1:1",
                "SPEECHNEXUS_OBJECT_STORAGE_HEALTHCHECK_URL": "http://127.0.0.1:1/",
                "SPEECHNEXUS_OBJECT_STORAGE_BUCKET": "speechnexus-results",
                "SPEECHNEXUS_OBJECT_STORAGE_ACCESS_KEY": "gate-output-access",
                "SPEECHNEXUS_OBJECT_STORAGE_SECRET_KEY": "gate-output-secret",
                "SPEECHNEXUS_SPEECH_RECIPE_FILE": str(recipe),
                "SPEECHNEXUS_SPEECH_DEFAULT_LANGUAGE": "en",
                "SPEECHNEXUS_SPEECH_SETTLEMENT_DATABASE_URL": url,
                "SPEECHNEXUS_EXECUTION_GATEWAY_BASE_URL": "http://127.0.0.1:1",
                "SPEECHNEXUS_EXECUTION_GATEWAY_COMPONENT_CREDENTIAL": "gate-output-credential",
            }
        )
    finally:
        with portable_pg_connect(url, autocommit=True) as administrator:
            administrator.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')

    text = result.stdout + result.stderr
    assert result.returncode == 1, text[-2000:]
    assert "Traceback" not in text, text[-2000:]
    lines = [line.strip() for line in text.splitlines() if line.strip().startswith("FAIL:")]
    assert len(lines) == 1, lines
    assert "the settled-capacity preflight cannot read the admission tables" in lines[0]
    assert "migration profile" in lines[0]
    # The table name is the documented repair surface; the deployment input is not.
    for forbidden in ("postgresql://", schema, "gate-output-secret"):
        assert forbidden not in text, f"the api echoed {forbidden!r}"
    # The refusal is the first gate: uvicorn never reports a started server process.
    assert "Started server process" not in text


# ------------------------------------------------------- job-dispatch-worker gate

DISPATCH_GATE = ROOT / "deploy/job-startup-gate.py"


def refused_role_url(url: str) -> tuple[str, str]:
    """The same server with a login role that does not exist: a real refusal, not a fake."""
    role = "sn_absent_gate_role_" + uuid.uuid4().hex[:8]
    parsed = url.split("://", 1)
    assert len(parsed) == 2
    credentials, rest = parsed[1].split("@", 1)
    return f"{parsed[0]}://{role}:{credentials.split(':', 1)[1]}@{rest}", role


def test_job_dispatch_gate_names_a_refused_login_without_echoing_it() -> None:
    """A real driver refusal must produce one named line with no host, port, database or login.

    psycopg reports no SQLSTATE for a connect failure, and its own message quotes the
    server address and the login, so the gate classifies by exception type (#928 M2).
    The case drives the real driver against the isolated server, so the assertion is
    about an output the driver can actually produce.
    """
    url, role = refused_role_url(database_url())
    environment = {
        "SPEECHNEXUS_JOB_DISPATCH_KINDS": "asr",
        "SPEECHNEXUS_DATABASE_URL": url,
        "SPEECHNEXUS_DATABASE_SCHEMA": "speech",
    }
    inherited = {name: os.environ[name] for name in PASSTHROUGH if name in os.environ}
    inherited.update(environment)
    result = subprocess.run(
        [sys.executable, str(DISPATCH_GATE)],
        cwd=ROOT,
        env=inherited,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )

    text = result.stdout + result.stderr
    assert result.returncode == 1, text[-2000:]
    assert "Traceback" not in text, text[-2000:]
    lines = [line.strip() for line in text.splitlines() if line.strip().startswith("FAIL:")]
    connect = [line for line in lines if "cannot connect to PostgreSQL" in line]
    assert len(connect) == 1, lines
    assert "(OperationalError)" in connect[0]
    assert (
        "the dispatch role, the database name or the database network is not ready" in (connect[0])
    )
    for leaked in (role, "postgresql://", "127.0.0.1", "sn892"):
        assert leaked not in text, f"the gate echoed {leaked!r}"


# --------------------------------------------------------------- api entry point

JOB_SCHEMA_DETAIL = (
    "The Job database has not run the migration profile: a required relation is "
    "missing, so no Job can be stored. Run docker compose --profile migration run "
    "--rm migrate, then restart the api process."
)


def test_api_job_schema_gate_fails_in_the_lifespan_with_exit_three(tmp_path: Path) -> None:
    """`compose-minimal.md` §9.1: the startup Job-schema refusal is a traceback + exit 3.

    The document used to promise one `FAIL:` line here; the preflight runs inside the
    ASGI lifespan, where uvicorn ends the process with `sys.exit(3)` before the entry
    point's `except` can run. This case pins the shape the document now states, so a
    later move back into synchronous assembly has to change the document again.
    """
    url = database_url()
    schema = "sn_gate_lifespan_" + uuid.uuid4().hex[:12]
    with portable_pg_connect(url, autocommit=True) as administrator:
        administrator.execute(f'CREATE SCHEMA "{schema}"')
    try:
        result = run_api(
            {
                "SPEECHNEXUS_ENVIRONMENT": "test",
                "SPEECHNEXUS_PROCESS_ROLE": "api",
                "SPEECHNEXUS_HOST": "127.0.0.1",
                "SPEECHNEXUS_PORT": "18098",
                "SPEECHNEXUS_DATABASE_URL": url,
                "SPEECHNEXUS_DATABASE_SCHEMA": schema,
                "SPEECHNEXUS_OBJECT_STORAGE_ENDPOINT": "http://127.0.0.1:1",
                "SPEECHNEXUS_OBJECT_STORAGE_HEALTHCHECK_URL": "http://127.0.0.1:1/",
                "SPEECHNEXUS_OBJECT_STORAGE_BUCKET": "speechnexus-results",
                "SPEECHNEXUS_OBJECT_STORAGE_ACCESS_KEY": "gate-output-access",
                "SPEECHNEXUS_OBJECT_STORAGE_SECRET_KEY": "gate-output-secret",
                "SPEECHNEXUS_SPEECH_ENABLED": "false",
                "SPEECHNEXUS_JOB_WRITER_DATABASE_URL": url,
                "SPEECHNEXUS_JOB_ADMISSION_DATABASE_URL": url,
            }
        )
    finally:
        with portable_pg_connect(url, autocommit=True) as administrator:
            administrator.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')

    text = result.stdout + result.stderr
    assert result.returncode == 3, text[-2000:]
    assert "Traceback" in text
    assert "JobSchemaNotMigratedError" in text
    assert JOB_SCHEMA_DETAIL in text
    # Not a `FAIL:` line: that shape belongs to the synchronous settlement preflight.
    assert not [line for line in text.splitlines() if line.strip().startswith("FAIL:")], text[
        -2000:
    ]
    # uvicorn logs the server process and then fails in the lifespan; the promise is
    # about the failure shape, not about what precedes it.
    assert "Application startup failed. Exiting." in text
